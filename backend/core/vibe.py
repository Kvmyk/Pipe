"""
VIBE — agent z czasem dopasowuje sie do stylu rozmowy uzytkownika.

Co VIBE_EVERY wiadomosci danego uzytkownika agent w tle (bez blokowania
odpowiedzi) wysyla do LLM obecna notatke VIBE i ostatnie wiadomosci
uzytkownika i zapisuje zaktualizowana notatke (vibe/<klucz>.md w DATA_DIR).
Notatka wraca do system promptu przy kazdej rozmowie z tym uzytkownikiem.

Do destylacji trafiaja tylko wiadomosci pisane przez uzytkownika — bez
promptow skladanych przez backend (skan, /status, skille), bez wynikow
narzedzi. Uzytkownik moze zobaczyc i wyczyscic notatke (/vibe, /vibe reset).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from backend.config import settings
from backend.config.prompts import VIBE_DISTILL_PROMPT
from backend.core import audit, memory
from backend.core.session import Session

MAX_MESSAGE_CHARS = 600


def recent_user_messages(session: Session, limit: int) -> list[str]:
    """Ostatnie wiadomosci uzytkownika (od najstarszej), pomijajac wygenerowane przez backend."""
    messages = []
    for message in reversed(session.messages):
        if message.get("role") != "user" or message.get("pipe_generated"):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            messages.append(content.strip()[:MAX_MESSAGE_CHARS])
        if len(messages) >= limit:
            break
    return list(reversed(messages))


def clean_distilled(text: str) -> str | None:
    """Tresc notatki z odpowiedzi modelu albo None, gdy odpowiedz nie wyglada na notatke."""
    text = (text or "").strip()
    fence = re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n?```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("# VIBE"):
        return None
    return text[:memory.MAX_VIBE_CHARS]


class VibeLearner:
    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self._counts: dict[str, int] = {}
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def observe(self, session: Session) -> None:
        """Wolane po kazdej wiadomosci uzytkownika. Nie blokuje."""
        every = settings.VIBE_EVERY
        if every <= 0 or not session.learns_vibe:
            return
        last = session.messages[-1] if session.messages else {}
        if last.get("pipe_generated"):
            return
        key = session.user_key
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] % every or key in self._running:
            return
        messages = recent_user_messages(session, every * 2)
        if not messages:
            return
        self._running.add(key)
        task = asyncio.create_task(self._distill(key, messages))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _distill(self, key: str, messages: list[str]) -> None:
        try:
            await self.distill(key, messages)
        except Exception as exc:  # nauka stylu nigdy nie psuje rozmowy
            print(f"[VIBE] Nie zaktualizowano notatki {key}: {exc}", flush=True)
        finally:
            self._running.discard(key)

    async def distill(self, key: str, messages: list[str]) -> str | None:
        current = memory.read_vibe(key).strip() or "# VIBE\n\n(brak obserwacji)"
        listing = "\n".join(f"- {m}" for m in messages)
        prompt = (f"Obecna notatka:\n<<<\n{current}\n>>>\n\n"
                  f"Ostatnie wiadomosci uzytkownika (od najstarszej):\n{listing}")
        answer = await self._agent.complete(VIBE_DISTILL_PROMPT, prompt, model=settings.WORKER_MODEL or None,
                                            who="vibe")
        text = clean_distilled(answer)
        if text is None or text.strip() == current.strip():
            return None
        try:
            memory.write_vibe(key, text)
        except memory.MemoryWriteError:
            return None
        await audit.log_file_write("vibe", f"vibe/{memory.vibe_key(key)}.md", 0)
        return text
