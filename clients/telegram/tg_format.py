"""
Formatowanie odpowiedzi backendu dla Telegrama (parse_mode=HTML).

Modul jest bez zaleznosci od python-telegram-bot, zeby dalo sie go testowac
zwyklym `pytest backend/tests/`.

Zasady:
  - Tekst w backtickach (`inline` i ```blok```) jest DOSLOWNY. Tak backend
    pokazuje komendy do potwierdzenia, wiec uzytkownik musi zobaczyc dokladnie
    to, co zostanie wykonane — bez zamiany *gwiazdek* na pogrubienie itp.
  - Tagi dozwolone przez Telegram zostaja, caly pozostaly tekst jest escapowany.
    Telegram odrzuca wiadomosc, w ktorej '<', '>' lub '&' nie sa czescia tagu
    albo encji (https://core.telegram.org/bots/api#html-style).
"""

from __future__ import annotations

import html
import os
import re


def lang() -> str:
    """Jezyk bota: PIPE_LANG=pl (domyslnie) albo en — ten sam przelacznik co w backendzie."""
    return "en" if os.getenv("PIPE_LANG", "pl").strip().lower().startswith("en") else "pl"


def tr(pl, en):
    """Tekst w jezyku bota."""
    return en if lang() == "en" else pl


# Tagi HTML obslugiwane przez Telegram.
_ALLOWED_TAG = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|span|tg-spoiler|tg-emoji|blockquote)"
    r"(?:\s[^<>]*)?>",
    re.IGNORECASE,
)
# Otoczki jak w CommonMark: dowolnie dlugi ciag backtickow, zamykany ciagiem
# tej samej dlugosci — dzieki temu komenda moze sama zawierac backticki.
# Blok wymaga nowej linii po otwarciu, inaczej ```x``` w linii to kod inline.
_CODE_BLOCK = re.compile(r"(`{3,})[\w-]*\n(.*?)\n?\1(?!`)", re.DOTALL)
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")

TELEGRAM_MAX_LENGTH = 4000


def _escape_text(text: str) -> str:
    """Escapuje tekst poza tagami. unescape najpierw — encje wstawione przez LLM nie sa dublowane."""
    return html.escape(html.unescape(text), quote=False)


