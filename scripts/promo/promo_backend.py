"""
Atrapa backendu do nagrania reklamy — scenariusz z scripts/demo/demo_backend.py plus druga prosba (usuniecie
starych raportow), przy ktorej Pipe pyta o zgode, bo tego nie da sie cofnac.

`--slow K` wydluza kazda pauze scenariusza K razy: capture.py nagrywa w zwolnionym tempie (ostre klatki 3x sa
wolne), a render przyspiesza nagranie z powrotem, wiec rytm w filmie jest taki jak w zwyklym demo.

    python scripts/promo/promo_backend.py --port 7390 --lang en --slow 8
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "demo"))

import demo_backend as demo  # noqa: E402

REPORTS = "/srv/shop/reports/2025"
DELETE = f"rm -r {REPORTS}"


async def ask_delete(writer) -> None:
    """Usuniecie danych bez kopii to nie rutyna: Pipe sprawdza rozmiar i czeka na TAK."""
    from backend.core.events import Activity
    await asyncio.sleep(0.6)
    await demo.tool(writer, "d1", "execute_command", {"command": f"du -sh {REPORTS} && find {REPORTS} -type f | wc -l"},
                    [], 1.0)
    from backend.core import graph
    args = {"command": DELETE}
    activity = Activity("d2", "start", "execute_command", graph.describe("execute_command", args),
                        tuple(graph.locate("execute_command", args, "/srv/shop")))
    await demo.send(writer, demo.frame(activity))
    await asyncio.sleep(0.3)
    await demo.send(writer, demo.frame(
        f"[POTWIERDZ] Deleting old reports requires confirmation: `{DELETE}`\n"
        "Safety fuse:\n"
        "- what it removes: 2,140 files, 3.8 GB\n"
        "- backup before the change: none, the data is too large to copy\n"
        "- undo later: not possible, after deleting there is no copy to restore"))
    from dataclasses import replace
    await demo.send(writer, demo.frame(replace(activity, phase="wait")))


class PromoDemo(demo.Demo):
    async def route(self, request: dict, writer) -> None:
        if "message" in request and "delete" in str(request["message"]).lower():
            await ask_delete(writer)
            await demo.done(writer)
            return
        await super().route(request, writer)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Pipe promo backend (scripted)")
    parser.add_argument("--port", type=int, default=7390)
    parser.add_argument("--lang", choices=["en", "pl"], default="en")
    parser.add_argument("--slow", type=float, default=1.0, help="every pause of the scenario lasts K times longer")
    parser.add_argument("--full", action="store_true", help="dashboard with the inbox and an alert")
    args = parser.parse_args()
    os.environ["PIPE_LANG"] = args.lang
    demo.configure(args.lang)

    sleep = asyncio.sleep

    async def slow_sleep(seconds: float, *a, **kw):
        # subskrypcja czeka godzine — tego nie wydluzamy
        return await sleep(seconds * args.slow if seconds < 600 else seconds, *a, **kw)

    asyncio.sleep = slow_sleep
    from backend.core import graph
    import backend.server  # noqa: F401
    graph.build(demo.infra())
    server = await asyncio.start_server(PromoDemo(full=args.full).handle, "127.0.0.1", args.port, limit=32 * 1024 * 1024)
    print(f"promo backend on 127.0.0.1:{args.port} ({args.lang}, slow x{args.slow})", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
