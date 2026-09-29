"""
Agent -- petla LLM z tool calling do zarzadzania serwerami.

Pipe v0.10.0

Cykl jednej wiadomosci:
  1. Uzytkownik wysyla wiadomosc
  2. Agent dodaje ja do historii i wysyla do LLM
  3. LLM odpowiada: tool_call albo tekst
  4. tool_call -> handler (core/handlers) -> klasyfikacja -> wykonanie/potwierdzenie/odmowa
  5. Wynik (po redakcji sekretow) wraca do LLM jako tool_result
  6. LLM formuluje odpowiedz po polsku, ktora trafia do uzytkownika

Petla yielduje zdarzenia (core/events.py): tekst, zalaczniki (diagramy)
i statusy posrednie (workery). Te sama petle -- z innym zestawem narzedzi
i promptem -- uruchamiaja workery (core/workers.py).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from backend.config import settings
from backend.config.providers import NO_KEY_PLACEHOLDER, chat_model_ids, model_available
from backend.core import audit, executor, memory, runtime, usage
from backend.core.events import Event
from backend.core.session import ConfirmationRequest, Session
from backend.core.tools import TOOLS
import backend.core.handlers as handlers_module

# Wynik jednego narzedzia dla modelu — dluzszy jest przycinany (poczatek + koniec).
MAX_TOOL_RESULT_CHARS = 24_000
# Historia sesji — starsze tury sa odcinane na granicy wiadomosci uzytkownika.
MAX_HISTORY_MESSAGES = 120

SKIPPED_FOR_CONFIRMATION = (
    "NIE WYKONANO: poprzednie narzedzie czeka na potwierdzenie uzytkownika. "
    "Jesli to wywolanie jest nadal potrzebne, powtorz je po jego decyzji."
)
INTERRUPTED_TOOL = "PRZERWANO: klient rozlaczyl sie, zanim narzedzie skonczylo. Wynik nieznany."
ABANDONED_CONFIRMATION = (
    "Uzytkownik nie potwierdzil tej operacji -- zamiast odpowiedziec TAK/NIE napisal nowa wiadomosc. "
    "Operacja NIE zostala wykonana."
)


class VPSAgent:
    """
    Autonomiczny agent AI do zarzadzania serwerami.

    Utrzymuje sesje rozmow i obsluguje petle LLM z tool calling.
    Jeden egzemplarz obsluguje wiele sesji rownoczesnie.
    """

    def __init__(self, client: Any = None) -> None:
        llm = settings.LLM
        self._client = client or AsyncOpenAI(
            api_key=llm.api_key or NO_KEY_PLACEHOLDER,
            base_url=llm.base_url,
            timeout=llm.timeout,  # modele rozumujace potrafia odpowiadac dluzej niz minute
        )
        self._sessions: dict[str, Session] = {}
        from backend.core.vibe import VibeLearner
        self.vibe = VibeLearner(self)

    # ─── Sesje ──────────────────────────────────────────────────────────────

    def get_or_create_session(self, session_id: str, interface: str = "cli") -> Session:
        """Zwraca istniejaca sesje lub tworzy nowa."""
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(session_id=session_id, interface=interface)
        return self._sessions[session_id]

    def delete_session(self, session_id: str) -> None:
        """Usuwa sesje (np. po rozlaczeniu klienta)."""
        self._sessions.pop(session_id, None)

    # ─── API dla serwera ────────────────────────────────────────────────────

    async def chat(
        self,
        session_id: str,
        user_message: str,
        interface: str = "cli",
        *,
        generated: bool = False,
    ) -> AsyncGenerator[Event, None]:
        """
        Przetwarza wiadomosc uzytkownika i strumieniuje zdarzenia odpowiedzi.
        Nie rzuca wyjatkow — bledy wracaja jako tekst [BLAD].

        `generated=True` — tresc zbudowal backend (skan, /status, skill), nie
        uzytkownik; nie uczy VIBE.
        """
        session = self.get_or_create_session(session_id, interface)

        # Nowa wiadomosc zamiast TAK/NIE: operacja przepada, a wywolanie narzedzia
        # dostaje odpowiedz — inaczej provider odrzuci historie z nieodpowiedzianym tool_call.
        if session.pending_confirmation:
            pending = session.pending_confirmation
            session.pending_confirmation = None
            session.messages.append({"role": "tool", "tool_call_id": pending.tool_call_id,
                                     "content": ABANDONED_CONFIRMATION})

        _repair_history(session)
        message: dict[str, Any] = {"role": "user", "content": user_message}
        if generated:
            message["pipe_generated"] = True
        session.messages.append(message)
        _trim_history(session)
        self.vibe.observe(session)

        try:
            async for event in self.run_loop(session):
                yield event
        except Exception as exc:
            yield f"[BLAD] Blad wykonania: {exc}"

    async def confirm(self, session_id: str, confirmed: bool) -> AsyncGenerator[Event, None]:
        """Obsluguje TAK/NIE dla oczekujacej operacji i kontynuuje petle."""
        session = self._sessions.get(session_id)
        if not session or not session.pending_confirmation:
            yield "[OSTRZEZENIE] Brak oczekujacej operacji do potwierdzenia."
            return

        pending = session.pending_confirmation
        session.pending_confirmation = None

        if confirmed:
            await self._execute_tool_confirmed(session, pending)
        else:
            session.messages.append({"role": "tool", "tool_call_id": pending.tool_call_id,
                                     "content": "Uzytkownik odmowil wykonania tej operacji."})

        try:
            async for event in self.run_loop(session):
                yield event
        except Exception as exc:
            yield f"[BLAD] Blad po potwierdzeniu: {exc}"

    # ─── Petla ──────────────────────────────────────────────────────────────

    async def run_loop(
        self,
        session: Session,
        *,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
        model: str | None = None,
        max_iterations: int | None = None,
        dispatch: Any = None,
    ) -> AsyncGenerator[Event, None]:
        """
        Petla LLM z tool calling. Parametry pozwalaja uruchomic ja dla workera:
        wlasny prompt, zestaw narzedzi, model i funkcja dispatch(session, tool_call, args).
        """
        iterations = max_iterations or settings.AGENT_MAX_ITERATIONS
        for _ in range(iterations):
            response = await self.call_llm(
                system_prompt if system_prompt is not None else session.system_prompt,
                session.messages,
                tools=TOOLS if tools is None else tools,
                model=model,
                who=session.interface,
            )
            message = response.choices[0].message
            session.messages.append(message.model_dump(exclude_unset=True, exclude_none=True))

            if not message.tool_calls:
                yield message.content or ""
                return

            for tool_call in message.tool_calls:
                if session.pending_confirmation:
                    # Kazde wywolanie musi dostac odpowiedz, nawet gdy nie zostalo wykonane.
                    session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                             "content": SKIPPED_FOR_CONFIRMATION})
                    continue
                async for event in self._handle_tool_call(session, tool_call, dispatch):
                    yield event

            if session.pending_confirmation:
                return

        yield (
            "[OSTRZEZENIE] Agent osiagnal limit krokow. "
            "Napisz \"kontynuuj\", zebym dokonczyl, albo podziel zadanie na mniejsze kroki."
        )

    async def call_llm(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        who: str = "",
    ) -> ChatCompletion:
        """
        Wysyla historie do LLM i zwraca odpowiedz. Zuzycie tokenow trafia do
        licznika (core/usage.py) z etykieta `who` (interfejs sesji); przy
        wyczerpanym dziennym limicie rzuca usage.BudgetExceeded bez zapytania.
        """
        usage.check_budget(settings.DAILY_TOKEN_LIMIT, settings.DAILY_COST_LIMIT)
        # tool_choice pomijamy celowo: "auto" jest i tak domyslne, gdy podano
        # tools, a czesc providerow (np. Ollama) nie obsluguje tego parametru.
        # reasoning_effort idzie przez extra_body, zeby dzialal na kazdej
        # wersji SDK; providerzy, ktorzy go nie znaja, ignoruja pole.
        extra_body: dict[str, Any] = {}
        if settings.LLM.reasoning_effort:
            extra_body["reasoning_effort"] = settings.LLM.reasoning_effort
        # Klucze pipe_* to metadane Pipe — providerzy odrzucaja nieznane pola wiadomosci.
        clean = [{k: v for k, v in m.items() if not k.startswith("pipe_")} for m in messages]
        kwargs: dict[str, Any] = {
            "model": model or settings.LLM.model,
            "messages": [{"role": "system", "content": system_prompt}, *clean],
            "extra_body": extra_body or None,
        }
        if tools:
            kwargs["tools"] = tools
        response = await self._client.chat.completions.create(**kwargs)
        _record_usage(kwargs["model"], getattr(response, "usage", None), who)
        return response

    async def complete(self, system_prompt: str, user_message: str, model: str | None = None,
                       who: str = "") -> str:
        """Jedno zapytanie bez narzedzi (np. aktualizacja VIBE). Zwraca tekst."""
        response = await self.call_llm(system_prompt, [{"role": "user", "content": user_message}],
                                       model=model, who=who)
        return response.choices[0].message.content or ""

    async def verify_model(self) -> str | None:
        """
        Sprawdza, czy skonfigurowany model jest na liscie providera.
        Zwraca tresc ostrzezenia albo None. Nigdy nie rzuca wyjatku.
        """
        llm = settings.LLM
        try:
            page = await asyncio.wait_for(self._client.models.list(), timeout=15)
            available = chat_model_ids([m.model_dump() for m in page.data])
        except Exception as exc:
            return f"Nie udalo sie pobrac listy modeli od {llm.provider_name} ({exc}) — pomijam weryfikacje modelu."

        if not available or model_available(llm.model, available):
            return None
        return (
            f"Model {llm.model!r} nie wystepuje na liscie modeli {llm.provider_name} — "
            f"mogl zostac wycofany. Najnowsze dostepne: {', '.join(available[:5])}. "
            "Zmien model: python3 -m backend.configure"
        )

    # ─── Narzedzia ──────────────────────────────────────────────────────────

    async def _handle_tool_call(self, session: Session, tool_call: Any, dispatch: Any = None) -> AsyncGenerator[Event, None]:
        """Obsluguje jedno wywolanie narzedzia i porzadkuje jego wynik dla modelu."""
        before = len(session.messages)
        try:
            args = json.loads(tool_call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise json.JSONDecodeError("argumenty nie sa obiektem", "", 0)
        except json.JSONDecodeError as exc:
            session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                     "content": f"Blad: argumenty narzedzia nie sa poprawnym JSON-em ({exc})."})
            return

        if dispatch is not None:
            handler = lambda s, tc, a: dispatch(s, tc, a)  # noqa: E731
        else:
            found = getattr(handlers_module, f"handle_{tool_call.function.name}", None)
            handler = (lambda s, tc, a: found(self, s, tc, a)) if found else None

        if handler is None:
            session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                     "content": f"Nieznane narzedzie: {tool_call.function.name}"})
            return

        try:
            async for event in handler(session, tool_call, args):
                yield event
        except Exception as exc:  # blad handlera nie moze zostawic wywolania bez odpowiedzi
            if not _answered(session, tool_call.id, before) and session.pending_confirmation is None:
                session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                         "content": f"Blad narzedzia: {exc}"})
            yield f"[BLAD] Narzedzie {tool_call.function.name} zglosilo blad: {exc}"

        if not _answered(session, tool_call.id, before) and (
            session.pending_confirmation is None or session.pending_confirmation.tool_call_id != tool_call.id
        ):
            session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                     "content": "Narzedzie nie zwrocilo wyniku."})
        _sanitize_new_results(session, before)

    async def _execute_tool_confirmed(self, session: Session, pending: ConfirmationRequest) -> str:
        """
        Wykonuje zatwierdzona operacje i dopisuje wynik do historii.

        Handler nie jest wywolywany ponownie: `action` (operacje na rejestrach Pipe)
        jest wywolywana wprost, write_file odtwarza sciezke i tresc, a kazde inne
        narzedzie przechowuje w ConfirmationRequest gotowa komende shell.
        """
        before = len(session.messages)
        from backend.core.handlers.common import format_result

        if pending.action is not None:
            try:
                result = await pending.action()
                await audit.log_confirmed(session.interface, pending.command, 0)
            except Exception as exc:
                await audit.log_confirmed(session.interface, pending.command, 1)
                result = f"Blad: {exc}"
        elif pending.tool_name == "write_file":
            path = pending.file_path or ""
            try:
                await executor.write_file(path, pending.file_content or "")
                await audit.log_file_write(session.interface, path, 0)
                result = f"Plik {runtime.to_host(path)} zostal zapisany pomyslnie."
            except PermissionError as exc:
                await audit.log_file_write(session.interface, path, 1)
                result = f"Blad zapisu (brak uprawnien): {exc}"
            except OSError as exc:
                await audit.log_file_write(session.interface, path, 1)
                result = f"Blad zapisu pliku: {exc}"
        else:
            stdout, stderr, exit_code = await executor.execute(
                pending.command,
                cwd=runtime.to_local(session.cwd),
                timeout=settings.CONFIRMED_COMMAND_TIMEOUT,
            )
            await audit.log_confirmed(session.interface, pending.command, exit_code)
            result = format_result(stdout, stderr, exit_code)

        session.messages.append({"role": "tool", "tool_call_id": pending.tool_call_id, "content": result})
        _sanitize_new_results(session, before)
        return result


# ─── Pomocnicze ─────────────────────────────────────────────────────────────

def _record_usage(model: str, response_usage: Any, who: str) -> None:
    """Licznik kosztow nigdy nie przerywa rozmowy — blad zapisu jest tylko logowany."""
    main_model = settings.LLM.model if settings.LLM else ""
    worker = bool(settings.WORKER_MODEL) and model == settings.WORKER_MODEL and model != main_model
    prices = usage.Prices(settings.WORKER_PRICE_IN, settings.WORKER_PRICE_OUT) if worker \
        else usage.Prices(settings.LLM_PRICE_IN, settings.LLM_PRICE_OUT)
    try:
        usage.record(model, response_usage, usage.who_from_interface(who), prices)
    except OSError as exc:
        print(f"[usage] Nie zapisano zuzycia tokenow: {exc}", flush=True)


def _answered(session: Session, tool_call_id: str, since: int) -> bool:
    return any(m.get("role") == "tool" and m.get("tool_call_id") == tool_call_id for m in session.messages[since:])


def _repair_history(session: Session) -> None:
    """
    Domyka wywolania narzedzi bez odpowiedzi — zostaja, gdy klient rozlaczy sie
    w trakcie petli (generator zostaje zamkniety w polowie handlera).
    """
    for index in range(len(session.messages) - 1, -1, -1):
        message = session.messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            answered = {m.get("tool_call_id") for m in session.messages[index + 1:] if m.get("role") == "tool"}
            for call in message["tool_calls"]:
                if call.get("id") not in answered:
                    session.messages.append({"role": "tool", "tool_call_id": call["id"], "content": INTERRUPTED_TOOL})
            return
        if message.get("role") == "user":
            return


def truncate_result(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Przycina dlugi wynik, zostawiajac poczatek i koniec (bledy sa zwykle na koncu)."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n\n[... pominieto {len(text) - limit} znakow ...]\n\n{text[-tail:]}"


def _sanitize_new_results(session: Session, since: int) -> None:
    """Redakcja sekretow i przycinanie wynikow narzedzi dopisanych od indeksu `since`."""
    for message in session.messages[since:]:
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        if settings.REDACT_SECRETS:
            content, count = memory.redact_secrets(content)
            if count:
                content += f"\n[System: ukryto {count} sekret(ow) przed wyslaniem do modelu.]"
        message["content"] = truncate_result(content)


def _trim_history(session: Session, limit: int = MAX_HISTORY_MESSAGES) -> None:
    """
    Odcina najstarsze tury, gdy historia jest za dluga. Ciecie zawsze na
    wiadomosci uzytkownika — para tool_call/tool_result nie moze zostac rozdzielona.
    """
    if len(session.messages) <= limit:
        return
    cut = len(session.messages) - limit
    for index in range(cut, len(session.messages)):
        if session.messages[index].get("role") == "user":
            del session.messages[:index]
            return


# --- Globalna instancja agenta ---
_agent: VPSAgent | None = None


def get_agent() -> VPSAgent:
    """Zwraca globalna instancje agenta (singleton)."""
    global _agent
    if _agent is None:
        _agent = VPSAgent()
    return _agent
