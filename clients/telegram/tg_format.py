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
            if "[POTWIERDZ]" in text:
                text = confirm_text(text)
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


_CONFIRM_LEAD = re.compile(r"^(?P<subject>.*?)\s+(?:wymaga potwierdzenia|requires confirmation)\s*[:.]?\s*(?P<rest>.*)$",
                           re.IGNORECASE)
_FUSE_HEAD = re.compile(r"^(Bezpiecznik|Safety fuse):\s*$", re.MULTILINE)


def confirm_text(text: str) -> str:
    """
    "[POTWIERDZ] Operacja wymaga potwierdzenia: `cmd`" -> "🔐 <b>Potrzebuje Twojej zgody</b>" + komenda w osobnej linii.
    Temat ("Zapis pliku", "Nowa rutyna") zostaje, ogolnikowe "Operacja" znika — naglowek juz prosi o zgode.
    Komenda zostaje w backtickach: to_telegram_html pokazuje ja doslownie.
    """
    body = text.replace("[POTWIERDZ]", "", 1).strip()
    first, _, rest = body.partition("\n")
    match = _CONFIRM_LEAD.match(first)
    lines = [tr("🔐 <b>Potrzebuje Twojej zgody</b>", "🔐 <b>I need your approval</b>")]
    if match:
        subject, tail = match["subject"], match["rest"].lstrip("—–- ")
        if subject.lower() not in ("operacja", "operation", "komenda", "command"):
            lines.append(subject + (":" if tail else ""))
        if tail:
            lines.append(tail)
    else:
        lines.append(first)
    in_fuse = False
    for line in rest.strip("\n").split("\n") if rest.strip() else []:
        head = _FUSE_HEAD.match(line.strip())
        if head:
            in_fuse = True
            lines.append(f"\n<b>{head.group(1)}</b>")
        elif in_fuse and line.startswith("- "):
            lines.append("• " + line[2:])            # tylko wiersze planu — linie diffa w podgladzie zostaja nietkniete
        else:
            in_fuse = False
            lines.append(line)
    return "\n".join(lines)


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
    ("status", "Stan serwera — od razu, bez AI"),
    ("nowosci", "Co Pipe napisal sam: alerty, podpowiedzi, raporty"),
    ("projekty", "Projekty w warsztacie: stan, logi, zatrzymanie"),
    ("sprzataj", "Zwolnij miejsce na dysku (bez AI)"),
    ("powiadomienia", "Kiedy Pipe ma powiadamiac (poziom, cisza nocna)"),
    ("pobierz", "Skille i pluginy z repozytorium (/pobierz skill wlasciciel/repo)"),
    ("mcp", "Serwery MCP, z ktorych korzysta Pipe (/mcp dodaj, /mcp usun)"),
    ("aktualizuj", "Zaktualizuj Pipe (/aktualizuj sprawdz - tylko wersja)"),
    ("raport", "Poranny raport: stan, zmiany, certyfikaty, backupy"),
    ("zmiany", "Co sie zmienilo na serwerze (/zmiany 3d)"),
    ("wykres", "Wykres load/ram/dysk (/wykres ram 7d)"),
    ("zdrowie", "Certyfikaty, strony, DNS i backupy"),
    ("audyt", "Audyt bezpieczenstwa z ocena i poprawkami"),
    ("podatnosci", "Znane podatnosci z opisami"),
    ("zalataj", "Zalataj od razu (/zalataj obrazy, /zalataj serwer, /zalataj aplikacje, /zalataj serwery)"),
    ("mapa", "Diagram infrastruktury serwera (obraz)"),
    ("server", "Pokaz SERVER.md (/server aktualizuj - zbadaj serwer ponownie)"),
    ("katalogi", "Mapa repozytoriow i katalogow (DIRECTORY)"),
    ("skille", "Lista zapisanych skilli"),
    ("alerty", "Aktywne alerty czuwania"),
    ("incydenty", "Pamiec incydentow: przyczyny i co pomoglo"),
    ("rutyny", "Zadania wykonywane wedlug harmonogramu"),
    ("przypomnienia", "Jednorazowe przypomnienia (/przypomnienia anuluj <id>)"),
    ("cele", "Zdalne serwery, kontenery i klastry"),
    ("nowa", "Nowa rozmowa od czystej karty (wiedza o serwerze zostaje)"),
    ("vibe", "Co wiem o Twoim stylu rozmowy (/vibe reset - wyczysc)"),
    ("cofnij", "Cofnij ostatnia zmiane (/cofnij <id> - wybrana)"),
    ("dziennik", "Dziennik zatwierdzonych zmian"),
    ("zgody", "Operacje agentow MCP czekajace na zgode"),
    ("koszt", "Zuzycie tokenow i koszt LLM"),
    ("historia", "Ostatnie wpisy z audit logu"),
    ("providerzy", "Providerzy LLM i model (/providerzy groq, /providerzy model)"),
    ("yolo", "Zmiany bez pytania o TAK (/yolo on, /yolo off)"),
    ("jezyk", "Jezyk Pipe (/jezyk en albo /jezyk pl)"),
    ("pomoc", "Lista komend"),
)
BUILTIN_COMMANDS_EN: tuple[tuple[str, str], ...] = (
    ("status", "Server status — instant, no AI"),
    ("news", "What Pipe wrote on its own: alerts, suggestions, reports"),
    ("projects", "Workshop projects: state, logs, stopping"),
    ("cleanup", "Free up disk space (no AI)"),
    ("notifications", "When Pipe should notify you (level, quiet hours)"),
    ("download", "Skills and plugins from a repository (/download skill owner/repo)"),
    ("mcp", "MCP servers Pipe uses (/mcp add, /mcp remove)"),
    ("update", "Update Pipe (/update check - version only)"),
    ("report", "Morning report: health, changes, certificates, backups"),
    ("changes", "What changed on the server (/changes 3d)"),
    ("chart", "Chart of load/ram/disk (/chart ram 7d)"),
    ("health", "Certificates, sites, DNS and backups"),
    ("audit", "Security audit with a score and fixes"),
    ("vulns", "Known vulnerabilities with descriptions"),
    ("patch", "Patch right away (/patch images, /patch server, /patch apps, /patch servers)"),
    ("map", "Server infrastructure diagram (image)"),
    ("server", "Show SERVER.md (/server update - explore the server again)"),
    ("directory", "Map of repositories and directories (DIRECTORY)"),
    ("skills", "List of saved skills"),
    ("alerts", "Active watcher alerts"),
    ("incidents", "Incident memory: causes and what helped"),
    ("routines", "Tasks run on a schedule"),
    ("reminders", "One-off reminders (/reminders cancel <id>)"),
    ("targets", "Remote servers, containers and clusters"),
    ("new", "New conversation from a clean slate (server knowledge stays)"),
    ("vibe", "What I know about your conversation style (/vibe reset - clear)"),
    ("undo", "Undo the last change (/undo <id> - a chosen one)"),
    ("journal", "Journal of approved changes"),
    ("approvals", "MCP agent operations waiting for approval"),
    ("cost", "Token usage and LLM cost"),
    ("history", "Latest audit-log entries"),
    ("providers", "LLM providers and model (/providers groq, /providers model)"),
    ("yolo", "Changes without asking for YES (/yolo on, /yolo off)"),
    ("language", "Pipe's language (/language pl or /language en)"),
    ("help", "List of commands"),
)
# Angielskie nazwy komend -> polskie (handlery sa rejestrowane pod obiema nazwami w obu jezykach).
COMMAND_ALIASES: dict[str, str] = {
    "report": "raport", "changes": "zmiany", "chart": "wykres", "health": "zdrowie", "audit": "audyt",
    "map": "mapa", "directory": "katalogi", "skills": "skille", "alerts": "alerty", "routines": "rutyny",
    "targets": "cele", "undo": "cofnij", "journal": "dziennik", "approvals": "zgody", "cost": "koszt",
    "history": "historia", "help": "pomoc", "incidents": "incydenty", "reminders": "przypomnienia",
    "update": "aktualizuj", "language": "jezyk", "providers": "providerzy", "patch": "zalataj", "vulns": "podatnosci",
    "new": "nowa", "news": "nowosci", "inbox": "nowosci", "projects": "projekty", "notifications": "powiadomienia",
    "cleanup": "sprzataj", "download": "pobierz",
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


# Pomoc i menu "/" w grupach wedlug zadan (polskie nazwy; angielskie przez COMMAND_ALIASES). Komenda spoza grup
# trafia do "Inne", wiec nowa komenda nie zniknie z pomocy.
HELP_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Na co dzien", "Every day", ("status", "nowosci", "projekty", "raport", "zmiany", "mapa", "wykres")),
    ("Bezpieczenstwo i zmiany", "Security and changes",
     ("audyt", "podatnosci", "zalataj", "sprzataj", "dziennik", "cofnij", "zgody")),
    ("Pamiec i automatyzacja", "Memory and automation",
     ("server", "skille", "pobierz", "rutyny", "przypomnienia", "incydenty", "katalogi", "cele", "vibe")),
    ("Ustawienia", "Settings", ("powiadomienia", "mcp", "nowa", "providerzy", "jezyk", "yolo", "koszt", "aktualizuj",
                                "pomoc")),
)
# Dzialaja, ale nie ma ich w /pomoc ani w menu — to samo daja /status i /nowosci (alerty, strony, certyfikaty)
# oraz /dziennik (zmiany).
HIDDEN_COMMANDS: frozenset[str] = frozenset({"alerty", "zdrowie", "historia"})

