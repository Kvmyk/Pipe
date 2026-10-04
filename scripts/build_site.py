"""
Sklada strone projektu (GitHub Pages) do jednego katalogu: site/ + logo i nagrania z docs/assets
+ kroje IBM Plex z pipe web + dokumentacja z docs/*.md (polska, /docs/) i docs/en/*.md (angielska,
/en/docs/). Uzywa go workflow .github/workflows/pages.yml i podglad lokalny:

    python scripts/build_site.py              # -> _site/
    python -m http.server -d _site 8000       # http://localhost:8000

Markdown renderuje wlasny, maly konwerter (`render_markdown`) -- obsluguje tylko to, czego uzywaja
pliki w docs/: naglowki, akapity, listy (takze zagniezdzone), tabele, bloki kodu, cytaty, linie
poziome, kod w linii, linki, pogrubienie i kursywe. Cala tresc jest escapowana, surowy HTML z plikow
.md nie przechodzi. Tylko biblioteka standardowa.
"""

from __future__ import annotations

import html
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ["docs/assets/logo.svg", "docs/assets/demo-pl.gif", "docs/assets/demo-en.gif"]
FONTS = ROOT / "clients" / "webui" / "static" / "fonts"
REPO = "https://github.com/Kvmyk/pipe"

# Strony dokumentacji w kolejnosci menu: (plik bez .md, tytul w menu pl, en, opis pl, en).
# Kazdy plik musi istniec w docs/ i w docs/en/ -- inaczej budowanie konczy sie bledem.
DOCS: list[tuple[str, str, str, str, str]] = [
    ("quickstart", "Szybki start", "Quick start",
     "Instalacja na serwerze i na komputerze, pierwsza rozmowa, typowe problemy.",
     "Installing on the server and on your computer, the first conversation, common problems."),
    ("features", "Funkcje", "Features",
     "Czuwanie, historia serwera, cele i pomocnicy, rutyny, bezpiecznik zmian, audyt, MCP i reszta.",
     "Watching, server history, targets and workers, routines, the change safety fuse, audit, MCP and more."),
    ("cli", "Terminal i przeglądarka", "Terminal and browser",
     "Komenda pipe, pipe web, tunel SSH, Kubernetes, lista komend.",
     "The pipe command, pipe web, the SSH tunnel, Kubernetes, the command list."),
    ("telegram", "Telegram", "Telegram",
     "Bot krok po kroku: alerty, głosówki, zdjęcia, role, komendy.",
     "The bot step by step: alerts, voice messages, photos, roles, commands."),
    ("deploy", "Wdrożenie", "Deployment",
     "Docker, systemd, Kubernetes, cloud-init, strefa czasowa, aktualizacja.",
     "Docker, systemd, Kubernetes, cloud-init, time zone, updating."),
    ("security", "Bezpieczeństwo", "Security",
     "Klasyfikacja komend, redakcja sekretów, potwierdzenia, role, ograniczenia.",
     "Command classification, secret redaction, confirmations, roles, limitations."),
    ("backend", "Backend i konfiguracja", "Backend and configuration",
     "Moduły, providerzy LLM, plik .env, pamięć agenta, narzędzia.",
     "Modules, LLM providers, the .env file, agent memory, tools."),
    ("protocol", "Protokół", "Protocol",
     "Format JSON lines, komendy klienta, zdarzenia, webhooki. Dla autorów klientów.",
     "The JSON lines format, client commands, events, webhooks. For client authors."),
]

LANGS = {
    "pl": {"src": ROOT / "docs", "out": "docs", "home": "../", "other": "../en/docs/"},
    "en": {"src": ROOT / "docs" / "en", "out": "en/docs", "home": "../", "other": "../../docs/"},
}

T = {
    "pl": {"docs": "Dokumentacja", "on_page": "Na tej stronie", "all": "Wszystkie strony",
           "edit": "Popraw tę stronę na GitHubie", "changelog": "Historia zmian", "issues": "Zgłoś błąd",
           "source": "Kod na GitHubie", "license": "licencja MIT", "install": "Instalacja",
           "index_lead": "Wszystko o instalacji, codziennej pracy z Pipe i o tym, jak działa w środku. "
                         "Strony powstają z plików w katalogu docs/ repozytorium, więc opisują aktualną wersję.",
           "index_title": "Dokumentacja Pipe", "start": "Zacznij od szybkiego startu",
           "changelog_note": "Historia zmian jest na GitHubie (po polsku)."},
    "en": {"docs": "Docs", "on_page": "On this page", "all": "All pages",
           "edit": "Edit this page on GitHub", "changelog": "Changelog", "issues": "Report a bug",
           "source": "Source on GitHub", "license": "MIT license", "install": "Installation",
           "index_lead": "Everything about installing Pipe, working with it day to day and how it works inside. "
                         "The pages are built from the docs/ directory of the repository, so they describe the current version.",
           "index_title": "Pipe documentation", "start": "Start with the quick start",
           "changelog_note": "The changelog is on GitHub (in Polish)."},
}

