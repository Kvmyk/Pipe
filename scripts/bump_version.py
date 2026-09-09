#!/usr/bin/env python3
"""
Podbija wersje PipeClaw w calym repozytorium.

Zrodlem prawdy jest plik VERSION w korzeniu repo. Skrypt czyta z niego
aktualna wersje, wylicza nowa i podmienia ja we wszystkich miejscach,
w ktorych wersja jest wpisana na sztywno.

Uzycie:
    python scripts/bump_version.py patch     # 0.3.0 -> 0.3.1
    python scripts/bump_version.py minor     # 0.3.0 -> 0.4.0
    python scripts/bump_version.py major     # 0.3.0 -> 1.0.0
    python scripts/bump_version.py 1.2.3     # wersja wprost
    python scripts/bump_version.py --current # tylko wypisz aktualna wersje
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"

# Pliki, w ktorych wersja wystepuje w tekscie. Kazdy wzorzec musi zawierac
# grupe nazwana (?P<ver>...) obejmujaca sam numer wersji.
PATTERNS: list[tuple[str, str]] = [
    ("README.md", r"\*\*v(?P<ver>\d+\.\d+(?:\.\d+)?)\*\*"),
    ("AGENTS.md", r"\*\*v(?P<ver>\d+\.\d+(?:\.\d+)?) status\*\*"),
    ("docs/backend.md", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("docs/cli.md", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("docs/telegram.md", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("docs/security.md", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("docs/protocol.md", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("docs/quickstart.md", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("clients/discord/README.md", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("backend/core/agent.py", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("backend/core/tools.py", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("backend/config/prompts.py", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("backend/config/prompts.py", r"Wersja oprogramowania: (?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("clients/telegram/bot.py", r"PipeClaw v(?P<ver>\d+\.\d+(?:\.\d+)?)"),
    ("clients/cli/cli.py", r"\| v(?P<ver>\d+\.\d+(?:\.\d+)?)\[/dim\]"),
]


def read_current() -> str:
    if not VERSION_FILE.exists():
        sys.exit("Brak pliku VERSION w korzeniu repozytorium.")
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def compute_next(current: str, bump: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", bump):
        return bump

    parts = current.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        sys.exit(f"Wersja w pliku VERSION nie jest w formacie X.Y.Z: {current!r}")
    major, minor, patch = (int(p) for p in parts)

    if bump == "patch":
        patch += 1
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump == "major":
        major, minor, patch = major + 1, 0, 0
    else:
        sys.exit(f"Nieznany typ podbicia: {bump!r} (uzyj patch|minor|major|X.Y.Z)")

    return f"{major}.{minor}.{patch}"


def rewrite(new: str) -> list[str]:
    """Podmienia wersje we wszystkich plikach. Zwraca liste zmienionych plikow."""
    changed: list[str] = []

    for rel, pattern in PATTERNS:
        path = ROOT / rel
        if not path.exists():
            print(f"  [pominieto] brak pliku: {rel}")
            continue

        text = path.read_text(encoding="utf-8")

        def _sub(match: re.Match[str]) -> str:
            span_start, span_end = match.span("ver")
            return match.group(0)[: span_start - match.start()] + new + match.group(0)[span_end - match.start() :]

        new_text, count = re.subn(pattern, _sub, text)
        if count == 0:
            print(f"  [uwaga] wzorzec nie pasuje w {rel} — sprawdz recznie")
        elif new_text != text:
            path.write_text(new_text, encoding="utf-8")
            if rel not in changed:
                changed.append(rel)

    VERSION_FILE.write_text(new + "\n", encoding="utf-8")
    changed.insert(0, "VERSION")
    return changed


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)

    arg = sys.argv[1]
    current = read_current()

    if arg == "--current":
        print(current)
        return

    new = compute_next(current, arg)
    print(f"Wersja: {current} -> {new}")
    for rel in rewrite(new):
        print(f"  zaktualizowano: {rel}")
    print("\nPamietaj o dopisaniu sekcji do docs/changelog.md.")


if __name__ == "__main__":
    main()
