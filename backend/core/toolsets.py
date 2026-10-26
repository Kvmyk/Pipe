"""
Narzedzia na zadanie — model dostaje pelne opisy tylko tych narzedzi, ktorych rozmowa potrzebuje.

Schematy wszystkich narzedzi to ~6 tys. tokenow wysylanych z KAZDYM zapytaniem do modelu, takze z kazdym krokiem
petli agenta. Wiekszosc rozmow uzywa kilku narzedzi: komendy, plikow, Dockera, historii serwera. Dlatego sesja
zaczyna od grupy `core` (z malym `load_tools`), a reszta grup wlacza sie:

  - sama, gdy wiadomosc uzytkownika (albo komenda klienta) pasuje do grupy: "przypomnij jutro" -> schedule,
    "czy serwer jest bezpieczny" -> security, adres URL -> web,
  - gdy model wywola `load_tools` (opis wymienia grupy) albo od razu narzedzie spoza listy — handler i tak je
    obsluzy, a grupa zostaje wlaczona.

Wlaczona grupa zostaje w sesji do konca rozmowy: lista narzedzi zmienia sie rzadko, wiec cache promptu u providera
(ten sam poczatek zapytania) dziala dalej. TOOLS_MODE=all przywraca stare zachowanie (wszystkie narzedzia zawsze).
Bez importu `settings`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable

CORE = "core"
GROUPS: dict[str, tuple[str, ...]] = {
    CORE: ("execute_command", "read_file", "write_file", "change_directory", "git_command", "system_stats",
           "network_info", "server_history", "project", "load_tools"),
    "memory": ("server_md", "directory", "skill_manage", "vibe"),
    "security": ("security_audit", "security_patch", "journal"),
    "schedule": ("reminder", "routine_manage", "cron_manage"),
    "fleet": ("target_manage", "remote_exec", "delegate"),
    "web": ("web_search", "web_fetch", "software_info"),
    "diagram": ("diagram",),
    "update": ("pipe_update",),
}
TOOL_GROUP = {tool: group for group, tools in GROUPS.items() for tool in tools}

# Fragmenty wyrazen (male litery, bez polskich znakow) — wiadomosc pasujaca do grupy wlacza ja od razu.
# Pomylka w strone "wlacz" kosztuje kilkaset tokenow; w strone "nie wlaczaj" — jeden krok modelu (load_tools).
TRIGGERS: dict[str, tuple[str, ...]] = {
    "memory": (r"zapamiet", r"zapisz (to|sobie|jako|w pamieci)", r"notatk", r"pamiec", r"server\.?md", r"skill",
               r"procedur", r"\bdirectory\b", r"styl (rozmowy|odpowiedzi)", r"\bvibe\b", r"odpowiadaj (krocej|dluzej|po )",
               r"mow do mnie", r"remember", r"\bmemory\b", r"save (this|it) as", r"\bnote (that|this|it)",
               r"answer (shorter|longer)", r"talk to me"),
    "security": (r"bezpiecz", r"audyt", r"podatn", r"\bcve\b", r"cve-\d", r"zalataj", r"\blatk", r"latani", r"cofnij",
                 r"przywroc", r"wycofaj", r"dziennik", r"journal", r"\bundo\b", r"revert", r"roll ?back", r"secur",
                 r"vulnerab", r"\bpatch", r"harden", r"fail2ban", r"zapor", r"firewall", r"wlam", r"hack"),
    "schedule": (r"przypomn", r"\bza \d+\s*(s|sek|sekund|min|minut|m|h|godz|godzin|dn|dni|dzien)\b", r"\bjutro\b",
                 r"pojutrze", r"\bdzis(iaj)? o\b", r"\bo (godz\w* )?\d{1,2}(:\d{2})?\b", r"codzienn",
                 r"\bco (godzin|dzien|dnia|tydzien|tygodni|minut|noc|rano|wieczor|poniedzial)", r"kazd\w+ (dnia|rana|ranka|tygodnia|nocy)",
                 r"harmonogram", r"rutyn", r"\bcron", r"zaplanuj", r"napisz do mnie", r"odezwij sie", r"remind",
                 r"tomorrow", r"tonight", r"every (day|hour|week|morning|night|minute|monday)", r"schedul", r"routine",
                 r"\bin \d+\s*(s|sec|seconds?|min|minutes?|h|hours?|days?)\b", r"\bat \d{1,2}(:\d{2})?\s*(am|pm)?\b"),
    "fleet": (r"zdaln", r"inn(y|e|ych|ym) serwer", r"wszystki\w* serwer", r"serwer(y|ach|ow|ami)\b", r"\bcel(e|ow|ach)\b",
              r"\btarget", r"\bworker", r"delegat", r"rownoleg", r"kubernetes", r"klast", r"kubectl", r"\bk8s\b", r"\bhelm\b",
              r"remote", r"fleet", r"cluster", r"other server", r"all server", r"\bservers\b"),
    "web": (r"internet", r"w sieci\b", r"wyszukaj", r"poszukaj", r"google", r"dokumentacj", r"changelog", r"release note",
            r"najnowsz\w* wersj", r"wspieran", r"end.of.life", r"\beol\b", r"https?://", r"stack ?overflow",
            r"\bsearch", r"look (it )?up", r"\bdocs?\b", r"latest version", r"supported", r"\bcve\b"),
    "diagram": (r"diagram", r"schemat", r"\bmap(a|e|y|ie|ka)\b", r"architektur", r"narysuj", r"rysun",
                r"pokaz (jak|co|mi jak)", r"infrastruktur", r"\bdraw", r"architecture", r"\bmap\b", r"flowchart",
                r"mermaid", r"wizualiz", r"visuali"),
    "update": (r"zaktualizuj (sie|pipe)", r"aktualizacj\w* pipe", r"wersj\w* pipe", r"nowsz\w* (wersj\w* )?pipe",
               r"update (yourself|pipe)", r"upgrade pipe", r"pipe version", r"\bpipe_update\b"),
}
_COMPILED = {group: re.compile("|".join(f"(?:{p})" for p in patterns)) for group, patterns in TRIGGERS.items()}
_TOOL_NAMES = re.compile(r"\b(" + "|".join(sorted(TOOL_GROUP, key=len, reverse=True)) + r")\b")
_FOLD = str.maketrans("ąćęłńóśźż", "acelnoszz")


def normalize(text: str) -> str:
    """Male litery bez polskich znakow i akcentow — ten sam zapis dla "zapamiętaj" i "zapamietaj"."""
    lowered = (text or "").lower().translate(_FOLD)
    return "".join(ch for ch in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(ch))


def groups_for_text(text: str) -> set[str]:
    """Grupy, ktorych potrzebuje wiadomosc (slowa kluczowe i nazwy narzedzi wprost)."""
    plain = normalize(text)[:8000]
    found = {group for group, pattern in _COMPILED.items() if pattern.search(plain)}
    found |= {TOOL_GROUP[name] for name in _TOOL_NAMES.findall(plain)}
    found.discard(CORE)
    return found


def activate(session: Any, groups: Iterable[str]) -> list[str]:
    """Wlacza grupy w sesji. Zwraca nowo wlaczone."""
    current = set(getattr(session, "tool_groups", None) or {CORE})
    added = [g for g in groups if g in GROUPS and g not in current]
    if added:
        session.tool_groups = current | set(added)
    return added


def observe(session: Any, text: str) -> list[str]:
    """Nowa wiadomosc w sesji: wlacza pasujace grupy."""
    return activate(session, groups_for_text(text))


def activate_for_tool(session: Any, tool: str) -> list[str]:
    """Model wywolal narzedzie spoza listy — jego grupa zostaje wlaczona na reszte rozmowy."""
    group = TOOL_GROUP.get(tool)
    return activate(session, [group]) if group else []


def allowed(session: Any) -> set[str]:
    names: set[str] = set()
    for group in set(getattr(session, "tool_groups", None) or {CORE}) | {CORE}:
        names.update(GROUPS.get(group, ()))
    return names


def select(tools: list[dict], session: Any | None, *, lazy: bool) -> list[dict]:
    """
    Narzedzia dla sesji. Bez sesji albo bez trybu na zadanie — wszystkie poza load_tools (stare zachowanie).
    Kolejnosc zostaje ta z TOOLS — rowny poczatek zapytania pomaga cache providera.
    """
    if session is None or not lazy:
        return [t for t in tools if t["function"]["name"] != "load_tools"]
    names = allowed(session)
    return [t for t in tools if t["function"]["name"] in names]


def size(tools: list[dict]) -> int:
    """Szacunek tokenow schematow (znaki / 4)."""
    return len(json.dumps(tools, ensure_ascii=False)) // 4


def inactive(session: Any) -> list[str]:
    current = set(getattr(session, "tool_groups", None) or {CORE})
    return [g for g in GROUPS if g not in current and g != CORE]
