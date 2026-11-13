"""
Renderuje reklame: scena (stage/) w Chrome klatka po klatce (seek(t) -> zrzut), kilka procesow rownolegle, kazdy do
wlasnego bezstratnego odcinka; potem jeden plik H.264 + dzwiek z score.py.

    python scripts/promo/render.py --capture <dir> --timeline timeline.json --audio score.wav --format f45 --out pipe-4x5.mp4
    python scripts/promo/render.py ... --stills 3,9,16,19.5,27,32,37     # tylko klatki PNG do podgladu

Formaty: f45 (1080x1350, feed), f916 (1080x1920, Reels/Stories/Shorts), f169 (1920x1080, YouTube/strona).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGE = Path(__file__).with_name("stage")
FONTS = ROOT / "clients" / "webui" / "static" / "fonts"
SIZES = {"f45": (1080, 1350), "f916": (1080, 1920), "f169": (1920, 1080)}


class Handler(http.server.SimpleHTTPRequestHandler):
    routes: dict[str, Path] = {}

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0]
        for prefix, base in self.routes.items():
            if path == prefix or path.startswith(prefix + "/"):
                rest = path[len(prefix):].lstrip("/")
                target = (base / rest) if rest else base
                return str(target)
        return str(STAGE / "missing")

    def log_message(self, *args) -> None:
        pass


def serve(capture: Path, timeline: Path, surfaces: Path) -> int:
    Handler.routes = {"/stage": STAGE, "/fonts": FONTS, "/cap": capture, "/timeline.json": timeline,
                      "/surfaces.json": surfaces}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


async def shoot(port: int, fmt: str, times: list[float], sink, delay: float = 0.0) -> None:
    from playwright.async_api import async_playwright
    w, h = SIZES[fmt]
    await asyncio.sleep(delay)          # procesy startuja po kolei: lokalny serwer zrywal rownoczesne pobrania
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
        for attempt in range(4):
            try:
                await page.goto(f"http://127.0.0.1:{port}/stage/index.html?f={fmt}")
                await page.evaluate("window.ready")
                break
            except Exception:
                if attempt == 3:
                    raise
                await asyncio.sleep(1 + attempt)
        for t in times:
            await page.evaluate("(t) => window.seek(t)", t)
            sink(t, await page.screenshot(type="png", animations="disabled", caret="hide"))
        await browser.close()


def encode_segment(port: int, fmt: str, times: list[float], fps: int, out: Path, delay: float = 0.0) -> None:
    w, h = SIZES[fmt]
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps),
                           "-i", "-", "-c:v", "libx264rgb", "-qp", "0", "-preset", "ultrafast", str(out)],
                          stdin=subprocess.PIPE)

    def sink(t, png):
        ff.stdin.write(png)

    asyncio.run(shoot(port, fmt, times, sink, delay))
    ff.stdin.close()
    if ff.wait():
        raise SystemExit(f"ffmpeg failed for {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the promo video")
    parser.add_argument("--capture", required=True)
    parser.add_argument("--timeline", required=True)
    parser.add_argument("--surfaces", required=True, help="surfaces.json from surfaces.py (Telegram, terminal)")
    parser.add_argument("--audio", default="")
    parser.add_argument("--format", choices=list(SIZES), default="f45")
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--stills", default="", help="comma-separated times: write PNG stills instead of a video")
    parser.add_argument("--fps", type=int, default=0, help="output frame rate (default: timeline fps)")
    args = parser.parse_args()

    timeline = Path(args.timeline)
    data = json.loads(timeline.read_text(encoding="utf-8"))
    port = serve(Path(args.capture), timeline, Path(args.surfaces))
    out = Path(args.out)

    if args.stills:
        out.mkdir(parents=True, exist_ok=True)
        times = [float(x) for x in args.stills.split(",")]

        def save(t, png):
            (out / f"{args.format}_{t:06.2f}.png").write_bytes(png)

        asyncio.run(shoot(port, args.format, times, save))
        print(f"{len(times)} stills in {out}")
        return

    fps = args.fps or data["fps"]
    times = [i / fps for i in range(int(data["duration"] * fps))]
    chunk = -(-len(times) // args.workers)
    with tempfile.TemporaryDirectory() as tmp:
        parts = [Path(tmp) / f"part{i}.mkv" for i in range(args.workers)]
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                encode_segment(port, args.format, times[i * chunk:(i + 1) * chunk], fps, parts[i], delay=i * 2.0)
            except BaseException as exc:          # brak jednego odcinka = zly film; nie skladamy reszty
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise SystemExit(f"render failed: {errors[0]!r}")
        listing = Path(tmp) / "parts.txt"
        listing.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")
        audio = ["-i", args.audio] if args.audio else []
        # RGB -> BT.709 w ograniczonym zakresie, oznaczone w strumieniu: bez przesuniecia kolorow na telefonach
        command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), *audio,
                   "-vf", "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,format=yuv420p",
                   "-c:v", "libx264", "-preset", "slow", "-crf", "15", "-profile:v", "high", "-level", "4.2",
                   "-tune", "film", "-g", str(fps * 2), "-bf", "2",
                   "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
        if args.audio:
            command += ["-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-shortest"]
        command += ["-movflags", "+faststart", str(out)]
        subprocess.run(command, check=True)
    print(out)


if __name__ == "__main__":
    main()