# Menu "/" Telegrama: tylko to, czego uzywa sie co dzien (reszta dziala, jest w /pomoc). Mniej znaczy czytelniej.
MENU_COMMANDS: tuple[str, ...] = ("status", "nowosci", "projekty", "raport", "audyt", "zalataj", "cofnij", "sprzataj",
                                  "powiadomienia", "aktualizuj", "nowa", "pomoc")

# Klawiatura pod polem wiadomosci — cztery najczestsze sprawy jednym dotknieciem, bez znajomosci komend.
KEYBOARD: tuple[tuple[tuple[str, str], ...], ...] = (
    (("📊 Stan serwera", "status"), ("📬 Co nowego", "nowosci")),
    (("🧩 Projekty", "projekty"), ("❓ Pomoc", "pomoc")),
)
KEYBOARD_EN: tuple[tuple[tuple[str, str], ...], ...] = (
    (("📊 Server status", "status"), ("📬 What's new", "nowosci")),
    (("🧩 Projects", "projekty"), ("❓ Help", "pomoc")),
)


def keyboard_rows() -> list[list[str]]:
    """Etykiety przyciskow klawiatury w jezyku bota."""
    return [[label for label, _ in row] for row in tr(KEYBOARD, KEYBOARD_EN)]


def keyboard_command(text: str) -> str:
    """Tekst przycisku klawiatury (w dowolnym jezyku) -> komenda; inny tekst -> ''."""
    for layout in (KEYBOARD, KEYBOARD_EN):
        for row in layout:
            for label, command in row:
                if text.strip() == label:
                    return command
    return ""