def _sanitize_html(text: str) -> str:
    """Zostawia dozwolone tagi Telegrama, reszte tekstu escapuje."""
    out: list[str] = []
    pos = 0
    for match in _ALLOWED_TAG.finditer(text):
        out.append(_escape_text(text[pos:match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_escape_text(text[pos:]))
    return "".join(out)


def to_telegram_html(text: str, allow_tags: bool = True) -> str:
    """
    Zamienia odpowiedz backendu (HTML od LLM + ewentualne resztki Markdowna
    + komunikaty protokolu z komendami w backtickach) na poprawny HTML Telegrama.

    allow_tags=False: tagi HTML w tekscie sa pokazywane doslownie (escapowane), dziala tylko
    Markdown i backticki. Dla raportow pisanych bez nadzoru (workery, rutyny) — tresc, na ktora
    mogl wplynac cudzy tekst z serwera, nie wstawi uzytkownikowi linku ani ukrytego fragmentu.
    """
    # 1. Wytnij fragmenty kodu, zanim cokolwiek je zmieni. Zawartosc jest
    #    escapowana bez unescape — komenda ma byc pokazana znak w znak.
    code: list[str] = []

    def _stash(tag: str):
        def replace(match: re.Match[str]) -> str:
            content = match.group(2)
            # CommonMark: jedna spacja z obu stron to czesc otoczki, nie tresci.
            if tag == "code" and len(content) > 2 and content[0] == content[-1] == " " and content.strip():
                content = content[1:-1]
            code.append(f"<{tag}>{html.escape(content, quote=False)}</{tag}>")
            return f"\x00{len(code) - 1}\x00"
        return replace

    text = _CODE_BLOCK.sub(_stash("pre"), text)
    text = _INLINE_CODE.sub(_stash("code"), text)

    if not allow_tags:
        text = html.escape(text, quote=False)

    # 2. Resztki Markdowna, ktore LLM moze wygenerowac mimo instrukcji.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!\\])", r"\1", text)  # escape'y MarkdownV2
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 3. Escapuj wszystko poza dozwolonymi tagami, potem wstaw kod z powrotem.
    if allow_tags:
        text = _sanitize_html(text)
    return _PLACEHOLDER.sub(lambda m: code[int(m.group(1))], text)


def to_plain_text(text: str) -> str:
    """Fallback, gdy Telegram mimo wszystko odrzuci HTML: usun tagi, rozwin encje."""
    return html.unescape(_ALLOWED_TAG.sub("", text))


def collect_response_text(responses: list[dict]) -> tuple[str, bool]:
    """
    Zbiera tekst odpowiedzi z listy fragmentow.

    Returns:
        (text, needs_confirmation)
    """
    parts: list[str] = []
    notices: list[str] = []
    needs_confirm = False

    for resp in responses:
        text = resp.get("response", "")
        status = resp.get("status", "ok")
        if text:
            # Powiadomienia o zmianie w pamieci agenta MUSZA dotrzec do uzytkownika,
            # nawet gdy nizej wybierzemy zwiezle podsumowanie modelu — inaczej injection
            # moglby zmienic pamiec bez wiedzy uzytkownika. Zbieramy je osobno.
            if text.lstrip().startswith("[PAMIEC]"):
                notices.append(tr("<b>Pamiec:</b> ", "<b>Memory:</b> ") + text.split("]", 1)[1].strip())
                continue
            # Tagi statusowe -- tylko ODMOWA jest widoczna
            # [SUKCES] -- usuniety calkowicie
            text = text.replace("[SUKCES]", "")
            text = text.replace("[BLAD]", tr("<b>BLAD:</b>", "<b>ERROR:</b>"))
            text = text.replace("[POTWIERDZ]", tr("<b>WYMAGA POTWIERDZENIA:</b>", "<b>NEEDS CONFIRMATION:</b>"))
            text = text.replace("[ODMOWA]", tr("<b>ODMOWA:</b>", "<b>REFUSED:</b>"))
            if lang() == "en":
                text = text.replace("[OSTRZEZENIE]", "<b>WARNING:</b>")

            parts.append(text)
        if status == "confirm":
            needs_confirm = True

    def _with_notices(body: str) -> str:
        return "\n".join(notices + [body]) if notices else body

    # Jeśli wiele części odpowiedzi, wybierz najbardziej zwięzłe podsumowanie
    # Preferuj fragmenty zawierające czytelne podsumowanie serwera.
    if parts:
        # Jeśli ostatni fragment jest prostym komunikatem o błędzie, zwróć go
        last = parts[-1].strip()
        if last.lower().startswith(("błąd:", "blad:", "error:")):
            return _with_notices(last), needs_confirm

        # W przeciwnym razie wybierz ostatni fragment który nie zaczyna się od '['
        for part in reversed(parts):
            txt = part.strip()
            if txt and not txt.startswith("["):
                return _with_notices(part), needs_confirm

        # Fallback: zwróć ostatni fragment
        return _with_notices(parts[-1]), needs_confirm

    return ("\n".join(notices) if notices else ""), needs_confirm


def split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Dzieli dluga wiadomosc na kawalki <= max_length znakow."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        # Znajdz ostatni znak nowej linii przed limitem
        split_at = text.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ─── Komendy "/" ────────────────────────────────────────────────────────────

# Komendy wbudowane w menu Telegrama, w kolejnosci wyswietlania.
BUILTIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("status", "Szybki przeglad obciazenia serwera"),
    ("aktualizuj", "Zaktualizuj Pipe (/aktualizuj sprawdz - tylko wersja)"),
    ("raport", "Poranny raport: stan, zmiany, certyfikaty, backupy"),
    ("zmiany", "Co sie zmienilo na serwerze (/zmiany 3d)"),
    ("wykres", "Wykres load/ram/dysk (/wykres ram 7d)"),
    ("zdrowie", "Certyfikaty, strony, DNS i backupy"),
    ("audyt", "Audyt bezpieczenstwa z ocena i poprawkami"),
    ("mapa", "Diagram infrastruktury serwera (obraz)"),
    ("server", "Pokaz SERVER.md (/server aktualizuj - zbadaj serwer ponownie)"),
    ("katalogi", "Mapa repozytoriow i katalogow (DIRECTORY)"),
    ("skille", "Lista zapisanych skilli"),
    ("alerty", "Aktywne alerty czuwania"),
    ("incydenty", "Pamiec incydentow: przyczyny i co pomoglo"),
    ("rutyny", "Zadania wykonywane wedlug harmonogramu"),
    ("przypomnienia", "Jednorazowe przypomnienia (/przypomnienia anuluj <id>)"),
    ("cele", "Zdalne serwery, kontenery i klastry"),
    ("vibe", "Co wiem o Twoim stylu rozmowy (/vibe reset - wyczysc)"),
    ("cofnij", "Cofnij ostatnia zmiane (/cofnij <id> - wybrana)"),
    ("dziennik", "Dziennik zatwierdzonych zmian"),
    ("zgody", "Operacje agentow MCP czekajace na zgode"),
    ("mcp", "Serwery MCP, z ktorych korzysta Pipe"),
    ("koszt", "Zuzycie tokenow i koszt LLM"),
    ("historia", "Ostatnie wpisy z audit logu"),
    ("pomoc", "Lista komend"),
)
BUILTIN_COMMANDS_EN: tuple[tuple[str, str], ...] = (
    ("status", "Quick look at the server load"),
    ("update", "Update Pipe (/update check - version only)"),
    ("report", "Morning report: health, changes, certificates, backups"),
    ("changes", "What changed on the server (/changes 3d)"),
    ("chart", "Chart of load/ram/disk (/chart ram 7d)"),
    ("health", "Certificates, sites, DNS and backups"),
    ("audit", "Security audit with a score and fixes"),
    ("map", "Server infrastructure diagram (image)"),
    ("server", "Show SERVER.md (/server update - explore the server again)"),
    ("directory", "Map of repositories and directories (DIRECTORY)"),
    ("skills", "List of saved skills"),
    ("alerts", "Active watcher alerts"),
    ("incidents", "Incident memory: causes and what helped"),
    ("routines", "Tasks run on a schedule"),
    ("reminders", "One-off reminders (/reminders cancel <id>)"),
    ("targets", "Remote servers, containers and clusters"),
    ("vibe", "What I know about your conversation style (/vibe reset - clear)"),
    ("undo", "Undo the last change (/undo <id> - a chosen one)"),
    ("journal", "Journal of approved changes"),
    ("approvals", "MCP agent operations waiting for approval"),
    ("mcp", "MCP servers Pipe uses"),
    ("cost", "Token usage and LLM cost"),
    ("history", "Latest audit-log entries"),
    ("help", "List of commands"),
)
# Angielskie nazwy komend -> polskie (handlery sa rejestrowane pod obiema nazwami w obu jezykach).
COMMAND_ALIASES: dict[str, str] = {
    "report": "raport", "changes": "zmiany", "chart": "wykres", "health": "zdrowie", "audit": "audyt",
    "map": "mapa", "directory": "katalogi", "skills": "skille", "alerts": "alerty", "routines": "rutyny",
    "targets": "cele", "undo": "cofnij", "journal": "dziennik", "approvals": "zgody", "cost": "koszt",
    "history": "historia", "help": "pomoc", "incidents": "incydenty", "reminders": "przypomnienia",
    "update": "aktualizuj",
}


def builtin_commands() -> tuple[tuple[str, str], ...]:
    """Komendy wbudowane w jezyku bota."""
    return tr(BUILTIN_COMMANDS, BUILTIN_COMMANDS_EN)


def command_names(polish: str) -> list[str]:
    """Polska nazwa komendy + jej angielskie aliasy — do rejestracji handlera."""
    return [polish] + [en for en, pl in COMMAND_ALIASES.items() if pl == polish]


MAX_MENU_COMMANDS = 100        # limit Telegrama
MAX_COMMAND_DESCRIPTION = 256  # limit Telegrama

SCAN_WORDS = frozenset({"aktualizuj", "odswiez", "odśwież", "skanuj", "update", "refresh", "scan"})


def parse_command(text: str) -> tuple[str, str]:
    """'/deploy_app@PipeBot na produkcji' -> ('deploy_app', 'na produkcji')."""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    name = parts[0].lstrip("/").split("@", 1)[0].lower()
    return name, parts[1].strip() if len(parts) > 1 else ""


def build_menu(skills: list[dict]) -> list[tuple[str, str]]:
    """Menu '/' Telegrama: komendy wbudowane, potem skille, ktore maja komende."""
    menu = list(builtin_commands())
    for skill in skills:
        if skill.get("command"):
            description = " ".join((skill.get("description") or "").split()) or "Skill"
            menu.append((skill["command"], description[:MAX_COMMAND_DESCRIPTION]))
    return menu[:MAX_MENU_COMMANDS]


def format_skill_list(skills: list[dict]) -> str:
    """Lista skilli jako HTML Telegrama."""
    if not skills:
        return tr("Brak zapisanych skilli. Po wykonaniu wieloetapowej procedury popros: "
                  "<i>\"zapisz to jako skill\"</i>.",
                  "No saved skills. After a multi-step procedure ask: <i>\"save this as a skill\"</i>.")
    lines = [tr("<b>Skille</b>", "<b>Skills</b>")]
    for skill in skills:
        description = html.escape(skill.get("description") or "")
        if skill.get("command"):
            lines.append(f"• /{skill['command']} — {description}")
        else:
            name = html.escape(skill["name"])
            lines.append(f"• <code>{name}</code> — {description} "
                         + tr(f"<i>(bez komendy — napisz: uruchom skill {name})</i>",
                              f"<i>(no command — write: run the skill {name})</i>"))
    example = next((s["command"] for s in skills if s.get("command")), None)
    if example:
        lines.append(tr(f"\nDo komendy mozesz dopisac wskazowki, np. <code>/{example} tylko dla example.com</code>",
                        f"\nYou can add hints to a command, e.g. <code>/{example} only for example.com</code>"))
    return "\n".join(lines)


def format_help(skills: list[dict]) -> str:
    """Pomoc (/pomoc) jako HTML Telegrama."""
    lines = [tr("<b>Komendy</b>", "<b>Commands</b>")]
    lines += [f"/{command} — {html.escape(description)}" for command, description in builtin_commands()]
    with_command = [s for s in skills if s.get("command")]
    if with_command:
        lines.append(tr(f"\n<b>Skille ({len(with_command)})</b> — kazdy ma wlasna komende, pelna lista: /skille",
                        f"\n<b>Skills ({len(with_command)})</b> — each has its own command, full list: /skills"))
    lines.append(tr("\nMozesz tez po prostu pisac, np. <i>\"ile mam wolnego miejsca?\"</i>",
                    "\nYou can also just write, e.g. <i>\"how much free space do I have?\"</i>"))
    return "\n".join(lines)


def response_data(responses: list[dict]) -> dict:
    """Pole 'data' z odpowiedzi na zadanie {"command": ...}; blad backendu -> RuntimeError."""
    for resp in reversed(responses):
        if "data" in resp:
            return resp["data"]
    error = next((r.get("response") for r in responses if r.get("status") == "error"), "") or tr("brak danych", "no data")
    raise RuntimeError(error)



# ─── Czuwanie, rutyny, listy ────────────────────────────────────────────────

SEVERITY_LABEL = {"critical": "KRYTYCZNY", "warning": "OSTRZEZENIE"}
SEVERITY_LABEL_EN = {"critical": "CRITICAL", "warning": "WARNING"}


def format_alert(event: dict) -> str:
    """Alert czuwania jako HTML Telegrama."""
    title = html.escape(str(event.get("title", "")))
    detail = html.escape(str(event.get("detail", "")))
    if event.get("state") == "resolved":
        return tr(f"<b>ROZWIAZANE</b> — {title}", f"<b>RESOLVED</b> — {title}")
    label = tr(SEVERITY_LABEL, SEVERITY_LABEL_EN).get(str(event.get("severity")), "ALERT")
    text = f"<b>{label}</b> — {title}\n<i>{detail}</i>" if detail else f"<b>{label}</b> — {title}"
    if event.get("history"):
        text += tr("\n<b>Poprzednio:</b> ", "\n<b>Previously:</b> ") + html.escape(str(event["history"]))
    return text


_WORKER_HEAD = re.compile(r"^=== WORKER (?P<name>\S+) \((?P<meta>[^)]*)\) ===\s*$", re.MULTILINE)
_STATUS_LINE = re.compile(r"\A\s*STATUS:\s*(OK|PROBLEM)\s*\n?", re.IGNORECASE)
# Etykiety sekcji raportu workera na poczatku linii: USTALENIA:, PROPOZYCJE:, FINDINGS:, PROPOSALS: ...
_SECTION_LABEL = re.compile(r"^([A-ZĄĆĘŁŃÓŚŹŻ][A-ZĄĆĘŁŃÓŚŹŻ ]{2,30}):", re.MULTILINE)
_LIST_DASH = re.compile(r"^(\s*)[-*] (?=\S)", re.MULTILINE)


def _outside_code(text: str, transform) -> str:
    """Stosuje `transform` do fragmentow tekstu poza blokami i wstawkami kodu (backticki)."""
    out, position = [], 0
    spans = sorted([m.span() for m in _CODE_BLOCK.finditer(text)] + [m.span() for m in _INLINE_CODE.finditer(text)])
    for start, end in spans:
        if start < position:
            continue                      # wstawka wewnatrz bloku juz pominietego
        out.append(transform(text[position:start]))
        out.append(text[start:end])
        position = end
    out.append(transform(text[position:]))
    return "".join(out)


def report_html(report: str, limit: int = 3000, strip_status: bool = False) -> str:
    """
    Raport workera (rutyna, badanie alertu, zadanie jednorazowe) jako zwykla wiadomosc: naglowek
    techniczny `=== WORKER ... ===` trafia do stopki, etykiety sekcji sa pogrubione, listy maja
    wypunktowanie, a komendy w backtickach zostaja doslowne. Tagi HTML z raportu sa escapowane.
    """
    text = str(report or "").strip()
    footer = ""
    head = _WORKER_HEAD.search(text)
    if head:
        footer = f"\n<i>{html.escape(head.group('meta'))}</i>"
        text = (text[:head.start()] + text[head.end():]).strip()
    if strip_status:
        text = _STATUS_LINE.sub("", text, count=1).strip()
    if len(text) > limit:
        text = text[:limit] + "\n[...]"
    if not text:
        return tr("<i>(pusty raport)</i>", "<i>(empty report)</i>") + footer
    # Etykiety i wypunktowania tylko poza kodem — Telegram nie pozwala na tagi wewnatrz <pre>/<code>,
    # a tresc w backtickach ma zostac znak w znak.
    text = _outside_code(text, lambda part: _LIST_DASH.sub(r"\1• ", _SECTION_LABEL.sub(r"**\1:**", part)))
    return to_telegram_html(text, allow_tags=False) + footer


def format_routine(event: dict, limit: int = 3000) -> str:
    """Raport rutyny jako HTML Telegrama: status w naglowku, tresc jako zwykly tekst (nie blok kodu)."""
    name = html.escape(str(event.get("name", "")))
    status = str(event.get("status", ""))
    mark = {"OK": "✅", "PROBLEM": "⚠️"}.get(status.upper(), "")
    head = tr(f"<b>Rutyna {name}</b>", f"<b>Routine {name}</b>") + f" — {mark} {html.escape(status)}".rstrip()
    return f"{head}\n\n{report_html(event.get('report', ''), limit, strip_status=True)}"


def format_list(title: str, items: list[str], empty: str) -> str:
    if not items:
        return f"<b>{html.escape(title)}</b>\n{empty}"
    return f"<b>{html.escape(title)}</b>\n" + "\n".join(f"• {html.escape(i)}" for i in items)


def format_alerts(data: dict) -> str:
    if not data.get("enabled", True):
        return tr("Czuwanie jest wylaczone (WATCH_ENABLED=0 w backend/.env).",
                  "The watcher is disabled (WATCH_ENABLED=0 in backend/.env).")
    active = data.get("active") or []
    lines = [tr("<b>Aktywne alerty</b>", "<b>Active alerts</b>")]
    lines += [format_alert(a) for a in active] or [tr("Brak — wszystko w normie.", "None — everything is normal.")]
    recent = [e for e in (data.get("recent") or []) if e.get("type") == "alert" and e.get("state") != "new"]
    if recent:
        lines.append(tr("\n<b>Ostatnie zdarzenia</b>", "\n<b>Recent events</b>"))
        lines += [f"{html.escape(str(e.get('at', '')))} — {format_alert(e)}" for e in recent[-5:]]
    return "\n".join(lines)


def format_directory(entries: list[dict]) -> str:
    if not entries:
        return tr("<b>DIRECTORY</b>\nMapa katalogow jest pusta. Napisz np. <i>\"znajdz repozytoria na serwerze\"</i> "
                  "albo uzyj /server aktualizuj.",
                  "<b>DIRECTORY</b>\nThe directory map is empty. Write e.g. <i>\"find the repositories on the server\"</i> "
                  "or use /server update.")
    lines = ["<b>DIRECTORY</b>"]
    for e in entries:
        extra = f" ({html.escape(e['branch'])})" if e.get("branch") else ""
        desc = html.escape(e.get("description") or "")
        lines.append(f"• <code>{html.escape(e['path'])}</code> [{html.escape(e.get('kind', ''))}]{extra} {desc}".rstrip())
    return "\n".join(lines)


def progress_text(lines: list[str], limit: int = 12) -> str:
    """Tresc wiadomosci-statusu aktualizowanej na biezaco (workery, rutyny)."""
    shown = lines[-limit:]
    skipped = len(lines) - len(shown)
    head = tr(f"<i>... i {skipped} wczesniej</i>\n", f"<i>... and {skipped} earlier</i>\n") if skipped else ""
    return tr("<b>W toku</b>\n", "<b>In progress</b>\n") + head + "\n".join(f"<code>{html.escape(l[:200])}</code>" for l in shown)


def format_digest(event: dict) -> str:
    """Poranny raport (zdarzenie albo dane z /raport) jako HTML Telegrama."""
    lines = [f"<b>{html.escape(str(event.get('title', tr('Raport', 'Report'))))}</b>"]
    for section in event.get("sections") or []:
        lines.append(f"\n<b>{html.escape(str(section.get('title', '')))}</b>")
        for line in section.get("lines") or []:
            text = html.escape(str(line))
            for mark in ("[BEZPIECZENSTWO]", "[SECURITY]"):
                if text.startswith(mark):
                    text = f"<b>{mark}</b>" + text[len(mark):]
            lines.append(f"• {text}")
    return "\n".join(lines)


_ENTRY_ID = re.compile(r"^#([0-9a-f]{6,10})\b")
_SECURITY_MARKS = ("[BEZPIECZENSTWO]", "[SECURITY]")


def _listing_line(body: str) -> str:
    """Tresc jednej pozycji: identyfikator (#ab12cd) do skopiowania jednym dotknieciem, znacznik bezpieczenstwa pogrubiony."""
    text = html.escape(body, quote=False)
    for mark in _SECURITY_MARKS:
        if text.startswith(mark):
            return f"<b>{mark}</b>" + text[len(mark):]
    return _ENTRY_ID.sub(r"<code>#\1</code>", text)


def format_listing(title: str, text: str, limit: int = 3500) -> str:
    """
    Zestawienie od backendu (bez LLM) jako zwykla wiadomosc — /zmiany, /zdrowie, /koszt, /dziennik,
    /przypomnienia, /incydenty. Uklad tekstu backendu: linia konczaca sie ':' to naglowek, '- ' to pozycja,
    wciecie 4+ spacji to szczegoly (np. roznica w pliku) — te zostaja czcionka o stalej szerokosci.
    """
    body = (text or "").strip("\n")
    if len(body) > limit:
        body = body[:limit] + "\n[...]"
    out = [f"<b>{html.escape(title)}</b>"] if title else []
    if not body.strip():
        return "\n".join(out + [tr("(pusto)", "(empty)")])
    for raw in body.splitlines():
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if not stripped:
            out.append("")
        elif indent >= 4:
            out.append(f"    <code>{html.escape(stripped, quote=False)}</code>")
        elif stripped.startswith("- "):
            out.append("• " + _listing_line(stripped[2:]))
        elif stripped.endswith(":") and not stripped.startswith("#"):
            out.append(("\n" if len(out) > 1 and out[-1] != "" else "") + f"<b>{html.escape(stripped, quote=False)}</b>")
        elif indent >= 2 or stripped.startswith("#"):
            out.append("• " + _listing_line(stripped))
        else:
            out.append(_listing_line(stripped))
    return "\n".join(out)


def format_pre(title: str, text: str, limit: int = 3500) -> str:
    """Doslowny tekst w bloku <pre> — wynik komendy, log audytu. Zestawienia: format_listing()."""
    body = (text or "").strip() or tr("(pusto)", "(empty)")
    if len(body) > limit:
        body = body[:limit] + "\n[...]"
    return f"<b>{html.escape(title)}</b>\n<pre>{html.escape(body)}</pre>"


AUDIT_SEVERITY = {"high": "WYSOKIE", "medium": "SREDNIE", "low": "NISKIE"}
AUDIT_SEVERITY_EN = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


def format_audit(data: dict) -> str:
    """Audyt bezpieczenstwa (/audyt) jako HTML Telegrama."""
    lines = [f"<b>{tr('Bezpieczenstwo', 'Security')}: {int(data.get('score', 0))}/100 "
             f"({html.escape(str(data.get('grade', '?')))})</b>"]
    for index, finding in enumerate(data.get("findings") or [], start=1):
        label = tr(AUDIT_SEVERITY, AUDIT_SEVERITY_EN).get(finding.get("severity"), "")
        lines.append(f"\n<b>{index}. [{label}]</b> {html.escape(str(finding.get('title', '')))}")
        if finding.get("detail"):
            lines.append(f"<i>{html.escape(str(finding['detail']))}</i>")
        if finding.get("fix"):
            lines.append(tr("Poprawka: ", "Fix: ") + html.escape(str(finding["fix"])))
        if finding.get("command"):
            where = tr(" (na hoscie)", " (on the host)") if finding.get("host_only") else ""
            lines.append(f"<code>{html.escape(str(finding['command']))}</code>{where}")
    if data.get("passed"):
        lines.append(tr("\n<b>W porzadku:</b> ", "\n<b>OK:</b> ") + html.escape("; ".join(data["passed"])))
    if data.get("findings"):
        lines.append(tr("\nNapisz <i>\"napraw 1\"</i> — przygotuje poprawke do zatwierdzenia.",
                        "\nWrite <i>\"fix 1\"</i> — I will prepare the fix for your approval."))
    return "\n".join(lines)


def format_investigation(event: dict, limit: int = 3000) -> str:
    """Raport workera, ktory sam zbadal alert z zewnatrz (webhook)."""
    report = str(event.get("report", "")).strip()
    if len(report) > limit:
        report = report[:limit] + "\n[...]"
    return (tr("<b>Zbadalem alert</b>", "<b>I investigated the alert</b>")
            + f" — {html.escape(str(event.get('title', '')))}\n\n{report_html(report, limit)}")


def format_reminder(event: dict, limit: int = 3000) -> str:
    """Przypomnienie, ktore wlasnie odpalilo: tresc doslownie; dla zadania — raport workera."""
    text = html.escape(str(event.get("text", "")))
    since = html.escape(str(event.get("set_at", "")))
    footer = tr(f"<i>ustawione {since}</i>", f"<i>set {since}</i>") if since else ""
    if event.get("kind") == "task":
        report = str(event.get("report", "")).strip()
        if len(report) > limit:
            report = report[:limit] + "\n[...]"
        return (tr("<b>Zadanie zaplanowane</b>", "<b>Scheduled task</b>") + f" — {text}\n\n"
                f"{report_html(report, limit, strip_status=True)}\n{footer}").rstrip()
    return (tr("<b>Przypomnienie</b>", "<b>Reminder</b>") + f"\n{text}\n{footer}").rstrip()


def reminder_recipients(event: dict, admins: set[int], viewers: set[int]) -> list[int]:
    """Przypomnienie z Telegrama wraca do tego, kto je ustawil; ustawione gdzie indziej (CLI) — do administratorow."""
    origin = str(event.get("to", ""))
    if origin.startswith("telegram:") and origin.split(":", 1)[1].isdigit():
        user_id = int(origin.split(":", 1)[1])
        if user_id in admins or user_id in viewers:
            return [user_id]
    return sorted(admins)


def format_approval(event: dict) -> str:
    """Prosba o zgode dla zewnetrznego agenta (MCP) — komenda doslownie, plan bezpiecznika."""
    agent = html.escape(str(event.get("requested_by", "?")))
    target = html.escape(str(event.get("target", "local")))
    lines = [tr(f"<b>ZGODA</b> — agent <code>{agent}</code> chce wykonac na celu <code>{target}</code>:",
                f"<b>APPROVAL</b> — agent <code>{agent}</code> wants to run on target <code>{target}</code>:"),
             f"<pre>{html.escape(str(event.get('command', '')))}</pre>"]
    if event.get("reason"):
        lines.append(f"<i>{tr('Powod', 'Reason')}: {html.escape(str(event['reason']))}</i>")
    if event.get("plan"):
        lines.append(html.escape(str(event["plan"])))
    lines.append(tr("<i>Zgoda wygasa po 30 minutach.</i>", "<i>The approval expires after 30 minutes.</i>"))
    return "\n".join(lines)
