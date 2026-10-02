"""
Agent -- petla LLM z tool calling do zarzadzania serwerami.

Pipe v0.19.0

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
import dataclasses
import json
from typing import Any, AsyncGenerator

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from backend.config import settings
from backend.config.providers import NO_KEY_PLACEHOLDER, LLMConfig, chat_model_ids, model_available
from backend.core import audit, executor, llm as llm_choice, memory, runtime, usage
from backend.core.i18n import tr
from backend.core.events import Activity, Event
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
SKIPPED_FOR_CONFIRMATION_EN = (
    "NOT EXECUTED: the previous tool is waiting for the user's confirmation. "
    "If this call is still needed, repeat it after their decision."
)
VIEWER_NOTICE = "Twoja rola pozwala tylko na odczyt — tej zmiany nie wykonam. Moze ja zatwierdzic administrator."
VIEWER_NOTICE_EN = "Your role is read-only — I will not make this change. An administrator can approve it."
VIEWER_REFUSAL = ("ODMOWA SYSTEMOWA: uzytkownik ma role viewer (tylko odczyt). Operacja nie zostala wykonana — "
                  "opisz, co trzeba zrobic; zmiane moze zatwierdzic administrator.")
VIEWER_REFUSAL_EN = ("SYSTEM REFUSAL: the user has the viewer role (read-only). The operation was not executed — "
                     "describe what needs to be done; an administrator can approve the change.")
# Operacje, ktore zmieniaja pamiec/rejestry Pipe bez potwierdzenia — dla viewera zablokowane z gory.
VIEWER_WRITE_OPERATIONS: dict[str, frozenset[str] | None] = {
    "write_file": None,
    "server_md": frozenset({"update_section", "write"}),
    "directory": frozenset({"upsert", "remove", "scan"}),
    "skill_manage": frozenset({"save", "delete"}),
    "target_manage": frozenset({"add", "remove"}),
    "routine_manage": frozenset({"add", "remove", "enable", "disable"}),
    "journal": frozenset({"undo"}),
    "cron_manage": frozenset({"add", "remove"}),
    "mcp_manage": frozenset({"add", "remove", "reload"}),
    "pipe_update": frozenset({"apply"}),
}
INTERRUPTED_TOOL = "PRZERWANO: klient rozlaczyl sie, zanim narzedzie skonczylo. Wynik nieznany."
INTERRUPTED_TOOL_EN = "INTERRUPTED: the client disconnected before the tool finished. Result unknown."
ABANDONED_CONFIRMATION = (
    "Uzytkownik nie potwierdzil tej operacji -- zamiast odpowiedziec TAK/NIE napisal nowa wiadomosc. "
    "Operacja NIE zostala wykonana."
)
ABANDONED_CONFIRMATION_EN = (
    "The user did not confirm this operation -- instead of answering YES/NO they wrote a new message. "
    "The operation was NOT executed."
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
        # Klienci providerow wybranych w trakcie pracy (core/llm.py), po (adres, klucz).
        self._injected = client is not None
        self._clients: dict[tuple[str, str], Any] = {}
        self._sessions: dict[str, Session] = {}
        from backend.core.vibe import VibeLearner
        self.vibe = VibeLearner(self)

    # ─── Sesje ──────────────────────────────────────────────────────────────

    def get_or_create_session(self, session_id: str, interface: str = "cli", *, owner: str = "",
                              role: str = "admin") -> Session:
        """Zwraca istniejaca sesje lub tworzy nowa. Rola jest ustawiana przy kazdym zadaniu."""
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(session_id=session_id, interface=interface, owner=owner)
        session = self._sessions[session_id]
        session.role = role
        return session

    def owns(self, session_id: str, owner: str) -> bool:
        """Czy klient o tej tozsamosci moze uzyc sesji (nieistniejaca sesja — tak)."""
        session = self._sessions.get(session_id)
        return session is None or session.owner in ("", owner)

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
        owner: str = "",
        role: str = "admin",
    ) -> AsyncGenerator[Event, None]:
        """
        Przetwarza wiadomosc uzytkownika i strumieniuje zdarzenia odpowiedzi.
        Nie rzuca wyjatkow — bledy wracaja jako tekst [BLAD].

        `generated=True` — tresc zbudowal backend (skan, /status, skill), nie
        uzytkownik; nie uczy VIBE.
        """
        session = self.get_or_create_session(session_id, interface, owner=owner, role=role)

        # Nowa wiadomosc zamiast TAK/NIE: operacja przepada, a wywolanie narzedzia
        # dostaje odpowiedz — inaczej provider odrzuci historie z nieodpowiedzianym tool_call.
        if session.pending_confirmation:
            pending = session.pending_confirmation
            session.pending_confirmation = None
            session.messages.append({"role": "tool", "tool_call_id": pending.tool_call_id,
                                     "content": tr(ABANDONED_CONFIRMATION, ABANDONED_CONFIRMATION_EN)})

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
            yield tr(f"[BLAD] Blad wykonania: {exc}", f"[BLAD] Execution error: {exc}")

    async def confirm(self, session_id: str, confirmed: bool) -> AsyncGenerator[Event, None]:
        """Obsluguje TAK/NIE dla oczekujacej operacji i kontynuuje petle."""
        session = self._sessions.get(session_id)
        if not session or not session.pending_confirmation:
            yield tr("[OSTRZEZENIE] Brak oczekujacej operacji do potwierdzenia.",
                     "[OSTRZEZENIE] There is no pending operation to confirm.")
            return

        pending = session.pending_confirmation
        session.pending_confirmation = None

        if confirmed:
            try:
                if pending.activity is not None:
                    yield dataclasses.replace(pending.activity, phase="start")
                async for event in self._execute_tool_confirmed(session, pending):
                    yield event
            finally:
                # Klient rozlaczyl sie w trakcie weryfikacji — wywolanie i tak musi dostac odpowiedz.
                if not _answered(session, pending.tool_call_id, 0):
                    session.messages.append({"role": "tool", "tool_call_id": pending.tool_call_id,
                                             "content": tr(INTERRUPTED_TOOL, INTERRUPTED_TOOL_EN)})
        else:
            if pending.activity is not None:
                yield dataclasses.replace(pending.activity, phase="end", ok=None)
            session.messages.append({"role": "tool", "tool_call_id": pending.tool_call_id,
                                     "content": tr("Uzytkownik odmowil wykonania tej operacji.",
                                                   "The user declined this operation.")})

        try:
            async for event in self.run_loop(session):
                yield event
        except Exception as exc:
            yield tr(f"[BLAD] Blad po potwierdzeniu: {exc}", f"[BLAD] Error after confirmation: {exc}")

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
                tools=tools_for_agent() if tools is None else tools,
                model=model,
                who=session.interface,
            )
            message = response.choices[0].message
            entry = message.model_dump(exclude_unset=True, exclude_none=True)
            # Skad pochodzi odpowiedz — po przelaczeniu providera jego pola wlasne nie ida do innego.
            entry["pipe_provider"] = self.active_llm()[0].provider_id
            session.messages.append(entry)

            if not message.tool_calls:
                yield message.content or ""
                return

            for tool_call in message.tool_calls:
                if session.pending_confirmation:
                    # Kazde wywolanie musi dostac odpowiedz, nawet gdy nie zostalo wykonane.
                    session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                             "content": tr(SKIPPED_FOR_CONFIRMATION, SKIPPED_FOR_CONFIRMATION_EN)})
                    continue
                async for event in self._handle_tool_call(session, tool_call, dispatch):
                    yield event

            if session.pending_confirmation:
                return

        yield tr(
            "[OSTRZEZENIE] Agent osiagnal limit krokow. "
            "Napisz \"kontynuuj\", zebym dokonczyl, albo podziel zadanie na mniejsze kroki.",
            "[OSTRZEZENIE] The agent reached its step limit. "
            "Write \"continue\" so I can finish, or split the task into smaller steps."
        )

    def active_llm(self) -> tuple[LLMConfig, Any]:
        """
        (konfiguracja, klient) providera, z ktorego agent korzysta teraz — bazowy z .env albo
        wybrany w interfejsie (core/llm.py). Wstrzykniety klient (testy) obsluguje kazdy wybor.
        """
        base = settings.LLM
        config = llm_choice.resolve(base)
        if self._injected or (config.base_url, config.api_key) == (base.base_url, base.api_key):
            return config, self._client
        key = (config.base_url, config.api_key)
        if key not in self._clients:
            self._clients[key] = AsyncOpenAI(api_key=config.api_key or NO_KEY_PLACEHOLDER, base_url=config.base_url,
                                             timeout=config.timeout)
        return config, self._clients[key]

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
        config, client = self.active_llm()
        switched = config.provider_id != settings.LLM.provider_id
        extra_body: dict[str, Any] = {}
        if config.reasoning_effort:
            extra_body["reasoning_effort"] = config.reasoning_effort
        clean = [_for_provider(m, config.provider_id) for m in messages]
        kwargs: dict[str, Any] = {
            # WORKER_MODEL to nazwa modelu u providera bazowego — u innego nie istnieje.
            "model": config.model if switched or not model else model,
            "messages": [{"role": "system", "content": system_prompt}, *clean],
            "extra_body": extra_body or None,
        }
        if tools:
            kwargs["tools"] = tools
        response = await client.chat.completions.create(**kwargs)
        _record_usage(kwargs["model"], getattr(response, "usage", None), who, priced=not switched)
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
        llm, client = self.active_llm()
        try:
            page = await asyncio.wait_for(client.models.list(), timeout=15)
            available = chat_model_ids([m.model_dump() for m in page.data])
        except Exception as exc:
            return tr(f"Nie udalo sie pobrac listy modeli od {llm.provider_name} ({exc}) — pomijam weryfikacje modelu.",
                      f"Could not fetch the model list from {llm.provider_name} ({exc}) — skipping model verification.")

        if not available or model_available(llm.model, available):
            return None
        return tr(
            f"Model {llm.model!r} nie wystepuje na liscie modeli {llm.provider_name} — "
            f"mogl zostac wycofany. Najnowsze dostepne: {', '.join(available[:5])}. "
            "Zmien model: python3 -m backend.configure",
            f"Model {llm.model!r} is not on {llm.provider_name}'s model list — "
            f"it may have been retired. Newest available: {', '.join(available[:5])}. "
            "Change the model: python3 -m backend.configure"
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
                                     "content": tr(f"Blad: argumenty narzedzia nie sa poprawnym JSON-em ({exc}).",
                                                   f"Error: the tool arguments are not valid JSON ({exc}).")})
            return

        if dispatch is not None:
            handler = lambda s, tc, a: dispatch(s, tc, a)  # noqa: E731
        else:
            found = getattr(handlers_module, f"handle_{tool_call.function.name}", None)
            if found is None and tool_call.function.name.startswith("mcp__"):
                from backend.core.handlers.mcp import handle_mcp_tool
                found = handle_mcp_tool
            handler = (lambda s, tc, a: found(self, s, tc, a)) if found else None

        if handler is None:
            session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                     "content": tr(f"Nieznane narzedzie: {tool_call.function.name}",
                                                   f"Unknown tool: {tool_call.function.name}")})
            return

        viewer = session.role == "viewer" and dispatch is None
        if viewer and viewer_blocked(tool_call.function.name, args):
            session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                     "content": tr(VIEWER_REFUSAL, VIEWER_REFUSAL_EN)})
            yield f"[ODMOWA] {tr(VIEWER_NOTICE, VIEWER_NOTICE_EN)}"
            return

        # Interfejs webowy rysuje schemat na zywo: co agent robi i na ktorym elemencie.
        activity: Activity | None = None
        if session.shows_activity and dispatch is None and not viewer:
            from backend.core import graph
            name = tool_call.function.name
            workers = tuple((str(t.get("name") or t.get("target") or ""), str(t.get("target") or ""))
                            for t in args.get("tasks") or [] if isinstance(t, dict)) if name == "delegate" else ()
            activity = Activity(str(tool_call.id), "start", name, graph.describe(name, args),
                                tuple(graph.locate(name, args, session.cwd)), workers=workers)
            yield activity
        failed = False

        buffered: list[Event] = []
        try:
            async for event in handler(session, tool_call, args):
                if viewer:
                    buffered.append(event)   # przegladajacy nie moze zobaczyc pytania o TAK, ktorego nie zatwierdzi
                else:
                    yield event
        except Exception as exc:  # blad handlera nie moze zostawic wywolania bez odpowiedzi
            if not _answered(session, tool_call.id, before) and session.pending_confirmation is None:
                session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                         "content": tr(f"Blad narzedzia: {exc}", f"Tool error: {exc}")})
            failed = True
            yield tr(f"[BLAD] Narzedzie {tool_call.function.name} zglosilo blad: {exc}",
                     f"[BLAD] Tool {tool_call.function.name} raised an error: {exc}")

        if viewer:
            pending = session.pending_confirmation
            if pending is not None and pending.tool_call_id == tool_call.id:
                session.pending_confirmation = None
                session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                         "content": tr(VIEWER_REFUSAL, VIEWER_REFUSAL_EN)})
                buffered = [e for e in buffered if not (isinstance(e, str) and "[POTWIERDZ]" in e)]
                buffered.append(f"[ODMOWA] {tr(VIEWER_NOTICE, VIEWER_NOTICE_EN)}")
            for event in buffered:
                yield event

        if not _answered(session, tool_call.id, before) and (
            session.pending_confirmation is None or session.pending_confirmation.tool_call_id != tool_call.id
        ):
            session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                     "content": tr("Narzedzie nie zwrocilo wyniku.", "The tool returned no result.")})
        _sanitize_new_results(session, before)

        if activity is not None:
            pending = session.pending_confirmation
            if pending is not None and pending.tool_call_id == tool_call.id:
                pending.activity = activity
                yield dataclasses.replace(activity, phase="wait")
            else:
                yield dataclasses.replace(activity, phase="end", ok=not failed and not _refused(session, tool_call.id, before))

    async def _execute_tool_confirmed(self, session: Session, pending: ConfirmationRequest) -> AsyncGenerator[Event, None]:
        """
        Wykonuje zatwierdzona operacje i dopisuje wynik do historii. Yielduje postep bezpiecznika.

        Handler nie jest wywolywany ponownie: `action` (operacje na rejestrach Pipe)
        jest wywolywana wprost, write_file odtwarza sciezke i tresc, a kazde inne
        narzedzie przechowuje w ConfirmationRequest gotowa komende shell.
        Zmiany (wszystko poza akcja bez planu, np. odczytem pliku z sekretami) ida
        przez bezpiecznik (core/safety.py): kopia do dziennika, sprawdzenie przed,
        weryfikacja po, automatyczne przywrocenie plikow — wedlug planu z potwierdzenia.
        """
        before = len(session.messages)
        from backend.core import safety
        from backend.core.handlers.common import format_result

        async def run_action() -> tuple[str, int]:
            try:
                result = await pending.action()
                await audit.log_confirmed(session.interface, pending.command, 0)
                return result, 0
            except Exception as exc:
                await audit.log_confirmed(session.interface, pending.command, 1)
                return f"Blad: {exc}", 1

        async def run_write() -> tuple[str, int]:
            path = pending.file_path or ""
            try:
                await executor.write_file(path, pending.file_content or "")
                await audit.log_file_write(session.interface, path, 0)
                return tr(f"Plik {runtime.to_host(path)} zostal zapisany pomyslnie.",
                          f"File {runtime.to_host(path)} was written successfully."), 0
            except PermissionError as exc:
                await audit.log_file_write(session.interface, path, 1)
                return tr(f"Blad zapisu (brak uprawnien): {exc}", f"Write error (permission denied): {exc}"), 1
            except OSError as exc:
                await audit.log_file_write(session.interface, path, 1)
                return tr(f"Blad zapisu pliku: {exc}", f"File write error: {exc}"), 1

        async def run_command() -> tuple[str, int]:
            stdout, stderr, exit_code = await executor.execute(
                pending.command,
                cwd=runtime.to_local(session.cwd),
                timeout=settings.CONFIRMED_COMMAND_TIMEOUT,
            )
            await audit.log_confirmed(session.interface, pending.command, exit_code)
            return format_result(stdout, stderr, exit_code), exit_code

        exit_code, entry_id = 0, ""
        if pending.action is not None and pending.plan is None:
            result, exit_code = await run_action()
        else:
            if pending.action is not None:
                operation, plan = run_action, pending.plan
            elif pending.tool_name == "write_file":
                operation = run_write
                plan = pending.plan or safety.plan_write(runtime.to_host(pending.file_path or ""))
            else:
                operation, plan = run_command, pending.plan or safety.Plan()
            result = ""
            from backend.core import checks
            async for item in safety.guarded(
                plan, operation, interface=session.interface, tool=pending.tool_name, description=pending.command,
                cwd=runtime.to_local(session.cwd), auto_restore=settings.SAFE_AUTO_ROLLBACK,
                sites_enabled=settings.WATCH_SITES, site_ignore=checks.parse_ignore(settings.WATCH_IGNORE),
            ):
                if isinstance(item, safety.Outcome):
                    result, exit_code, entry_id = item.text, item.exit_code, item.entry_id
                else:
                    yield item

        session.messages.append({"role": "tool", "tool_call_id": pending.tool_call_id, "content": result})
        _sanitize_new_results(session, before)
        if pending.activity is not None:
            yield dataclasses.replace(pending.activity, phase="end", ok=exit_code == 0, entry=entry_id)


# ─── Pomocnicze ─────────────────────────────────────────────────────────────

def _for_provider(message: dict[str, Any], provider_id: str) -> dict[str, Any]:
    """
    Wiadomosc historii w postaci dla providera. Klucze pipe_* to metadane Pipe. Odpowiedz innego
    providera (rozmowa sprzed przelaczenia) zostaje sprowadzona do pol standardu Chat Completions —
    pola wlasne (np. podpisy rozumowania Gemini) inny provider odrzucilby jako nieznane.
    """
    origin = message.get("pipe_provider")
    clean = {k: v for k, v in message.items() if not k.startswith("pipe_")}
    if origin is None or origin == provider_id or message.get("role") != "assistant":
        return clean
    plain: dict[str, Any] = {"role": "assistant", "content": clean.get("content") or ""}
    calls = [{"id": call.get("id"), "type": "function",
              "function": {"name": (call.get("function") or {}).get("name"),
                           "arguments": (call.get("function") or {}).get("arguments") or "{}"}}
             for call in clean.get("tool_calls") or [] if isinstance(call, dict)]
    if calls:
        plain["tool_calls"] = calls
    return plain


def _record_usage(model: str, response_usage: Any, who: str, *, priced: bool = True) -> None:
    """
    Licznik kosztow nigdy nie przerywa rozmowy — blad zapisu jest tylko logowany.
    `priced=False`: provider wybrany w interfejsie — ceny z .env dotycza bazowego, wiec liczymy same tokeny.
    """
    main_model = settings.LLM.model if settings.LLM else ""
    worker = bool(settings.WORKER_MODEL) and model == settings.WORKER_MODEL and model != main_model
    if not priced:
        prices = usage.Prices()
    elif worker:
        prices = usage.Prices(settings.WORKER_PRICE_IN, settings.WORKER_PRICE_OUT)
    else:
        prices = usage.Prices(settings.LLM_PRICE_IN, settings.LLM_PRICE_OUT)
    try:
        usage.record(model, response_usage, usage.who_from_interface(who), prices)
    except OSError as exc:
        print(tr(f"[usage] Nie zapisano zuzycia tokenow: {exc}", f"[usage] Token usage not saved: {exc}"), flush=True)


def tools_for_agent() -> list[dict]:
    """Narzedzia Pipe + narzedzia polaczonych serwerow MCP (lista zmienia sie po mcp_manage)."""
    from backend.core.mcp.registry import get_manager

    from backend.core.i18n import is_en

    base = _english_tools() if is_en() else TOOLS
    extra = get_manager().tool_schemas()
    return base + extra if extra else base


_ENGLISH_TOOLS: list[dict] | None = None


def _english_tools() -> list[dict]:
    global _ENGLISH_TOOLS
    if _ENGLISH_TOOLS is None:
        from backend.core.tools_en import english_tools
        _ENGLISH_TOOLS = english_tools(TOOLS)
    return _ENGLISH_TOOLS


def viewer_blocked(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name not in VIEWER_WRITE_OPERATIONS:
        return False
    operations = VIEWER_WRITE_OPERATIONS[tool_name]
    return operations is None or str(args.get("operation", "") or "").strip().lower() in operations


def _refused(session: Session, tool_call_id: str, since: int) -> bool:
    """Czy wynik narzedzia to odmowa albo blad (do stanu dzialania na schemacie)."""
    for message in session.messages[since:]:
        if message.get("role") == "tool" and message.get("tool_call_id") == tool_call_id:
            content = str(message.get("content", "")).lstrip()
            return content.startswith(("ODMOWA", "REFUSED", "SYSTEM REFUSAL", "Blad", "Błąd", "Error", "[BLAD]", "[ODMOWA]"))
    return False


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
                    session.messages.append({"role": "tool", "tool_call_id": call["id"],
                                             "content": tr(INTERRUPTED_TOOL, INTERRUPTED_TOOL_EN)})
            return
        if message.get("role") == "user":
            return


def truncate_result(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Przycina dlugi wynik, zostawiajac poczatek i koniec (bledy sa zwykle na koncu)."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    skipped = tr(f"[... pominieto {len(text) - limit} znakow ...]", f"[... {len(text) - limit} characters omitted ...]")
    return f"{text[:head]}\n\n{skipped}\n\n{text[-tail:]}"


def _sanitize_new_results(session: Session, since: int) -> None:
    """Redakcja sekretow i przycinanie wynikow narzedzi dopisanych od indeksu `since`."""
    for message in session.messages[since:]:
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        if settings.REDACT_SECRETS:
            content, count = memory.redact_secrets(content)
            if count:
                content += tr(f"\n[System: ukryto {count} sekret(ow) przed wyslaniem do modelu.]",
                              f"\n[System: {count} secret(s) hidden before sending to the model.]")
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