def _grouped_commands() -> list[tuple[str, list[tuple[str, str]]]]:
    """[(tytul grupy, [(komenda w jezyku bota, opis)])] — kolejnosc z HELP_GROUPS, reszta w "Inne"."""
    by_polish = {COMMAND_ALIASES.get(name, name): (name, desc) for name, desc in builtin_commands()}
    groups, used = [], set()
    for title_pl, title_en, names in HELP_GROUPS:
        items = [by_polish[n] for n in names if n in by_polish]
        used.update(n for n in names if n in by_polish)
        if items:
            groups.append((tr(title_pl, title_en), items))
    rest = [item for key, item in by_polish.items() if key not in used and key not in HIDDEN_COMMANDS]
    if rest:
        groups.append((tr("Inne", "Other"), rest))
    return groups


def build_menu(skills: list[dict]) -> list[tuple[str, str]]:
    """Menu '/' Telegrama: najczestsze komendy (MENU_COMMANDS), potem skille z komenda. Pozostale — /pomoc."""
    by_polish = {COMMAND_ALIASES.get(name, name): (name, desc) for name, desc in builtin_commands()}
    menu = [by_polish[name] for name in MENU_COMMANDS if name in by_polish]
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
    lines = [tr("Nie musisz znac komend — mozesz po prostu napisac, co mam zrobic.",
                "You don't need the commands — you can just write what I should do.")]
    for title, items in _grouped_commands():
        lines.append(f"\n<b>{html.escape(title)}</b>")
        lines += [f"/{command} — {html.escape(description)}" for command, description in items]
    with_command = [s for s in skills if s.get("command")]
    if with_command:
        lines.append(tr(f"\n<b>Skille ({len(with_command)})</b> — kazdy ma wlasna komende, pelna lista: /skille",
                        f"\n<b>Skills ({len(with_command)})</b> — each has its own command, full list: /skills"))
    lines.append(tr("\nMozesz tez nagrac glosowke albo wyslac zrzut ekranu, log czy plik konfiguracyjny "
                    "(podpis to pytanie).",
                    "\nYou can also record a voice note or send a screenshot, a log or a config file "
                    "(the caption is the question)."))
    return "\n".join(lines)