LOGO = ('<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill-rule="evenodd" '
        'd="M4 7a4 4 0 0 1 4-4h5a5 5 0 0 1 0 10H9v8.5H4zm9-1.2a2.2 2.2 0 1 0 0 4.4 2.2 2.2 0 0 0 0-4.4z" '
        'fill="currentColor"/></svg>')

COPYABLE = {"bash", "sh", "shell", "powershell", "ps1", "console"}


# --------------------------------------------------------------------------- Markdown


def slugify(text: str, used: dict[str, int]) -> str:
    """Kotwica jak na GitHubie: male litery, bez interpunkcji, spacje -> '-', duplikaty z -1, -2..."""
    base = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")
    n = used.get(base, 0)
    used[base] = n + 1
    return base if n == 0 else f"{base}-{n}"


def plain(text: str) -> str:
    """Tekst naglowka bez skladni Markdown (do kotwic i spisu tresci)."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.replace("`", "").replace("**", "").replace("*", "")


_CODE = re.compile(r"(`+)(.+?)\1")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_AUTO = re.compile(r"(?<![\"'=>])\bhttps?://[^\s<)]+[^\s<).,:;!?]")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITAL = re.compile(r"(?<![\w*\\])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")


def inline(text: str, link) -> str:
    """Formatowanie w linii. `link(url)` przepisuje adresy (linki miedzy plikami .md -> .html)."""
    slots: list[str] = []

    def keep(fragment: str) -> str:
        slots.append(fragment)
        return f"\x00{len(slots) - 1}\x00"

    text = _CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(2).strip())}</code>"), text)
    text = _LINK.sub(lambda m: keep(f'<a href="{html.escape(link(m.group(2)))}">'
                                    f"{_format(html.escape(m.group(1), quote=False), slots)}</a>"), text)
    text = _AUTO.sub(lambda m: keep(f'<a href="{html.escape(m.group(0))}">{html.escape(m.group(0))}</a>'), text)
    text = _format(html.escape(text, quote=False), slots)
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], text)


def _format(text: str, slots: list[str]) -> str:
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITAL.sub(r"<em>\1</em>", text)
    text = re.sub(r"(?<=\s)--(?=\s)", "–", text)
    text = re.sub(r"(?<=\s)-&gt;(?=\s)", "→", text)
    return text


@dataclass
class Rendered:
    title: str
    body: str
    headings: list[tuple[str, str]] = field(default_factory=list)   # (id, tekst) naglowkow h2


_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells, current, in_code = [], "", False
    for i, ch in enumerate(row):
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code and (i == 0 or row[i - 1] != "\\"):
            cells.append(current)
            current = ""
        else:
            current += ch
    cells.append(current)
    return [c.strip().replace("\\|", "|") for c in cells]


def render_markdown(source: str, link=lambda url: url) -> Rendered:
    lines = source.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    used: dict[str, int] = {}
    headings: list[tuple[str, str]] = []
    title = ""
    i = 0

    def para_end(line: str) -> bool:
        return (not line.strip() or line.startswith(("#", "```", ">", "|")) or bool(_LIST_ITEM.match(line))
                or re.fullmatch(r"\s*(-{3,}|\*{3,})\s*", line) is not None)

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        fence = re.match(r"^(\s*)(```+|~~~+)\s*([\w+-]*)", line)
        if fence:
            marker, lang = fence.group(2), fence.group(3).lower()
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                block.append(lines[i])
                i += 1
            i += 1
            cls = ' class="cmd"' if lang in COPYABLE else ""
            out.append(f"<pre{cls}><code>{html.escape(chr(10).join(block))}</code></pre>")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", line)
        if heading:
            level, text = len(heading.group(1)), heading.group(2)
            if level == 1 and not title:
                title = re.sub(r"\s+--\s+Pipe$", "", plain(text)).replace(" -- ", " – ")
                out.append(f"<h1>{inline(re.sub(r'\s+--\s+Pipe$', '', text), link)}</h1>")
            else:
                anchor = slugify(plain(text), used)
                if level == 2:
                    headings.append((anchor, plain(text).replace(" -- ", " – ")))
                out.append(f'<h{level} id="{anchor}">{inline(text, link)}'
                           f'<a class="anchor" href="#{anchor}" aria-hidden="true">#</a></h{level}>')
            i += 1
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c, link)}</th>" for c in head)
            body = "".join("<tr>" + "".join(f"<td>{inline(c, link)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>')
            continue

        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{render_markdown(chr(10).join(quote), link).body}</blockquote>")
            continue

        if _LIST_ITEM.match(line):
            html_list, i = _render_list(lines, i, link)
            out.append(html_list)
            continue

        block = [stripped]
        i += 1
        while i < len(lines) and not para_end(lines[i]):
            block.append(lines[i].strip())
            i += 1
        text = " ".join(block)
        if re.fullmatch(r"Pipe v\d+\.\d+\.\d+", text):
            out.append(f'<p class="ver">{html.escape(text)}</p>')
        else:
            out.append(f"<p>{inline(text, link)}</p>")

    return Rendered(title=title, body="\n".join(out), headings=headings)


def _render_list(lines: list[str], i: int, link) -> tuple[str, int]:
    """Lista od linii i. Wciecie pierwszej pozycji wyznacza poziom; glebsze pozycje to podlista,
    glebiej wciete zwykle linie to ciag dalszy pozycji."""
    first = _LIST_ITEM.match(lines[i])
    indent = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    start = int(re.match(r"\d+", first.group(2)).group(0)) if ordered else 1
    items: list[str] = []
    while i < len(lines):
        m = _LIST_ITEM.match(lines[i])
        if not m or len(m.group(1)) != indent or m.group(2)[0].isdigit() != ordered:
            break
        text = [m.group(3).strip()]
        nested: list[str] = []
        i += 1
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                # pusta linia: lista trwa, jesli dalej jest pozycja tego samego poziomu albo wciecie
                nxt = next((l for l in lines[i + 1:] if l.strip()), "")
                nm = _LIST_ITEM.match(nxt)
                if nxt and (len(nxt) - len(nxt.lstrip()) > indent or (nm and len(nm.group(1)) == indent)):
                    i += 1
                    continue
                break
            sub = _LIST_ITEM.match(line)
            if sub and len(sub.group(1)) > indent:
                block, i = _render_list(lines, i, link)
                nested.append(block)
                continue
            if sub:
                break
            if len(line) - len(line.lstrip()) > indent:
                text.append(line.strip())
                i += 1
                continue
            if line.startswith(("#", "```", "|", ">")) or re.fullmatch(r"\s*(-{3,}|\*{3,})\s*", line):
                break
            text.append(line.strip())       # leniwa kontynuacja bez wciecia
            i += 1
        items.append(f"<li>{inline(' '.join(text), link)}{''.join(nested)}</li>")
    tag = "ol" if ordered else "ul"
    attr = f' start="{start}"' if ordered and start != 1 else ""
    return f"<{tag}{attr}>{''.join(items)}</{tag}>", i


# --------------------------------------------------------------------------- strony


def doc_link(lang: str, doc_file: str):
    """Przepisuje adres z pliku docs[/en]/X.md: inna strona dokumentacji -> X.html, plik repo -> GitHub."""
    src_dir = posixpath.dirname(doc_file)
    names = {d[0] for d in DOCS}

    def link(url: str) -> str:
        if re.match(r"^[a-z]+:|^#", url):
            return url
        path, _, anchor = url.partition("#")
        target = posixpath.normpath(posixpath.join(src_dir, path))
        for base in ("docs/en", "docs"):
            if posixpath.dirname(target) == base and target.endswith(".md"):
                name = posixpath.basename(target)[:-3]
                if name in names and (base == "docs/en") == (lang == "en"):
                    return f"{name}.html" + (f"#{anchor}" if anchor else "")
        kind = "blob" if posixpath.splitext(target)[1] else "tree"
        return f"{REPO}/{kind}/main/{target}" + (f"#{anchor}" if anchor else "")

    return link


def page(lang: str, *, title: str, description: str, current: str, article: str,
         headings: list[tuple[str, str]], version: str, edit: str | None) -> str:
    t = T[lang]
    conf = LANGS[lang]
    root = "../" if lang == "pl" else "../../"
    other = "en" if lang == "pl" else "pl"
    other_href = conf["other"] + (f"{current}.html" if current != "index" else "")
    nav_items = []
    for name, pl, en, _, _ in DOCS:
        label = pl if lang == "pl" else en
        cur = ' aria-current="page"' if name == current else ""
        nav_items.append(f'<li><a href="{name}.html"{cur}>{html.escape(label)}</a></li>')
    toc = ""
    if headings:
        toc = (f'<div class="toc"><p class="side-h">{t["on_page"]}</p><ul>'
               + "".join(f'<li><a href="#{a}">{html.escape(h)}</a></li>' for a, h in headings)
               + "</ul></div>")
    edit_link = f'<p class="edit"><a href="{REPO}/edit/main/{edit}">{t["edit"]}</a></p>' if edit else ""
    install = "#instalacja" if lang == "pl" else "#installation"
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Pipe</title>
<meta name="description" content="{html.escape(description)}">
<link rel="icon" href="{root}assets/logo.svg" type="image/svg+xml">
<link rel="alternate" hreflang="{other}" href="{other_href}">
<link rel="stylesheet" href="{root}style.css">
</head>
<body class="docs">

<header class="top">
  <div class="wrap">
    <a class="brand" href="{conf["home"]}">
      {LOGO}
      Pipe
    </a>
    <nav>
      <a class="hide-sm" href="{conf["home"]}{install}">{t["install"]}</a>
      <a href="./"{' aria-current="page"' if current == "index" else ""}>{t["docs"]}</a>
      <a href="{REPO}">GitHub</a>
      <a class="lang" href="{other_href}" hreflang="{other}" lang="{other}">{other.upper()}</a>
    </nav>
  </div>
</header>

<div class="wrap doc-layout">
  <aside class="side">
    <p class="side-h"><a href="./">{t["docs"]}</a></p>
    <ul class="doc-nav">{"".join(nav_items)}</ul>
    {toc}
  </aside>
  <main class="doc" id="top">
{article}
{edit_link}
  </main>
</div>

<footer>
  <div class="wrap">
    <span>Pipe {version} · {t["license"]}</span>
    <a href="{REPO}">{t["source"]}</a>
    <a href="{REPO}/blob/main/docs/changelog.md">{t["changelog"]}</a>
    <a href="{REPO}/issues">{t["issues"]}</a>
  </div>
</footer>

<script src="{root}copy.js"></script>
</body>
</html>
"""


