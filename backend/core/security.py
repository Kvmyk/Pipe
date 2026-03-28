"""
Security — klasyfikacja komend na trzy poziomy bezpieczeństwa.

Kolejność sprawdzania:
  1. FORBIDDEN  → odmów bezwzględnie
  2. CONFIRM    → poproś o potwierdzenie
  3. SAFE       → wykonaj od razu

Jeśli komenda nie pasuje do żadnej kategorii → domyślnie "confirm"
"""

from __future__ import annotations

import re
from typing import Literal

# ─── Poziom 1: SAFE (wykonaj od razu) ───────────────────────────────────────
SAFE_PREFIXES: list[str] = [
    "systemctl status",
    "systemctl restart",
    "systemctl reload",
    "systemctl start",
    "systemctl stop",
    "journalctl",
    "tail ",
    "tail\t",
    "cat ",
    "cat\t",
    "head ",
    "head\t",
    "grep ",
    "grep\t",
    "df ",
    "df\t",
    "df",
    "free",
    "top",
    "uptime",
    "ps ",
    "ps\t",
    "ps",
    "netstat",
    "ss ",
    "ss\t",
    "ss",
    "ls ",
    "ls\t",
    "ls",
    "find ",
    "find\t",
    "du ",
    "du\t",
    "docker ps",
    "docker logs",
    "docker restart",
    "docker-compose up",
    "docker-compose down",
    "apt update",
    "apt install",
    "apt upgrade",
    "nginx -t",
    "hostname",
    "whoami",
    "id",
    "pwd",
    "date",
    "uname",
]

# ─── Poziom 2: CONFIRM (wymagają potwierdzenia) ──────────────────────────────
CONFIRM_PATTERNS: list[str] = [
    r".*\/etc\/.*",
    r"chmod\s",
    r"chown\s",
    r"ufw\s",
    r"iptables\s",
    r"crontab\s",
    r"passwd\s",
    r"\breboot\b",
    r"\bshutdown\b",
    r"apt\s+purge",
    r"apt\s+autoremove",
    r"mv\s",
    r"cp\s+-r",
    r"rm\s",           # rm bez -rf / jest confirm (bezwzględne rf/ jest forbidden)
    r"kill\s",
    r"pkill\s",
    r"service\s",
    r"systemctl\s+enable",
    r"systemctl\s+disable",
    r"systemctl\s+mask",
    r"docker\s+stop",
    r"docker\s+rm",
    r"docker\s+rmi",
    r"pip\s+install",
    r"pip3\s+install",
]

# ─── Poziom 3: FORBIDDEN (absolutnie zakazane) ──────────────────────────────
FORBIDDEN_PATTERNS: list[str] = [
    r"rm\s+-rf\s+/",
    r"rm\s+--no-preserve-root",
    r"dd\s+if=",
    r"mkfs\.",
    r">\s*/etc/passwd",
    r">\s*/etc/shadow",
    r":\(\)\{",                   # fork bomb
    r"curl.+\|\s*(bash|sh)",
    r"wget.+\|\s*(bash|sh)",
    r"base64.*\|\s*(bash|sh)",
    r"python\s+-c.+exec",
    r">\s*/dev/sd",               # nadpisanie dysku
    r"shred\s",
    r">\s*/boot/",
]

# ─── Skompilowane wyrażenia regularne ────────────────────────────────────────
_compiled_forbidden: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PATTERNS
]
_compiled_confirm: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in CONFIRM_PATTERNS
]


def classify_command(cmd: str) -> Literal["safe", "confirm", "forbidden"]:
    """
    Klasyfikuje komendę shell na jeden z trzech poziomów.

    Args:
        cmd: Komenda do sprawdzenia.

    Returns:
        "forbidden" | "confirm" | "safe"
    """
    cmd_stripped = cmd.strip()

    # 1. Najpierw sprawdź FORBIDDEN
    for pattern in _compiled_forbidden:
        if pattern.search(cmd_stripped):
            return "forbidden"

    # 2. Potem CONFIRM
    for pattern in _compiled_confirm:
        if pattern.search(cmd_stripped):
            return "confirm"

    # 3. Safe prefixes — sprawdź czy komenda zaczyna się od bezpiecznego prefiksu
    cmd_lower = cmd_stripped.lower()
    for prefix in SAFE_PREFIXES:
        if cmd_lower.startswith(prefix.lower()):
            return "safe"

    # 4. Domyślnie — wymagaj potwierdzenia
    return "confirm"


def classify_file_write(path: str) -> Literal["safe", "confirm", "forbidden"]:
    """
    Klasyfikuje operację zapisu pliku.
    Zapis do /etc/passwd i /etc/shadow jest forbidden.
    Każdy inny zapis pliku wymaga potwierdzenia.
    """
    forbidden_paths = ["/etc/passwd", "/etc/shadow", "/boot/", "/dev/"]
    for fp in forbidden_paths:
        if path.startswith(fp):
            return "forbidden"
    return "confirm"