def format_providers(data: dict, *, admin: bool) -> str:
    """
    /providerzy jako HTML Telegrama: aktywny, gotowi (z kluczem) i reszta. Kluczy tu nie ma — backend ich
    nie wysyla — i nie przyjmujemy ich przez czat (zostalyby w historii Telegrama): dodaje sie je w pipe web albo CLI.
    """
    active = data.get("active") or {}
    lines = [tr(f"<b>Providerzy LLM</b> — teraz: <b>{html.escape(active.get('name', '?'))}</b> · "
                f"<code>{html.escape(active.get('model', ''))}</code>",
                f"<b>LLM providers</b> — now: <b>{html.escape(active.get('name', '?'))}</b> · "
                f"<code>{html.escape(active.get('model', ''))}</code>"), ""]
    for provider in data.get("providers", []):
        mark = "✅" if provider.get("active") else "🟢" if provider.get("ready") else "⚪"
        model = provider.get("model") or provider.get("default_model") or ""
        lines.append(f"{mark} <b>{html.escape(provider.get('name', ''))}</b> (<code>{html.escape(provider.get('id', ''))}</code>)"
                     + (f" · <code>{html.escape(model)}</code>" if provider.get("ready") and model else "")
                     + ("" if provider.get("ready") else tr(" — brak klucza", " — no key")))
    if admin:
        lines.append(tr("\nWybierz providera przyciskiem (aktywny — zeby zmienic model) albo: <code>/providerzy groq</code>, "
                        "z modelem po fragmencie nazwy: <code>/providerzy groq llama</code>, sam model: <code>/providerzy model</code>.\n"
                        "<i>Klucze API dodasz w pipe web albo w CLI (/providerzy) — nie wysylaj ich przez czat.</i>",
                        "\nPick a provider with a button (the active one — to change the model) or: <code>/providers groq</code>, "
                        "with a model by part of its name: <code>/providers groq llama</code>, just the model: <code>/providers model</code>.\n"
                        "<i>Add API keys in pipe web or the CLI (/providers) — do not send them through the chat.</i>"))
    else:
        lines.append(tr("\n<i>Providera moze zmieniac tylko administrator.</i>",
                        "\n<i>Only an administrator can change the provider.</i>"))
    return "\n".join(lines)


def provider_choices(data: dict) -> list[tuple[str, str]]:
    """
    Przyciski providerow: (etykieta, id) gotowych (z kluczem). Aktywny tez — jego przycisk zmienia tylko model.
    Po kliknieciu bot pokazuje modele providera do wyboru.
    """
    return [(("✓ " if p.get("active") else "") + str(p.get("name", "")), str(p.get("id", "")))
            for p in data.get("providers", []) if p.get("ready") and p.get("id")]


MODELS_PER_PAGE = 8
MAX_MODEL_LABEL = 60


def match_models(models: list[str], query: str) -> list[str]:
    """Modele pasujace do fragmentu nazwy (bez fragmentu — wszystkie)."""
    query = (query or "").strip().lower()
    return [m for m in models if query in m.lower()] if query else list(models)


