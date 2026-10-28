"""
Obrazki podgladu linku (GitHub, strona projektu): docs/assets/social-preview.png (pl) i social-preview-en.png,
1280x640. Sklada strone HTML z kroju IBM Plex z pipe web i logo, a Playwright (Chrome) robi zrzut.

    python scripts/demo/social_preview.py

Po zmianie hasla Pipe zmien teksty w TEXT i uruchom ponownie; obrazek na GitHubie podmienia sie recznie
(Settings -> Social preview).
"""

from __future__ import annotations

import asyncio
import html
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "clients" / "webui" / "static" / "fonts"

TEXT = {
    "en": {
        "title": "A sysadmin that never sleeps, does the routine work itself and asks about what matters",
        "lead": "An AI agent on your Linux server. It messages you on Telegram when something breaks and fixes it, "
                "with a backup you can undo.",
        "auto": "Without asking: a routine change", "cmd": "$ sed -i 's/60s/120s/' shop.conf\n  && systemctl reload nginx",
        "checks": ["backup in the journal", "nginx -t passed", "sites still respond"],
        "done": "Fixed, the shop responds again. Undo it with", "undo": "/undo",
        "me": "you", "ask_msg": "and delete the old reports", "ask": "This one needs your yes",
        "ask_why": "data I cannot back up", "ask_cmd": "$ rm -r reports/2025",
    },
    "pl": {
        "title": "Admin, który nie śpi, rutynę robi sam, a o ważne sprawy pyta",
        "lead": "Agent AI na Twoim serwerze Linux. Pisze na Telegram, gdy coś się psuje, i naprawia to, "
                "z kopią, którą cofniesz.",
        "auto": "Bez pytania: rutynowa zmiana", "cmd": "$ sed -i 's/60s/120s/' sklep.conf\n  && systemctl reload nginx",
        "checks": ["kopia w dzienniku", "nginx -t przeszedł", "strony dalej odpowiadają"],
        "done": "Naprawione, sklep znów odpowiada. Cofniesz to komendą", "undo": "/cofnij",
        "me": "ty", "ask_msg": "i usuń stare raporty", "ask": "Tu potrzebuję Twojego „tak”",
        "ask_why": "danych nie da się skopiować", "ask_cmd": "$ rm -r raporty/2025",
    },
}


def page(lang: str) -> str:
    t = {k: (html.escape(v) if isinstance(v, str) else [html.escape(x) for x in v]) for k, v in TEXT[lang].items()}
    fonts = "".join(
        f'@font-face {{ font-family: "{family}"; font-weight: {weight}; src: url("{(FONTS / f"ibm-plex-{slug}-{sub}-{weight}-normal.woff2").as_uri()}"); '
        f'{"unicode-range: U+0100-024F, U+1E00-1EFF, U+2020;" if sub == "latin-ext" else ""} }}\n'
        for family, slug, weights in (("Plex Sans", "sans", (400, 500, 600)), ("Plex Mono", "mono", (400, 500)))
        for weight in weights for sub in ("latin-ext", "latin"))
    checks = "".join(f"<li>{c}</li>" for c in t["checks"])
    logo = (ROOT / "docs" / "assets" / "logo.svg").as_uri()
    return f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><style>
{fonts}
* {{ box-sizing: border-box; }}
body {{ margin: 0; width: 1280px; height: 640px; background: #f4f4f1; color: #1a1b1d; font-family: "Plex Sans"; display: flex;
  padding: 72px 80px 0; gap: 64px; position: relative; }}
.left {{ width: 590px; }}
.brand {{ display: flex; align-items: center; gap: 16px; font-size: 34px; font-weight: 600; }}
h1 {{ font-size: 50px; line-height: 1.12; letter-spacing: -.01em; margin: 38px 0 22px; font-weight: 600; }}
.lead {{ font-size: 22px; line-height: 1.6; color: #4e5055; margin: 0; }}
.foot {{ position: absolute; left: 80px; bottom: 44px; font: 400 18px "Plex Mono"; color: #75777c; }}
.card {{ width: 466px; align-self: flex-start; margin-top: 4px; background: #fff; border: 1px solid #e0dfd9; border-radius: 12px; }}
.row {{ display: grid; grid-template-columns: 62px 1fr; gap: 0; padding: 18px 22px; font-size: 17px; line-height: 1.45; }}
.row + .row {{ border-top: 1px solid #e8e7e2; }}
.who {{ font: 400 14px "Plex Mono"; color: #75777c; padding-top: 3px; }}
.box {{ border: 1px solid; border-radius: 6px; padding: 12px 14px; background: #f8f8f5; }}
.box.auto {{ border-color: #8fc3a6; margin-bottom: 12px; }}
.box.ask {{ border-color: #d9b47c; }}
.box b {{ display: block; font-weight: 600; margin-bottom: 6px; }}
.box .why {{ color: #75777c; font-weight: 400; }}
pre {{ margin: 0 0 8px; font: 400 14.5px/1.5 "Plex Mono"; white-space: pre-wrap; }}
.box.ask pre {{ margin: 0; }}
ul {{ margin: 0; padding: 0; list-style: none; color: #4e5055; font-size: 15.5px; }}
li::before {{ content: "✓"; color: #1d7f49; margin-right: 8px; }}
code {{ font: 400 15px "Plex Mono"; background: #efefeb; border-radius: 4px; padding: 1px 5px; }}
</style></head><body>
<div class="left">
  <div class="brand"><img src="{logo}" width="56" height="56" alt="">Pipe</div>
  <h1>{t["title"]}</h1>
  <p class="lead">{t["lead"]}</p>
</div>
<div class="card">
  <div class="row"><div class="who">pipe</div><div>
    <div class="box auto"><b>{t["auto"]}</b><pre>{t["cmd"]}</pre><ul>{checks}</ul></div>
    {t["done"]} <code>{t["undo"]}</code>.</div></div>
  <div class="row"><div class="who">{t["me"]}</div><div><b>{t["ask_msg"]}</b></div></div>
  <div class="row"><div class="who">pipe</div><div>
    <div class="box ask"><b>{t["ask"]} <span class="why">· {t["ask_why"]}</span></b><pre>{t["ask_cmd"]}</pre></div></div></div>
</div>
<div class="foot">open source · MIT · Gemini, OpenAI, Claude, Ollama</div>
</body></html>"""


async def main() -> None:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 640})
        page_ = await context.new_page()
        with tempfile.TemporaryDirectory() as tmp:
            for lang, name in (("en", "social-preview-en.png"), ("pl", "social-preview.png")):
                source = Path(tmp) / f"{lang}.html"
                source.write_text(page(lang), encoding="utf-8")
                await page_.goto(source.as_uri())
                await page_.evaluate("document.fonts.ready")
                await page_.wait_for_timeout(300)
                out = ROOT / "docs" / "assets" / name
                await page_.screenshot(path=str(out))
                print(out.relative_to(ROOT))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
