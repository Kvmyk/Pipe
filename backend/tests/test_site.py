"""Strona projektu: renderer Markdown z scripts/build_site.py i dokumentacja w dwoch jezykach."""

import importlib.util
import re
import sys
from pathlib import Path

# scripts/ nie jest pakietem (a nazwa "scripts" bywa zajeta w site-packages) -- ladujemy modul z pliku
_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_site.py"
_spec = importlib.util.spec_from_file_location("build_site", _PATH)
build_site = importlib.util.module_from_spec(_spec)
sys.modules["build_site"] = build_site
_spec.loader.exec_module(build_site)
DOCS, ROOT, build, doc_link, render_markdown = (
    build_site.DOCS, build_site.ROOT, build_site.build, build_site.doc_link, build_site.render_markdown)


def test_every_doc_page_exists_in_both_languages():
    for name, *_ in DOCS:
        assert (ROOT / "docs" / f"{name}.md").exists(), name
        assert (ROOT / "docs" / "en" / f"{name}.md").exists(), f"brak tlumaczenia docs/en/{name}.md"


def test_english_docs_have_the_same_sections_count_roughly():
    # tlumaczenie nie gubi rozdzialow: tyle samo naglowkow h2
    for name, *_ in DOCS:
        pl = re.findall(r"^## ", (ROOT / "docs" / f"{name}.md").read_text(encoding="utf-8"), re.M)
        en = re.findall(r"^## ", (ROOT / "docs" / "en" / f"{name}.md").read_text(encoding="utf-8"), re.M)
        assert len(pl) == len(en), name


def test_render_basics():
    doc = render_markdown(
        "# Tytul -- Pipe\n\nPipe v1.2.3\n\n## Sekcja `kod`\n\nTekst **gruby** i *kursywa* -- [link](x.md).\n\n"
        "- a\n- b\n  ciag dalszy\n\n1. jeden\n2. dwa\n\n| A | B |\n|---|---|\n| `x\\|y` | 2 |\n\n```bash\necho <hi>\n```\n"
    )
    assert doc.title == "Tytul"
    assert '<p class="ver">Pipe v1.2.3</p>' in doc.body
    assert doc.headings == [("sekcja-kod", "Sekcja kod")]
    assert "<strong>gruby</strong>" in doc.body and "<em>kursywa</em>" in doc.body and "–" in doc.body
    assert "<li>b ciag dalszy</li>" in doc.body and "<ol>" in doc.body
    assert "<code>x|y</code>" in doc.body
    assert '<pre class="cmd"><code>echo &lt;hi&gt;</code></pre>' in doc.body


def test_raw_html_is_escaped():
    body = render_markdown("<script>alert(1)</script> i [x](javascript:alert(1))").body
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_links_between_docs_and_to_repo():
    pl = doc_link("pl", "docs/quickstart.md")
    assert pl("features.md#czuwanie") == "features.html#czuwanie"
    assert pl("../deploy/cloud-init/user-data.yaml").endswith("/blob/main/deploy/cloud-init/user-data.yaml")
    en = doc_link("en", "docs/en/quickstart.md")
    assert en("./deploy.md") == "deploy.html"
    assert en("../../deploy/kubernetes/README.md").endswith("/blob/main/deploy/kubernetes/README.md")
    assert en("https://example.com") == "https://example.com"


def test_built_site_has_no_broken_internal_links(tmp_path):
    out = tmp_path / "site"
    build(out)
    pages = list(out.glob("docs/*.html")) + list(out.glob("en/docs/*.html"))
    assert len(pages) == 2 * (len(DOCS) + 1)
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for href in re.findall(r'href="([^"]+)"', text):
            if href.startswith(("http://", "https://")):
                continue
            path, _, anchor = href.partition("#")
            target = (page.parent / path).resolve() if path else page
            if target.is_dir():
                target = target / "index.html"
            assert target.exists(), f"{page.relative_to(out)}: {href}"
            if anchor and target.suffix == ".html":
                assert f'id="{anchor}"' in target.read_text(encoding="utf-8"), f"{page.relative_to(out)}: {href}"