def model_page(models: list[str], current: str, page: int | None = None) -> tuple[list[tuple[str, int]], int, int]:
    """
    Jedna strona wyboru modelu: ([(etykieta, indeks w `models`)], strona, liczba stron). Bez `page` —
    strona z obecnym modelem. Indeks (a nie nazwa) idzie do callback_data, ktore Telegram ogranicza do 64 bajtow.
    """
    pages = max(1, -(-len(models) // MODELS_PER_PAGE))
    if page is None:
        page = models.index(current) // MODELS_PER_PAGE if current in models else 0
    page = min(max(page, 0), pages - 1)
    start = page * MODELS_PER_PAGE
    buttons = []
    for index, name in enumerate(models[start:start + MODELS_PER_PAGE], start):
        label = ("✓ " if name == current else "") + name
        buttons.append((label if len(label) <= MAX_MODEL_LABEL else label[:MAX_MODEL_LABEL - 1] + "…", index))
    return buttons, page, pages


def format_model_picker(name: str, current: str, total: int, shown: int, query: str = "") -> str:
    """Naglowek wiadomosci z wyborem modelu (HTML Telegrama)."""
    head = tr(f"<b>Model: {html.escape(name)}</b>", f"<b>Model: {html.escape(name)}</b>")
    if query:
        head += tr(f" — pasuje do <code>{html.escape(query)}</code>: {shown} z {total}",
                   f" — matching <code>{html.escape(query)}</code>: {shown} of {total}")
    else:
        head += tr(f" — {total} do wyboru", f" — {total} to choose from")
    if current:
        head += tr(f"\nObecny: <code>{html.escape(current)}</code>", f"\nCurrent: <code>{html.escape(current)}</code>")
    return head + tr("\n<i>Dluga lista? Zawez: /providerzy &lt;id&gt; &lt;fragment nazwy&gt;</i>",
                     "\n<i>Long list? Narrow it: /providers &lt;id&gt; &lt;part of the name&gt;</i>")


def response_data(responses: list[dict]) -> dict:
    """Pole 'data' z odpowiedzi na zadanie {"command": ...}; blad backendu -> RuntimeError."""
    for resp in reversed(responses):
        if "data" in resp:
            return resp["data"]
    error = next((r.get("response") for r in responses if r.get("status") == "error"), "") or tr("brak danych", "no data")
    raise RuntimeError(error)



# ─── Czuwanie, rutyny, listy ────────────────────────────────────────────────

SEVERITY_LABEL = {"critical": "🔴 KRYTYCZNY", "warning": "🟠 OSTRZEZENIE", "info": "🔵 INFO"}
SEVERITY_LABEL_EN = {"critical": "🔴 CRITICAL", "warning": "🟠 WARNING", "info": "🔵 INFO"}


def format_alert(event: dict) -> str:
    """
    Alert czuwania jako HTML Telegrama. `report` (Markdown, np. podatnosci z opisami) zastepuje krotkie
    `detail`. Zwykly tekst, bez kursywy — dluzszy tekst pochylony czyta sie zle.
    """
    title = html.escape(str(event.get("title", "")))
    detail = html.escape(str(event.get("detail", "")))
    if event.get("state") == "resolved":
        return tr(f"✅ <b>ROZWIAZANE</b> — {title}", f"✅ <b>RESOLVED</b> — {title}")
    label = tr(SEVERITY_LABEL, SEVERITY_LABEL_EN).get(str(event.get("severity")), "ALERT")
    mark, _, word = label.rpartition(" ")
    text = f"{mark} <b>{word}</b> — {title}".lstrip()
    if event.get("report"):
        text += "\n\n" + markdown_html(str(event["report"]))
    elif detail:
        text += f"\n{detail}"
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


def markdown_html(text: str) -> str:
    """
    Markdown z backendu (raport podatnosci, podglad latki, tresc alertu) jako HTML Telegrama: listy z kropkami,
    naglowki i **pogrubienia** pogrubione, tagi HTML z tekstu pokazane doslownie (tresc moze pochodzic z OSV).
    """
    return to_telegram_html(_outside_code(str(text or ""), lambda part: _LIST_DASH.sub(r"\1• ", part)), allow_tags=False)


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
    # pusta linia miedzy alertami — alert o podatnosciach ma kilka akapitow
    lines += ["\n".join("\n" + format_alert(a) for a in active)] if active else [tr("Brak — wszystko w normie.", "None — everything is normal.")]
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


def progress_text(lines: list[str], limit: int = 12, done: bool = False) -> str:
    """
    Tresc wiadomosci-statusu aktualizowanej na biezaco. Po zakonczeniu (`done`) ta sama wiadomosc zamienia sie
    w jedna linie "Wykonane kroki: N" z lista w zwinietym cytacie — zamiast "W toku" wiszacego w czacie na zawsze.
    """
    if done:
        steps = "\n".join("· " + html.escape(l[:160]) for l in lines[-30:])
        return tr(f"☑️ <b>Wykonane kroki: {len(lines)}</b>", f"☑️ <b>Steps taken: {len(lines)}</b>") \
            + f"\n<blockquote expandable>{steps}</blockquote>"
    shown = lines[-limit:]
    skipped = len(lines) - len(shown)
    head = tr(f"<i>... i {skipped} wczesniej</i>\n", f"<i>... and {skipped} earlier</i>\n") if skipped else ""
    return tr("⏳ <b>W toku</b>\n", "⏳ <b>In progress</b>\n") + head + "\n".join(f"<code>{html.escape(l[:200])}</code>" for l in shown)


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
            lines.append(html.escape(str(finding["detail"])))
        if finding.get("fix"):
            lines.append(tr("Poprawka: ", "Fix: ") + html.escape(str(finding["fix"])))
        if finding.get("patch"):
            # latke podatnosci sklada i instaluje /zalataj — komenda na 200 pakietow tylko zaslaniala audyt
            words = {"urgent": tr(" pilne", " urgent"), "images": tr(" obrazy", " images"),
                     "system": tr(" serwer", " server"), "servers": tr(" serwery", " servers")}
            lines.append(tr("Napraw: /zalataj", "Fix it: /patch") + words.get(str(finding["patch"]), ""))
        elif finding.get("command"):
            where = tr(" (na hoscie)", " (on the host)") if finding.get("host_only") else ""
            lines.append(f"<code>{html.escape(str(finding['command']))}</code>{where}")
    if data.get("passed"):
        lines.append(tr("\n<b>W porzadku:</b> ", "\n<b>OK:</b> ") + html.escape("; ".join(data["passed"])))
    if data.get("unknown"):
        # np. zdalny serwer bez polaczenia, nieobslugiwany system — bez tego cisza wygladala jak "wszystko dobrze"
        lines.append(tr("\n<b>Nie sprawdzono:</b> ", "\n<b>Not checked:</b> ") + html.escape("; ".join(data["unknown"])))
    if data.get("findings"):
        lines.append(tr("\nNapisz <i>\"napraw 1\"</i> — przygotuje poprawke do zatwierdzenia.",
                        "\nWrite <i>\"fix 1\"</i> — I will prepare the fix for your approval."))
    return "\n".join(lines)


def format_audit_vulns(data: dict) -> str:
    """Raport podatnosci pod audytem — osobna, jedna wiadomosc: kazdy pakiet z opisem, co grozi."""
    report = str(data.get("vulns") or "").strip()
    return tr("<b>Podatnosci</b>\n\n", "<b>Vulnerabilities</b>\n\n") + markdown_html(report) if report else ""


def format_investigation(event: dict, limit: int = 3000) -> str:
    """Raport workera, ktory sam zbadal alert — z zewnatrz (webhook) albo czuwania (Pipe pisze pierwszy)."""
    report = str(event.get("report", "")).strip()
    if len(report) > limit:
        report = report[:limit] + "\n[...]"
    head = tr("🔎 <b>Ustalilem, co sie stalo</b>", "🔎 <b>I found out what happened</b>") if event.get("auto") \
        else tr("<b>Zbadalem alert</b>", "<b>I investigated the alert</b>")
    return head + f" — {html.escape(str(event.get('title', '')))}\n\n{report_html(report, limit)}"


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


# ─── Pipe pisze pierwszy: stan, skrzynka, projekty, powiadomienia ───────────

STATUS_MARK = {"ok": "🟢", "warning": "🟠", "critical": "🔴"}
LEVEL_MARK = {"ok": "", "warn": " ⚠️", "crit": " 🔴"}
SEVERITY_MARK = {"critical": "🔴", "warning": "🟠", "ok": "✅", "info": "💡"}
TYPE_MARK = {"digest": "☀️", "welcome": "👋", "investigation": "🔎", "routine": "🕒", "notice": "🌙"}


def _bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(float(pct) * width / 100)))
    return "▰" * filled + "▱" * (width - filled)


