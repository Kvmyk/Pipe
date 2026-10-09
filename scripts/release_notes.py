#!/usr/bin/env python3
"""
Notatki do wydania na GitHubie: sekcja `## vX.Y.Z` z docs/changelog.md + jak pobrac i sprawdzic obraz.

    python scripts/release_notes.py 0.27.0 > notes.md

Uzywa go .github/workflows/release.yml. Tylko biblioteka standardowa.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "docs" / "changelog.md"
IMAGE = "ghcr.io/kvmyk/pipe"
REPO = "Kvmyk/Pipe"        # nazwa z GitHuba — podpis cosign zawiera ja z ta wielkoscia liter


def section(changelog: str, version: str) -> str:
    """Tresc sekcji danej wersji (bez naglowka) albo pusty tekst."""
    match = re.search(rf"^## v{re.escape(version)}(?=\s|$).*?$\n(.*?)(?=^## v|\Z)", changelog, re.M | re.S)
    return match.group(1).strip() if match else ""


def notes(changelog: str, version: str) -> str:
    body = section(changelog, version)
    if not body:
        raise SystemExit(f"Brak sekcji v{version} w docs/changelog.md")
    return f"""{body}

---

Obrazy / images (linux/amd64, linux/arm64):

```
docker pull {IMAGE}:{version}
docker pull {IMAGE}-telegram:{version}
```

Podpis / signature (cosign, keyless):

```
cosign verify {IMAGE}:{version} \\
  --certificate-identity-regexp '^https://github.com/{REPO}/\\.github/workflows/release\\.yml@' \\
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
"""


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("uzycie: python scripts/release_notes.py X.Y.Z", file=sys.stderr)
        return 2
    print(notes(CHANGELOG.read_text(encoding="utf-8"), argv[0].lstrip("v")), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
