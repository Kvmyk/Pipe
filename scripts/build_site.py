"""
Sklada strone projektu (GitHub Pages) do jednego katalogu: site/ + logo i nagrania z docs/assets
+ kroje IBM Plex z pipe web. Uzywa go workflow .github/workflows/pages.yml i podglad lokalny:

    python scripts/build_site.py              # -> _site/
    python -m http.server -d _site 8000       # http://localhost:8000

Tylko biblioteka standardowa.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ["docs/assets/logo.svg", "docs/assets/demo-pl.gif", "docs/assets/demo-en.gif"]
FONTS = ROOT / "clients" / "webui" / "static" / "fonts"


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "site", out)
    assets = out / "assets"
    (assets / "fonts").mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        shutil.copy2(ROOT / name, assets / Path(name).name)
    for font in FONTS.glob("*.woff2"):
        shutil.copy2(font, assets / "fonts" / font.name)
    shutil.copy2(FONTS / "OFL.txt", assets / "fonts" / "OFL.txt")
    (out / ".nojekyll").write_text("", encoding="utf-8")       # pliki serwowane jak sa, bez Jekylla


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site"
    build(target)
    print(f"strona: {target}")