def format_overview(data: dict) -> str:
    """Stan serwera bez LLM (/status): naglowek zwyklymi slowami, paski procesora, pamieci i dysku."""
    mark = STATUS_MARK.get(str(data.get("status")), "🟢")
    host = html.escape(str(data.get("hostname") or ""))
    lines = [f"{mark} <b>{html.escape(str(data.get('headline', '')))}</b>" + (f"\n<i>{host}</i>" if host else ""), ""]
    for gauge in data.get("gauges") or []:
        label = html.escape(str(gauge.get("label", "")))
        lines.append(f"<code>{_bar(gauge.get('pct', 0))}</code> <b>{int(gauge.get('pct', 0))}%</b> {label}"
                     f"{LEVEL_MARK.get(str(gauge.get('level')), '')}\n      <i>{html.escape(str(gauge.get('value', '')))}</i>")
    if data.get("facts"):
        lines.append("\n" + html.escape(" · ".join(data["facts"])))
    projects = data.get("projects") or []
    if projects:
        running = sum(1 for p in projects if p.get("status") == "running")
        lines.append(tr(f"Projekty w warsztacie: {len(projects)} (dziala: {running}) — /projekty",
                        f"Workshop projects: {len(projects)} (running: {running}) — /projects"))
    if data.get("unread"):
        lines.append(tr(f"📬 Nowe wiadomosci od Pipe: <b>{int(data['unread'])}</b> — /nowosci",
                        f"📬 New messages from Pipe: <b>{int(data['unread'])}</b> — /news"))
    return "\n".join(lines)