def index_article(lang: str) -> str:
    t = T[lang]
    cards = []
    for name, pl, en, dpl, den in DOCS:
        label, desc = (pl, dpl) if lang == "pl" else (en, den)
        cards.append(f'<li><a href="{name}.html">{html.escape(label)}</a><p>{html.escape(desc)}</p></li>')
    return (f'<h1>{t["index_title"]}</h1>\n<p class="lead">{t["index_lead"]}</p>\n'
            f'<p><a class="button" href="quickstart.html">{t["start"]} →</a></p>\n'
            f'<ul class="doc-cards">{"".join(cards)}</ul>\n'
            f'<p class="note"><a href="{REPO}/blob/main/docs/changelog.md">{t["changelog"]}</a>: '
            f'{t["changelog_note"]}</p>')


def build_docs(out: Path, version: str) -> list[Path]:
    written: list[Path] = []
    for lang, conf in LANGS.items():
        target = out / conf["out"]
        target.mkdir(parents=True, exist_ok=True)
        for name, pl, en, dpl, den in DOCS:
            src = conf["src"] / f"{name}.md"
            if not src.exists():
                raise SystemExit(f"brak pliku dokumentacji: {src.relative_to(ROOT)}")
            rel = src.relative_to(ROOT).as_posix()
            doc = render_markdown(src.read_text(encoding="utf-8"), doc_link(lang, rel))
            path = target / f"{name}.html"
            path.write_text(page(lang, title=doc.title or (pl if lang == "pl" else en),
                                 description=dpl if lang == "pl" else den, current=name, article=doc.body,
                                 headings=doc.headings, version=version, edit=rel), encoding="utf-8")
            written.append(path)
        path = target / "index.html"
        path.write_text(page(lang, title=T[lang]["index_title"], description=T[lang]["index_lead"],
                             current="index", article=index_article(lang), headings=[], version=version,
                             edit=None), encoding="utf-8")
        written.append(path)
    return written


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
    build_docs(out, (ROOT / "VERSION").read_text(encoding="utf-8").strip())
    (out / ".nojekyll").write_text("", encoding="utf-8")       # pliki serwowane jak sa, bez Jekylla


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site"
    build(target)
    print(f"strona: {target}")
