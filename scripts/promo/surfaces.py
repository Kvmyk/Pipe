"""
Telegram i terminal do reklamy — z prawdziwych funkcji klientow Pipe, nie z makiet.

Telegram: tresci wiadomosci robi clients/telegram/tg_format.py (format_alert, format_investigation, activity_entry,
progress_text, to_telegram_html) — dokladnie to, co bot wysyla. Terminal: clients/cli/cli.py rysuje baner i /status
na konsoli rich z nagrywaniem; kolejne stany ekranu to HTML z export_html (kolory wedlug palety ponizej).

    python scripts/promo/surfaces.py --out surfaces.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT, ROOT / "clients" / "telegram", ROOT / "clients" / "cli", ROOT / "scripts" / "demo"):
    sys.path.insert(0, str(extra))
os.environ["PIPE_LANG"] = "en"

DOMAIN = "shop.example.com"
CONF = "/etc/nginx/sites-enabled/shop.conf"
FIX = f"sed -i 's/proxy_read_timeout 60s;/proxy_read_timeout 120s;/' {CONF} && systemctl reload nginx"


def telegram() -> dict:
    import tg_format as tg

    def step(phase: str, label: str, ok=None, auto=False) -> str:
        return tg.activity_entry({"phase": phase, "label": label, "tool": "execute_command", "ok": ok, "auto": auto})

    title = f"The site {DOMAIN} is not working correctly"
    report = ("FINDINGS:\n"
              "- nginx returns 502: upstream timed out (110) in error.log\n"
              "- the PDF report in shop-app takes about 75 s, nginx waits 60 s\n"
              "PROPOSALS:\n"
              "- raise proxy_read_timeout to 120s in shop.conf")
    progress = [
        [step("end", "nginx -t", True)],
        [step("end", "nginx -t", True), step("start", FIX)],
        [step("end", "nginx -t", True), step("end", FIX, True, auto=True)],
        [step("end", "nginx -t", True), step("end", FIX, True, auto=True), step("end", f"curl -sI https://{DOMAIN}", True)],
    ]
    return {
        "alert": tg.format_alert({"severity": "warning", "title": title, "detail": f"https://{DOMAIN}/ -> HTTP 502"}),
        "investigation": tg.format_investigation({"auto": True, "title": title, "report": report}),
        "buttons": ["Fix it", "Later"],
        "progress": [tg.progress_text(lines) for lines in progress],
        "progress_done": tg.progress_text(progress[-1], done=True),
        "answer": tg.to_telegram_html(
            f"Fixed — **{DOMAIN}** responds again. The report takes 75 s, nginx waited only 60 s; "
            "`proxy_read_timeout` is now 120 s. The backup is in the journal — undo it with `/undo`."),
    }


def terminal(width: int = 62) -> list[dict]:
    """Kolejne stany terminala: [{"html", "typed"?}] — scena pokazuje je po kolei w czasach z edit.py."""
    import cli
    import demo_backend as demo
    from rich.console import Console
    from rich.terminal_theme import TerminalTheme

    demo.configure("en")
    # paleta pipe web (ciemny motyw): tlo, tekst, potem 8 kolorow ANSI i 8 jasnych
    theme = TerminalTheme((11, 12, 14), (236, 236, 234), [
        (26, 27, 29), (242, 113, 108), (82, 192, 138), (240, 176, 76), (127, 157, 255), (192, 140, 255), (110, 200, 220), (200, 200, 196),
    ], [
        (90, 92, 98), (255, 140, 135), (120, 220, 165), (255, 200, 110), (157, 180, 255), (210, 170, 255), (140, 220, 235), (255, 255, 255),
    ])
    con = Console(record=True, width=width, theme=cli.PIPE_THEME, force_terminal=True, color_system="truecolor",
                  file=io.StringIO())
    cli.console = con
    states = []

    def snap(**extra) -> None:
        html = con.export_html(theme=theme, inline_styles=True, clear=False, code_format="{code}")
        states.append({"html": html, **extra})

    def prompt(text: str) -> None:
        # linia promptu CLI: pogrubiony znak "›" i to, co wpisano; kazda litera to osobny stan (dzwiek klawisza)
        base = len(states)
        for i in range(len(text) + 1):
            line = con.export_html(theme=theme, inline_styles=True, clear=False, code_format="{code}")
            states.append({"html": line, "prompt": text[:i], "key": i > 0})
        return base

    con.print("[dim]~[/dim] [bold green]$[/bold green] pipe", highlight=False)
    snap()
    cli._print_banner("vps-shop")
    snap()
    prompt("/status")
    con.print(f"[bold]›[/bold] /status", highlight=False)
    cli._print_overview(demo.overview_data(True))
    snap(output=True)
    return states


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram and terminal content for the promo")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    data = {"telegram": telegram(), "terminal": terminal()}
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"{args.out}: telegram {len(data['telegram'])} parts, terminal {len(data['terminal'])} states")


if __name__ == "__main__":
    main()