def format_feed_item(item: dict, limit: int = 2500) -> str:
    """Wiadomosc ze skrzynki (podpowiedz, ustalenia, alert, raport) jako HTML Telegrama."""
    mark = TYPE_MARK.get(str(item.get("type")), SEVERITY_MARK.get(str(item.get("severity")), "💡"))
    title = html.escape(str(item.get("title", "")))
    text = str(item.get("text", "") or "").strip()
    if len(text) > limit:
        text = text[:limit] + "\n[...]"
    body = f"{mark} <b>{title}</b>"
    if text:
        body += "\n\n" + (report_html(text, limit) if item.get("type") == "investigation" else markdown_html(text))
    if item.get("done"):
        body += "\n\n✓ " + html.escape(str(item["done"]))
    return body


def feed_buttons(item: dict, admin: bool) -> list[tuple[str, str]]:
    """Przyciski wiadomosci: (etykieta, callback_data `fa:<id>:<indeks>`) — akcje zbudowal backend."""
    if item.get("done") or not item.get("feed_id", item.get("id")):
        return []
    feed_id = str(item.get("feed_id") or item.get("id"))
    buttons = []
    for index, action in enumerate(item.get("actions") or []):
        if action.get("admin") and not admin:
            continue
        buttons.append((str(action.get("label", "?"))[:40], f"fa:{feed_id}:{action.get('index', index)}"))
    return buttons


def format_feed(data: dict, limit: int = 8) -> str:
    """Skrzynka (/nowosci): ostatnie wiadomosci od Pipe, nowe oznaczone."""
    items = (data.get("items") or [])[:limit]
    if not items:
        return tr("📬 <b>Skrzynka jest pusta</b>\nCisza — nic nie wymaga Twojej uwagi. Napisze sam, gdy cos zauwaze.",
                  "📬 <b>The inbox is empty</b>\nQuiet — nothing needs your attention. I will write when I notice something.")
    lines = [tr(f"📬 <b>Od Pipe</b> — nowe: {int(data.get('unread', 0))}", f"📬 <b>From Pipe</b> — new: {int(data.get('unread', 0))}")]
    for item in items:
        mark = TYPE_MARK.get(str(item.get("type")), SEVERITY_MARK.get(str(item.get("severity")), "💡"))
        new = " 🆕" if item.get("unread") else ""
        done = f" <i>({html.escape(str(item['done']))})</i>" if item.get("done") else ""
        lines.append(f"{mark} {html.escape(str(item.get('at', ''))[5:])} — {html.escape(str(item.get('title', '')))}{new}{done}")
    return "\n".join(lines)


