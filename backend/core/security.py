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

# --- Poziom 1: SAFE (wykonaj od razu) ---
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
    "docker stats",
    "docker inspect",
    "docker images",
    "docker top",
    "docker restart",
    "docker compose ps",
    "docker compose logs",
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
    "nproc",
    "nslookup",
    "dig ",
    "ping ",
    "curl ",
    # Git -- operacje odczytujace
    "git status",
    "git log",
    "git diff",
    "git branch",
    "git remote",
    "git show",
    "git tag",
    "git -C",
    # Procinfo
    "cat /proc/",
]

# --- Poziom 2: CONFIRM (wymagaja potwierdzenia) ---
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
    r"rm\s",
    r"kill\s",
    r"pkill\s",
    r"service\s",
    r"systemctl\s+enable",
    r"systemctl\s+disable",
    r"systemctl\s+mask",
    r"docker\s+stop",
    r"docker\s+rm",
    r"docker\s+rmi",
    r"docker\s+system\s+prune",
    r"pip\s+install",
    r"pip3\s+install",
    # Git -- operacje modyfikujace
    r"git\s+push",
    r"git\s+commit",
    r"git\s+checkout",
    r"git\s+merge",
    r"git\s+rebase",
    r"git\s+reset",
    r"git\s+stash\s+pop",
    r"git\s+stash\s+drop",
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


def validate_workspace_access(path: str, workspace: str = "/hostfs") -> tuple[bool, str]:
    """
    Waliduje, czy ścieżka znajduje się w dozwolonym workspace'u.
    
    Agent ma dostęp TYLKO do /hostfs (zmapowanego na /opt/pipeclaw-workspace na hoście).
    Wszelkie próby dostępu do systemowych katalogów są blokowane.
    
    Args:
        path: Ścieżka do sprawdzenia (absolutna lub względna).
        workspace: Katalog workspace (domyślnie /hostfs).
        
    Returns:
        (is_allowed: bool, reason: str)
    """
    from pathlib import Path
    
    # Ścieżki systemowe, do których dostęp jest zawsze zabroniony
    FORBIDDEN_SYSTEM_PATHS = [
        "/etc/",
        "/boot/",
        "/dev/",
        "/proc/",
        "/sys/",
        "/root/",
        "/var/log/",
        "/var/spool/",
        "/usr/bin/",
        "/usr/sbin/",
        "/bin/",
        "/sbin/",
        "/lib/",
        "/lib64/",
        "/opt/docker",  # Protekcja Docker daemon
    ]
    
    # Normalizuj ścieżkę
    try:
        resolved_path = str(Path(path).resolve())
    except (ValueError, OSError):
        return False, f"Nieprawidłowa ścieżka: {path}"
    
    # Sprawdź czy ścieżka jest w workspace'ie
    try:
        workspace_path = Path(workspace).resolve()
        abs_path = Path(resolved_path).resolve()
        
        # Upewnij się, że ścieżka jest wewnątrz workspace'u
        abs_path.relative_to(workspace_path)
    except ValueError:
        return False, f"Dostęp poza workspace ({workspace}) jest zabroniony dla {path}"
    
    # Sprawdź czy ścieżka nie trafia w systemowe katalogi
    for forbidden in FORBIDDEN_SYSTEM_PATHS:
        if resolved_path.startswith(forbidden):
            return False, f"Dostęp do {forbidden} jest zabroniony"
    
    return True, "OK"
