"""
Handlery narzedzi web_search i web_fetch — internet dla agenta, bez kluczy API (core/websearch.py).

Agent sam decyduje, kiedy szukac. Zabezpieczenia:
  - zapytanie i adres z sekretem sa odrzucane (wyszlyby poza serwer),
  - web_fetch czyta bez pytania tylko adresy z wynikow web_search tej sesji albo podane przez
    uzytkownika; kazdy inny adres wymaga potwierdzenia (adres moze nesc dane na zewnatrz),
  - tylko publiczny internet (bez sieci prywatnej i metadanych chmury, takze po przekierowaniu),
  - strone czyta osobne zapytanie do modelu bez narzedzi (WEB_READER_PROMPT) — do agenta trafia
    odpowiedz na pytanie, a nie surowa strona,
  - po przeczytaniu tresci z internetu YOLO jest wstrzymane do nastepnej wiadomosci uzytkownika.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from backend.config import settings
from backend.core import audit, memory, software, websearch
from backend.core.events import Event, Progress
from backend.core.handlers.common import reply
from backend.core.i18n import prompt, tr
from backend.core.session import ConfirmationRequest, Session
from backend.core.text import as_code, visible

MAX_QUERY_CHARS = 300
# Ile tekstu strony dostaje model czytajacy i ile surowego tekstu wraca, gdy nie ma pytania.
READER_CHARS = 60_000
RAW_CHARS = 12_000


def _short(text: str, limit: int = 120) -> str:
    text = visible(" ".join(text.split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _user_gave(session: Session, url: str) -> bool:
    """
    Czy uzytkownik sam wpisal ten adres (wiadomosci zbudowane przez backend sie nie licza, tresc
    zalacznikow tez nie — `pipe_text` to sam tekst wpisany przez uzytkownika).
    """
    key = websearch.normalize_url(url)
    typed = (m.get("pipe_text", m.get("content")) for m in session.messages
             if m.get("role") == "user" and not m.get("pipe_generated"))
    return bool(key) and any(isinstance(text, str) and key in text for text in typed)


async def handle_web_search(agent: Any, session: Session, tool_call: Any, args: dict[str, Any]) -> AsyncGenerator[Event, None]:
    query = " ".join(str(args.get("query", "") or "").split())
    if settings.WEB_SEARCH == "off":
        reply(session, tool_call, tr("Blad: dostep do internetu jest wylaczony (WEB_SEARCH=off).",
                                     "Error: internet access is disabled (WEB_SEARCH=off)."))
        return
    if not query:
        reply(session, tool_call, tr("Blad: podaj query.", "Error: give a query."))
        return
    if len(query) > MAX_QUERY_CHARS:
        reply(session, tool_call, tr(f"Blad: zapytanie jest za dlugie (maks. {MAX_QUERY_CHARS} znakow) — "
                                     "szukaj krotkimi, ogolnymi frazami.",
                                     f"Error: the query is too long (max {MAX_QUERY_CHARS} characters) — "
                                     "search with short, general phrases."))
        return
    label = memory.find_secret(query)
    if label:
        await audit.log_blocked(session.interface, f"web_search(<{label}>)")
        reply(session, tool_call, tr(f"ODMOWA SYSTEMOWA: zapytanie zawiera sekret ({label}) i wyszloby poza serwer. "
                                     "Szukaj ogolnymi frazami, bez hasel, tokenow i kluczy.",
                                     f"SYSTEM REFUSAL: the query contains a secret ({label}) and would leave the server. "
                                     "Search with general phrases, without passwords, tokens or keys."))
        return

    yield Progress(tr(f"Szukam w sieci: {_short(query)}", f"Searching the web: {_short(query)}"))
    try:
        response = await websearch.search(query, limit=int(args.get("max_results") or 6))
    except websearch.WebError as exc:
        await audit.log_safe(session.interface, f"web_search({query})", 1)
        reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
        return
    await audit.log_safe(session.interface, f"web_search({query})", 0)
    session.web_tainted = True
    session.web_urls.update(websearch.normalize_url(r.url) for r in response.results)
    reply(session, tool_call, response.render())


async def handle_web_fetch(agent: Any, session: Session, tool_call: Any, args: dict[str, Any]) -> AsyncGenerator[Event, None]:
    url = str(args.get("url", "") or "").strip()
    question = str(args.get("question", "") or "").strip()
    if settings.WEB_SEARCH == "off":
        reply(session, tool_call, tr("Blad: dostep do internetu jest wylaczony (WEB_SEARCH=off).",
                                     "Error: internet access is disabled (WEB_SEARCH=off)."))
        return
    try:
        websearch.check_url(url)
    except websearch.WebError as exc:
        reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
        return
    label = memory.find_secret(url)
    if label:
        await audit.log_blocked(session.interface, f"web_fetch(<{label}>)")
        reply(session, tool_call, tr(f"ODMOWA SYSTEMOWA: adres zawiera sekret ({label}) i wyszedlby poza serwer.",
                                     f"SYSTEM REFUSAL: the address contains a secret ({label}) and would leave the server."))
        return

    async def read() -> str:
        page = await websearch.fetch(url)
        session.web_tainted = True
        session.web_urls.add(websearch.normalize_url(page.final_url))
        header = websearch.page_header(page)
        if not page.text.strip():
            return header + "\n\n" + tr("(strona nie zawiera tekstu — moze wymaga JavaScriptu)",
                                        "(the page has no text — it may need JavaScript)")
        if question:
            content = page.text[:READER_CHARS]
            try:
                answer = await agent.complete(
                    prompt("WEB_READER_PROMPT"),
                    tr(f"PYTANIE: {question}\n\nSTRONA ({page.final_url}, tytul: {page.title}):\n{content}",
                       f"QUESTION: {question}\n\nPAGE ({page.final_url}, title: {page.title}):\n{content}"),
                    model=settings.WORKER_MODEL or None, who=session.interface)
                return header + "\n\n" + tr("Odpowiedz na podstawie strony:\n", "Answer based on the page:\n") + answer.strip()
            except Exception as exc:                         # czytnik niedostepny — wraca sam tekst
                note = tr(f"(czytnik strony niedostepny: {exc}; ponizej poczatek tekstu)",
                          f"(page reader unavailable: {exc}; the start of the text follows)")
                return f"{header}\n{note}\n\n{page.text[:RAW_CHARS]}"
        more = tr("\n\n[... strona przycieta — zadaj web_fetch konkretne pytanie (question) ...]",
                  "\n\n[... page truncated — give web_fetch a specific question ...]") \
            if len(page.text) > RAW_CHARS or page.truncated else ""
        return f"{header}\n\n{page.text[:RAW_CHARS]}{more}"

    known = websearch.normalize_url(url) in session.web_urls or _user_gave(session, url)
    if not known:
        async def action() -> str:
            try:
                return await read()
            except websearch.WebError as exc:
                return tr(f"Blad: {exc}", f"Error: {exc}")

        session.pending_confirmation = ConfirmationRequest(
            tool_call_id=tool_call.id, tool_name="web_fetch", command=f"web_fetch {url}",
            classification="confirm", action=action)
        yield tr(f"[POTWIERDZ] Pobranie strony spoza wynikow wyszukiwania wymaga potwierdzenia: {as_code(url)}",
                 f"[POTWIERDZ] Fetching a page outside the search results requires confirmation: {as_code(url)}")
        return

    yield Progress(tr(f"Czytam strone: {_short(url)}", f"Reading page: {_short(url)}"))
    try:
        result = await read()
    except websearch.WebError as exc:
        await audit.log_safe(session.interface, f"web_fetch({url})", 1)
        reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
        return
    await audit.log_safe(session.interface, f"web_fetch({url})", 0)
    reply(session, tool_call, result)


async def handle_software_info(agent: Any, session: Session, tool_call: Any, args: dict[str, Any]) -> AsyncGenerator[Event, None]:
    """Koniec wsparcia (endoflife.date) i znane podatnosci (OSV.dev) — publiczne API bez klucza."""
    operation = str(args.get("operation", "") or "").strip().lower()
    if settings.WEB_SEARCH == "off":
        reply(session, tool_call, tr("Blad: dostep do internetu jest wylaczony (WEB_SEARCH=off).",
                                     "Error: internet access is disabled (WEB_SEARCH=off)."))
        return
    fields = {k: str(args.get(k, "") or "").strip() for k in ("product", "version", "ecosystem", "package")}
    label = memory.find_secret(" ".join(fields.values()))
    if label:
        reply(session, tool_call, tr(f"ODMOWA SYSTEMOWA: argumenty zawieraja sekret ({label}).",
                                     f"SYSTEM REFUSAL: the arguments contain a secret ({label})."))
        return
    if operation == "eol":
        if not fields["product"]:
            reply(session, tool_call, tr("Blad: podaj product, np. 'ubuntu', 'postgresql', 'nginx'.",
                                         "Error: give a product, e.g. 'ubuntu', 'postgresql', 'nginx'."))
            return
        what = f"eol {fields['product']} {fields['version']}".strip()
        yield Progress(tr(f"Sprawdzam wsparcie: {_short(what[4:])}", f"Checking support: {_short(what[4:])}"))
        call = lambda: software.eol(fields["product"], fields["version"])  # noqa: E731
    elif operation == "vulns":
        what = f"vulns {fields['ecosystem']} {fields['package']} {fields['version']}"
        yield Progress(tr(f"Sprawdzam podatnosci: {_short(fields['package'] + ' ' + fields['version'])}",
                          f"Checking vulnerabilities: {_short(fields['package'] + ' ' + fields['version'])}"))
        call = lambda: software.vulns(fields["ecosystem"], fields["package"], fields["version"])  # noqa: E731
    else:
        reply(session, tool_call, tr(f"Nieznana operacja {operation!r}. Dostepne: eol, vulns.",
                                     f"Unknown operation {operation!r}. Available: eol, vulns."))
        return
    try:
        result = await asyncio.to_thread(call)
    except websearch.WebError as exc:
        await audit.log_safe(session.interface, f"software_info({what})", 1)
        reply(session, tool_call, tr(f"Blad: {exc}", f"Error: {exc}"))
        return
    await audit.log_safe(session.interface, f"software_info({what})", 0)
    session.web_tainted = True            # opisy podatnosci pisza obcy ludzie — to tez tresc z internetu
    reply(session, tool_call, result)
