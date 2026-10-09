"""
Sesje na dysku — rozmowa przetrwa restart backendu (/aktualizuj, awaria, restart kontenera).

Po kazdej turze (`VPSAgent.chat()` / `confirm()`) sesja trafia do DATA_DIR/sessions/<sha256 id>.json (0600),
a po restarcie wraca przy pierwszym zadaniu z tym samym session_id. Nie zapisujemy:
- oczekujacego potwierdzenia (callback `action` nie da sie odtworzyc) — wywolanie dostaje po restarcie
  odpowiedz „nie wykonano”, zeby historia dla providera byla poprawna,
- trybu YOLO (z zalozenia ginie przy restarcie) ani roli (ustawiana przy kazdym zadaniu),
- obrazow z zalacznikow (i tak zyja w historii tylko przez jedna ture).

Pliki starsze niz SESSION_KEEP_DAYS sa usuwane. Bez importu `settings` — testy ustawiaja DATA_DIR.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from backend.core import attachments, memory
from backend.core.i18n import tr
from backend.core.session import Session

FORMAT = 1
MAX_WEB_URLS = 200

RESTARTED = ("Backend Pipe zostal zrestartowany, zanim uzytkownik potwierdzil te operacje. "
             "Operacja NIE zostala wykonana — jesli jest nadal potrzebna, zaproponuj ja ponownie.")
RESTARTED_EN = ("The Pipe backend restarted before the user confirmed this operation. "
                "The operation was NOT executed — if it is still needed, propose it again.")


def directory() -> Path:
    return memory.data_dir() / "sessions"


def _path(session_id: str) -> Path:
    # Nazwa pliku z hasha — session_id pochodzi od klienta i nie moze wyznaczac sciezki.
    return directory() / f"{hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:32]}.json"


def _storable(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied = copy.deepcopy(messages)
    attachments.forget_images(copied)
    return copied


def _write_private(path: Path, text: str) -> None:
    """Zapis atomowy z prawami 0600 od pierwszego bajtu (historia moze zawierac dane z serwera)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)


def save(session: Session) -> None:
    pending = session.pending_confirmation
    data = {
        "format": FORMAT,
        "session_id": session.session_id,
        "interface": session.interface,
        "owner": session.owner,
        "cwd": session.cwd,
        "messages": _storable(session.messages),
        "workers": {name: _storable(history) for name, history in session.workers.items()},
        "web_urls": sorted(session.web_urls)[-MAX_WEB_URLS:],
        "pending_tool_call_id": pending.tool_call_id if pending else None,
        "saved": time.time(),
    }
    _write_private(_path(session.session_id), json.dumps(data, ensure_ascii=False))


def load(session_id: str, keep_days: int) -> Session | None:
    """Sesja z dysku albo None (brak, przeterminowana, uszkodzona, cudzy plik)."""
    path = _path(session_id)
    try:
        if time.time() - path.stat().st_mtime > keep_days * 86400:
            path.unlink(missing_ok=True)
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("session_id") != session_id:
        return None
    messages = [m for m in data.get("messages") or [] if isinstance(m, dict)]
    pending = data.get("pending_tool_call_id")
    if pending:
        messages.append({"role": "tool", "tool_call_id": str(pending), "content": tr(RESTARTED, RESTARTED_EN)})
    workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
    return Session(
        session_id=session_id,
        interface=str(data.get("interface") or "cli"),
        owner=str(data.get("owner") or ""),
        cwd=str(data.get("cwd") or "/"),
        messages=messages,
        workers={str(k): [m for m in v if isinstance(m, dict)] for k, v in workers.items() if isinstance(v, list)},
        web_urls={str(u) for u in data.get("web_urls") or []},
    )


def remove(session_id: str) -> None:
    _path(session_id).unlink(missing_ok=True)


def prune(keep_days: int) -> int:
    """Usuwa sesje starsze niz keep_days (0 = wszystkie — zapis wylaczony). Zwraca liczbe usunietych."""
    folder = directory()
    if not folder.is_dir():
        return 0
    limit = time.time() - keep_days * 86400
    removed = 0
    for path in folder.glob("*.json"):
        try:
            if keep_days <= 0 or path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