def format_projects(data: dict) -> str:
    """Warsztat (/projekty): projekty ze stanem i adresem."""
    projects = data.get("projects") or []
    workspace = html.escape(str(data.get("workspace", "")))
    if not projects:
        return tr(f"🧩 <b>Warsztat</b> <code>{workspace}</code>\nNie ma jeszcze projektow. Napisz np. <i>\"sklonuj "
                  "https://github.com/... i uruchom\"</i> — Pipe sklonuje repozytorium, sprawdzi, co to jest, i uruchomi je "
                  "w izolowanym kontenerze.",
                  f"🧩 <b>Workshop</b> <code>{workspace}</code>\nNo projects yet. Write e.g. <i>\"clone https://github.com/... "
                  "and run it\"</i> — Pipe clones the repository, checks what it is and starts it in an isolated container.")
    lines = [tr(f"🧩 <b>Warsztat</b> <code>{workspace}</code>", f"🧩 <b>Workshop</b> <code>{workspace}</code>")]
    for project in projects:
        status = str(project.get("status", "none"))
        mark = {"running": "🟢", "partial": "🟠", "stopped": "🔴"}.get(status, "⚪")
        word = {"running": tr("dziala", "running"), "partial": tr("czesciowo", "partly"), "stopped": tr("zatrzymany", "stopped")}.get(
            status, tr("nieuruchomiony", "not started"))
        url = (project.get("run") or {}).get("url", "") if status in ("running", "partial") else ""
        stack = ", ".join(project.get("stack") or [])
        lines.append(f"{mark} <b>{html.escape(str(project.get('name', '')))}</b> — {word}"
                     + (f" · <code>{html.escape(url)}</code>" if url else "") + (f" <i>[{html.escape(stack)}]</i>" if stack else ""))
    return "\n".join(lines)


def project_buttons(project: dict, admin: bool) -> list[tuple[str, str]]:
    """Przyciski projektu: logi; administrator — zatrzymaj albo wznow (callback `pj:<op>:<nazwa>`)."""
    name = str(project.get("name", ""))[:40]
    buttons = [(tr("📜 Logi", "📜 Logs"), f"pj:logs:{name}")]
    if admin and project.get("status") == "running":
        buttons.append((tr("⏹ Zatrzymaj", "⏹ Stop"), f"pj:stop:{name}"))
    elif admin and project.get("status") in ("stopped", "partial"):
        buttons.append((tr("▶ Wznow", "▶ Resume"), f"pj:start:{name}"))
    return buttons


def format_prefs(data: dict) -> str:
    """Ustawienia powiadomien (/powiadomienia)."""
    level = {"all": tr("wszystko", "everything"), "important": tr("wazne", "important"),
             "critical": tr("tylko krytyczne", "critical only")}.get(str(data.get("level")), str(data.get("level")))
    quiet = html.escape(str(data.get("quiet") or "")) or tr("wylaczona", "off")
    lines = [tr("🔔 <b>Powiadomienia</b>", "🔔 <b>Notifications</b>"),
             tr(f"Kiedy: <b>{level}</b>", f"When: <b>{level}</b>"),
             tr(f"Cisza nocna: <b>{quiet}</b> (wtedy tylko sprawy krytyczne, reszta rano)",
                f"Quiet hours: <b>{quiet}</b> (critical matters only, the rest in the morning)"),
             tr(f"Podpowiedzi dziennie: <b>{int(data.get('per_day', 0))}</b>",
                f"Suggestions per day: <b>{int(data.get('per_day', 0))}</b>")]
    if data.get("muted"):
        lines.append(tr("Wyciszone: ", "Muted: ") + html.escape(", ".join(data["muted"])))
    lines.append(tr("<i>Wszystko i tak trafia do skrzynki — /nowosci.</i>", "<i>Everything lands in the inbox anyway — /news.</i>"))
    return "\n".join(lines)


def prefs_buttons(data: dict) -> list[list[tuple[str, str]]]:
    """Wiersze przyciskow ustawien (callback `pf:<pole>:<wartosc>`); wybrane oznaczone kropka."""
    def mark(on: bool, label: str) -> str:
        return ("● " if on else "") + label

    level, quiet, per_day = data.get("level"), data.get("quiet") or "", data.get("per_day")
    return [
        [(mark(level == value, label), f"pf:level:{value}") for value, label in
         (("all", tr("Wszystko", "Everything")), ("important", tr("Wazne", "Important")), ("critical", tr("Krytyczne", "Critical")))],
        [(mark(quiet == value, label), f"pf:quiet:{value or 'off'}") for value, label in
         (("", tr("Bez ciszy", "No quiet")), ("22:00-07:00", "🌙 22–7"), ("23:00-08:00", "🌙 23–8"))],
        [(mark(per_day == value, tr(f"{value}/dzien", f"{value}/day")), f"pf:per_day:{value}") for value in (0, 1, 3, 5)],
    ]
