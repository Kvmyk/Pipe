"""
Nagrywa GIF z demo `pipe web` do README (docs/assets/demo-<lang>.gif).

Uruchamia atrape backendu (demo_backend.py — zaplanowany scenariusz, bez serwera i LLM), prawdziwy most
`pipe web` (clients/webui/server.py) i przegladarke headless (Playwright), wpisuje wiadomosc, klika TAK
i sklada klatki (CDP screencast, PNG) w GIF przez ffmpeg.

    pip install playwright            # przegladarka: zainstalowany Chrome (channel=chrome)
    python scripts/demo/record.py --lang pl
    python scripts/demo/record.py --lang en --backend-python /sciezka/do/python   # python z zaleznosciami backendu

Wymaga ffmpeg w PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MESSAGE = {
    "pl": "sklep.example.com co chwilę sypie 502, sprawdź co jest grane",
    "en": "shop.example.com keeps throwing 502s, find out what is going on",
}
DONE_TEXT = {"pl": "Naprawione", "en": "Fixed"}
VIEWPORT = {"width": 1360, "height": 800}


def wait_for_port(port: int, timeout: float = 30) -> None:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"demo backend did not start on port {port}")


def start_bridge(backend_port: int, lang: str) -> str:
    """Prawdziwy most `pipe web` w osobnym watku; zwraca adres strony z kluczem."""
    from clients.webui.server import WebBridge, free_port

    ready = threading.Event()
    holder: dict[str, str] = {}

    def run() -> None:
        bridge = WebBridge("127.0.0.1", backend_port, "", lang=lang, server_label="vps-shop" if lang == "en" else "vps-sklep", port=free_port(7400))
        holder["url"] = bridge.url
        ready.set()
        asyncio.run(bridge.serve(open_browser=False))

    threading.Thread(target=run, daemon=True).start()
    ready.wait(10)
    return holder["url"]


async def record(url: str, lang: str, frames_dir: Path, theme: str, failure: Path) -> list[tuple[Path, float]]:
    """Klatki PNG z CDP (Page.startScreencast) — ostry tekst; zwraca [(plik, czas)]."""
    import base64

    from playwright.async_api import async_playwright

    frames: list[tuple[Path, float]] = []
    recording = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(viewport=VIEWPORT, color_scheme=theme, device_scale_factor=1)
        await context.add_init_script(f"try {{ localStorage.setItem('pipe-theme', '{theme}'); }} catch (_) {{}}")
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)

        async def on_frame(params: dict) -> None:
            await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            if recording:
                path = frames_dir / f"{len(frames):05d}.png"
                path.write_bytes(base64.b64decode(params["data"]))
                frames.append((path, time.monotonic()))

        cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(on_frame(params)))
        await page.goto(url)
        await page.wait_for_selector("#nodes g", timeout=15000)       # schemat narysowany
        await page.wait_for_timeout(1200)
        await cdp.send("Page.startScreencast", {"format": "png", "everyNthFrame": 1,
                                                "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"]})
        recording = True
        await page.wait_for_timeout(1200)
        try:
            await page.click("#input")
            await page.keyboard.type(MESSAGE[lang], delay=32)
            await page.wait_for_timeout(500)
            await page.keyboard.press("Enter")
            await page.wait_for_selector(".confirm .btn.yes", timeout=30000)
            await page.wait_for_timeout(3000)                           # czas na przeczytanie planu
            await page.hover(".confirm .btn.yes")
            await page.wait_for_timeout(500)
            await page.click(".confirm .btn.yes")
            await page.wait_for_selector(f"text={DONE_TEXT[lang]}", timeout=30000)
            await page.wait_for_timeout(1500)
            await page.evaluate("document.getElementById('messages').scrollTo({top: 1e6, behavior: 'smooth'})")
            await page.wait_for_timeout(4500)
        except Exception:
            await page.screenshot(path=str(failure))
            print(f"nagranie przerwane — zrzut ekranu: {failure}", file=sys.stderr)
            raise
        recording = False
        frames.append((frames[-1][0], time.monotonic()))                # ostatnia klatka trwa do konca
        await cdp.send("Page.stopScreencast")
        await context.close()
        await browser.close()
    return frames


def to_gif(frames: list[tuple[Path, float]], out: Path, width: int, fps: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found in PATH")
    gaps = sorted(b - a for (_, a), (_, b) in zip(frames, frames[1:]))
    span = frames[-1][1] - frames[0][1]
    print(f"klatki: {len(frames)} w {span:.1f} s (srednio {len(frames) / span:.1f}/s, "
          f"przerwa mediana {gaps[len(gaps) // 2] * 1000:.0f} ms, 95% {gaps[int(len(gaps) * .95)] * 1000:.0f} ms)")
    listing = out.with_suffix(".frames.txt")
    lines = []
    for (path, at), (_, following) in zip(frames, frames[1:]):
        lines += [f"file '{path.as_posix()}'", f"duration {max(0.02, following - at):.3f}"]
    lines.append(f"file '{frames[-1][0].as_posix()}'")
    listing.write_text("\n".join(lines), encoding="utf-8")
    filters = f"fps={fps},scale={width}:-1:flags=lanczos"
    palette = out.with_suffix(".palette.png")
    source = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", *source,
                    "-vf", f"{filters},palettegen=stats_mode=diff:max_colors=192", str(palette)], check=True)
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", *source, "-i", str(palette),
                    "-lavfi", f"{filters}[x];[x][1:v]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
                    "-loop", "0", str(out)], check=True)
    palette.unlink(missing_ok=True)
    listing.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Record the pipe web demo GIF")
    parser.add_argument("--lang", choices=["pl", "en"], default="pl")
    parser.add_argument("--theme", choices=["dark", "light"], default="dark")
    parser.add_argument("--port", type=int, default=7390)
    parser.add_argument("--backend-python", default=sys.executable,
                        help="python with backend requirements (openai) for demo_backend.py")
    parser.add_argument("--width", type=int, default=1100)
    parser.add_argument("--fps", type=int, default=25)   # 25 = 4 cs na klatke: najplynniej, co GIF odtwarza rowno
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    out = Path(args.out or ROOT / "docs" / "assets" / f"demo-{args.lang}.gif")
    out.parent.mkdir(parents=True, exist_ok=True)
    backend = subprocess.Popen([args.backend_python, str(Path(__file__).with_name("demo_backend.py")),
                                "--port", str(args.port), "--lang", args.lang], cwd=ROOT)
    try:
        wait_for_port(args.port)
        url = start_bridge(args.port, args.lang)
        with tempfile.TemporaryDirectory() as tmp:
            frames = asyncio.run(record(url, args.lang, Path(tmp), args.theme, out.with_suffix('.failure.png')))
            to_gif(frames, out, args.width, args.fps)
        print(f"{out} ({out.stat().st_size // 1024} KiB)")
    finally:
        backend.terminate()


if __name__ == "__main__":
    main()
