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
import re

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


def to_telegram_html(text: str) -> str:
    """
    Zamienia odpowiedz backendu (HTML od LLM + ewentualne resztki Markdowna
    + komunikaty protokolu z komendami w backtickach) na poprawny HTML Telegrama.
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

    # 2. Resztki Markdowna, ktore LLM moze wygenerowac mimo instrukcji.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!\\])", r"\1", text)  # escape'y MarkdownV2
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 3. Escapuj wszystko poza dozwolonymi tagami, potem wstaw kod z powrotem.
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
                notices.append("<b>Pamiec:</b> " + text.split("]", 1)[1].strip())
                continue
            # Tagi statusowe -- tylko ODMOWA jest widoczna
            # [SUKCES] -- usuniety calkowicie
            text = text.replace("[SUKCES]", "")
            text = text.replace("[BLAD]", "<b>BLAD:</b>")
            text = text.replace("[POTWIERDZ]", "<b>WYMAGA POTWIERDZENIA:</b>")
            text = text.replace("[ODMOWA]", "<b>ODMOWA:</b>")

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
        if last.lower().startswith("błąd:") or last.lower().startswith("blad:"):
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
    ("rutyny", "Zadania wykonywane wedlug harmonogramu"),
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
MAX_MENU_COMMANDS = 100        # limit Telegrama
MAX_COMMAND_DESCRIPTION = 256  # limit Telegrama

SCAN_WORDS = frozenset({"aktualizuj", "odswiez", "odśwież", "skanuj"})


def parse_command(text: str) -> tuple[str, str]:
    """'/deploy_app@PipeBot na produkcji' -> ('deploy_app', 'na produkcji')."""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    name = parts[0].lstrip("/").split("@", 1)[0].lower()
    return name, parts[1].strip() if len(parts) > 1 else ""


def build_menu(skills: list[dict]) -> list[tuple[str, str]]:
    """Menu '/' Telegrama: komendy wbudowane, potem skille, ktore maja komende."""
    menu = list(BUILTIN_COMMANDS)
    for skill in skills:
        if skill.get("command"):
            description = " ".join((skill.get("description") or "").split()) or "Skill"
            menu.append((skill["command"], description[:MAX_COMMAND_DESCRIPTION]))
    return menu[:MAX_MENU_COMMANDS]


def format_skill_list(skills: list[dict]) -> str:
    """Lista skilli jako HTML Telegrama."""
    if not skills:
        return ("Brak zapisanych skilli. Po wykonaniu wieloetapowej procedury popros: "
                "<i>\"zapisz to jako skill\"</i>.")
    lines = ["<b>Skille</b>"]
    for skill in skills:
        description = html.escape(skill.get("description") or "")
        if skill.get("command"):
            lines.append(f"• /{skill['command']} — {description}")
        else:
            name = html.escape(skill["name"])
            lines.append(f"• <code>{name}</code> — {description} "
                         f"<i>(bez komendy — napisz: uruchom skill {name})</i>")
    example = next((s["command"] for s in skills if s.get("command")), None)
    if example:
        lines.append(f"\nDo komendy mozesz dopisac wskazowki, np. <code>/{example} tylko dla example.com</code>")
    return "\n".join(lines)


def format_help(skills: list[dict]) -> str:
    """Pomoc (/pomoc) jako HTML Telegrama."""
    lines = ["<b>Komendy</b>"]
    lines += [f"/{command} — {html.escape(description)}" for command, description in BUILTIN_COMMANDS]
    with_command = [s for s in skills if s.get("command")]
    if with_command:
        lines.append(f"\n<b>Skille ({len(with_command)})</b> — kazdy ma wlasna komende, pelna lista: /skille")
    lines.append("\nMozesz tez po prostu pisac, np. <i>\"ile mam wolnego miejsca?\"</i>")
    return "\n".join(lines)


def response_data(responses: list[dict]) -> dict:
    """Pole 'data' z odpowiedzi na zadanie {"command": ...}; blad backendu -> RuntimeError."""
    for resp in reversed(responses):
        if "data" in resp:
            return resp["data"]
    error = next((r.get("response") for r in responses if r.get("status") == "error"), "") or "brak danych"
    raise RuntimeError(error)



# ─── Czuwanie, rutyny, listy ────────────────────────────────────────────────

SEVERITY_LABEL = {"critical": "KRYTYCZNY", "warning": "OSTRZEZENIE"}


def format_alert(event: dict) -> str:
    """Alert czuwania jako HTML Telegrama."""
    title = html.escape(str(event.get("title", "")))
    detail = html.escape(str(event.get("detail", "")))
    if event.get("state") == "resolved":
        return f"<b>ROZWIAZANE</b> — {title}"
    label = SEVERITY_LABEL.get(str(event.get("severity")), "ALERT")
    text = f"<b>{label}</b> — {title}\n<i>{detail}</i>" if detail else f"<b>{label}</b> — {title}"
    if event.get("history"):
        text += f"\n<b>Poprzednio:</b> {html.escape(str(event['history']))}"
    return text


def format_routine(event: dict, limit: int = 3000) -> str:
    """Raport rutyny jako HTML Telegrama (tresc od modelu — escapowana, w bloku)."""
    name = html.escape(str(event.get("name", "")))
    status = str(event.get("status", ""))
    report = str(event.get("report", "")).strip()
    if len(report) > limit:
        report = report[:limit] + "\n[...]"
    return f"<b>Rutyna {name}</b> — {html.escape(status)}\n<pre>{html.escape(report)}</pre>"


def format_list(title: str, items: list[str], empty: str) -> str:
    if not items:
        return f"<b>{html.escape(title)}</b>\n{empty}"
    return f"<b>{html.escape(title)}</b>\n" + "\n".join(f"• {html.escape(i)}" for i in items)


def format_alerts(data: dict) -> str:
    if not data.get("enabled", True):
        return "Czuwanie jest wylaczone (WATCH_ENABLED=0 w backend/.env)."
    active = data.get("active") or []
    lines = ["<b>Aktywne alerty</b>"]
    lines += [format_alert(a) for a in active] or ["Brak — wszystko w normie."]
    recent = [e for e in (data.get("recent") or []) if e.get("type") == "alert" and e.get("state") != "new"]
    if recent:
        lines.append("\n<b>Ostatnie zdarzenia</b>")
        lines += [f"{html.escape(str(e.get('at', '')))} — {format_alert(e)}" for e in recent[-5:]]
    return "\n".join(lines)


def format_directory(entries: list[dict]) -> str:
    if not entries:
        return ("<b>DIRECTORY</b>\nMapa katalogow jest pusta. Napisz np. <i>\"znajdz repozytoria na serwerze\"</i> "
                "albo uzyj /server aktualizuj.")
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
    head = f"<i>... i {skipped} wczesniej</i>\n" if skipped else ""
    return "<b>W toku</b>\n" + head + "\n".join(f"<code>{html.escape(l[:200])}</code>" for l in shown)


def format_digest(event: dict) -> str:
    """Poranny raport (zdarzenie albo dane z /raport) jako HTML Telegrama."""
    lines = [f"<b>{html.escape(str(event.get('title', 'Raport')))}</b>"]
    for section in event.get("sections") or []:
        lines.append(f"\n<b>{html.escape(str(section.get('title', '')))}</b>")
        for line in section.get("lines") or []:
            text = html.escape(str(line))
            if text.startswith("[BEZPIECZENSTWO]"):
                text = "<b>[BEZPIECZENSTWO]</b>" + text[len("[BEZPIECZENSTWO]"):]
            lines.append(f"• {text}")
    return "\n".join(lines)


def format_pre(title: str, text: str, limit: int = 3500) -> str:
    """Tekst od backendu (bez LLM) w bloku <pre> — /zmiany, /zdrowie, /koszt."""
    body = (text or "").strip() or "(pusto)"
    if len(body) > limit:
        body = body[:limit] + "\n[...]"
    return f"<b>{html.escape(title)}</b>\n<pre>{html.escape(body)}</pre>"


AUDIT_SEVERITY = {"high": "WYSOKIE", "medium": "SREDNIE", "low": "NISKIE"}


def format_audit(data: dict) -> str:
    """Audyt bezpieczenstwa (/audyt) jako HTML Telegrama."""
    lines = [f"<b>Bezpieczenstwo: {int(data.get('score', 0))}/100 ({html.escape(str(data.get('grade', '?')))})</b>"]
    for index, finding in enumerate(data.get("findings") or [], start=1):
        label = AUDIT_SEVERITY.get(finding.get("severity"), "")
        lines.append(f"\n<b>{index}. [{label}]</b> {html.escape(str(finding.get('title', '')))}")
        if finding.get("detail"):
            lines.append(f"<i>{html.escape(str(finding['detail']))}</i>")
        if finding.get("fix"):
            lines.append("Poprawka: " + html.escape(str(finding["fix"])))
        if finding.get("command"):
            where = " (na hoscie)" if finding.get("host_only") else ""
            lines.append(f"<code>{html.escape(str(finding['command']))}</code>{where}")
    if data.get("passed"):
        lines.append("\n<b>W porzadku:</b> " + html.escape("; ".join(data["passed"])))
    if data.get("findings"):
        lines.append("\nNapisz <i>\"napraw 1\"</i> — przygotuje poprawke do zatwierdzenia.")
    return "\n".join(lines)


def format_investigation(event: dict, limit: int = 3000) -> str:
    """Raport workera, ktory sam zbadal alert z zewnatrz (webhook)."""
    report = str(event.get("report", "")).strip()
    if len(report) > limit:
        report = report[:limit] + "\n[...]"
    return (f"<b>Zbadalem alert</b> — {html.escape(str(event.get('title', '')))}\n"
            f"<pre>{html.escape(report)}</pre>")


def format_approval(event: dict) -> str:
    """Prosba o zgode dla zewnetrznego agenta (MCP) — komenda doslownie, plan bezpiecznika."""
    lines = [f"<b>ZGODA</b> — agent <code>{html.escape(str(event.get('requested_by', '?')))}</code> "
             f"chce wykonac na celu <code>{html.escape(str(event.get('target', 'local')))}</code>:",
             f"<pre>{html.escape(str(event.get('command', '')))}</pre>"]
    if event.get("reason"):
        lines.append(f"<i>Powod: {html.escape(str(event['reason']))}</i>")
    if event.get("plan"):
        lines.append(html.escape(str(event["plan"])))
    lines.append("<i>Zgoda wygasa po 30 minutach.</i>")
    return "\n".join(lines)
