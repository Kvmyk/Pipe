"""
Nagrywa prawdziwy interfejs `pipe web` do reklamy: klatki JPEG w 3x (ostre zblizenia na telefonie) + znaczniki
czasu zdarzen (klawisze, wyslanie, kolejne kroki agenta, karty) i polozenie elementow do ruchu kamery.

Chrome w trybie headless robi tylko kilka ostrych klatek 3x na sekunde, wiec scenariusz idzie w zwolnionym tempie
(--slow K): backend czeka K razy dluzej, zegar strony (Playwright clock: timery, rAF, Date) i animacje CSS
(CDP Animation.setPlaybackRate) ida K razy wolniej, a klawisze sa wciskane K razy rzadziej. Czasy w plikach sa juz
przeliczone na czas scenariusza (prawdziwy czas / K).

    python scripts/promo/capture.py --out <dir> [--slow 10] [--scale 3]

Wynik: <dir>/<take>/NNNNN.jpg, <dir>/<take>/take.json  (frames: [[plik, t]], marks: [{name, t, rect?}], size)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "demo"))

import record  # noqa: E402  (wait_for_port, start_bridge)

# Waskie okno w ukladzie desktopowym (>900 px): czat ma ~480 px szerokosci, wiec zblizenie w pionowym kadrze 1080 px
# daje tekst ~30 px — czytelny na telefonie.
VIEWPORT = {"width": 960, "height": 800}
FIRST = "shop.example.com keeps throwing 502s, find out what is going on"
SECOND = "and delete the old reports from /srv/shop/reports"
NIGHT = "2026-10-25T03:41:07"          # zegar strony: godziny w osi czasu agenta pasuja do historii o 3 w nocy

# Obserwator DOM: kazdy nowy krok agenta, wiadomosc i karta zglasza sie do Pythona (prawdziwy czas nadejscia).
OBSERVER = """
(() => {
  const seen = new WeakSet();
  const rect = (node) => { const r = node.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
  const report = () => {
    for (const [sel, name] of [['#messages .msg, #messages .turn, #messages > *', 'msg'], ['.confirm', 'confirm'],
                               ['.fuse', 'fuse'], ['.step, .steps li, .act', 'step']]) {
      document.querySelectorAll(sel).forEach((node) => {
        if (seen.has(node)) return;
        seen.add(node);
        window.__mark(name, rect(node), (node.className || '') + '', (node.textContent || '').slice(0, 120));
      });
    }
  };
  new MutationObserver(report).observe(document.body, {childList: true, subtree: true});
  report();
})();
"""


# Polozenie elementow, za ktorymi idzie kamera (stage.js): ostatnia wymiana w czacie, pierwsza odpowiedz agenta
# w niej (np. "Fixed — ..."), pole wpisywania, karta zgody.
TRACK = """
(() => {
  const box = (nodes) => {
    const rs = nodes.filter(Boolean).map((n) => n.getBoundingClientRect()).filter((r) => r.height > 0);
    if (!rs.length) return null;
    const x = Math.min(...rs.map((r) => r.left)), y = Math.min(...rs.map((r) => r.top));
    return [x, y, Math.max(...rs.map((r) => r.right)) - x, Math.max(...rs.map((r) => r.bottom)) - y];
  };
  const all = [...document.querySelectorAll('#messages > *')];
  const users = all.filter((n) => n.matches('.msg.user'));
  const last = users[users.length - 1];
  const turn = last ? all.slice(all.indexOf(last)) : [];
  const confirm = [...document.querySelectorAll('.confirm')].pop();
  const input = document.getElementById('input');
  const reply = turn.find((n) => n.matches('.msg.agent') && !n.matches('.steps-msg'));
  return {turn: box(turn), reply: box([reply]), input: box([input && (input.closest('form') || input.parentElement)]), confirm: box([confirm])};
})()
"""


class Take:
    def __init__(self, name: str, out: Path, slow: float):
        self.name, self.dir, self.slow = name, out / name, slow
        self.dir.mkdir(parents=True, exist_ok=True)
        for old in self.dir.glob("*.jpg"):
            old.unlink()
        self.frames: list[list] = []
        self.marks: list[dict] = []
        self.start = 0.0
        self.wall = 0.0
        self.recording = False

    def now(self) -> float:
        return (time.monotonic() - self.start) / self.slow

    def mark(self, name: str, rect=None, **extra) -> None:
        if self.recording:
            entry = {"name": name, "t": round(self.now(), 4), **extra}
            if rect is not None:
                entry["rect"] = [round(v, 1) for v in rect]
            self.marks.append(entry)

    def save(self, scale: float) -> None:
        self.frames.sort(key=lambda f: f[1])
        data = {"name": self.name, "scale": scale, "viewport": VIEWPORT, "frames": self.frames, "marks": self.marks}
        (self.dir / "take.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        span = self.frames[-1][1] - self.frames[0][1] if self.frames else 0
        print(f"{self.name}: {len(self.frames)} frames, {span:.1f} s of scenario ({len(self.frames) / max(span, .01):.1f}/s)")


async def rect(page, selector: str):
    return await page.evaluate("(s) => { const n = [...document.querySelectorAll(s)].pop(); if (!n) return null;"
                               " const r = n.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }", selector)


async def session(url: str, take: Take, scale: float, script) -> None:
    from playwright.async_api import async_playwright

    slow = take.slow
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True, args=[f"--force-device-scale-factor={scale}"])
        context = await browser.new_context(viewport=VIEWPORT, color_scheme="dark")
        await context.add_init_script("try { localStorage.setItem('pipe-theme', 'dark'); } catch (_) {}")
        page = await context.new_page()
        await page.clock.install(time=NIGHT)
        await page.expose_function("__mark", lambda name, r, cls, text: take.mark(name, r, cls=cls, text=text))
        cdp = await context.new_cdp_session(page)
        await cdp.send("Animation.enable")

        async def on_frame(params: dict) -> None:
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:
                return
            if take.recording:
                # czas narysowania klatki w przegladarce, nie przyjscia do Pythona: duze klatki 3x przychodza
                # z kilkusekundowym opoznieniem i z czasem przyjscia obraz spoznial sie wzgledem klawiszy
                drawn = params.get("metadata", {}).get("timestamp")
                at = (drawn - take.wall) / take.slow if drawn else take.now()
                if at < 0:
                    return
                path = take.dir / f"{len(take.frames):05d}.jpg"
                path.write_bytes(base64.b64decode(params["data"]))
                take.frames.append([path.name, round(at, 4)])

        cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(on_frame(params)))

        ticking = True

        async def track() -> None:
            while ticking:
                await asyncio.sleep(0.05 * slow)
                if take.recording:
                    try:
                        take.mark("track", boxes=await page.evaluate(TRACK))
                    except Exception:
                        return

        async def tick() -> None:
            # zegar strony: przed nagraniem idzie normalnie (schemat zdazy sie ulozyc), w trakcie K razy wolniej
            step = 40
            while ticking:
                await asyncio.sleep(step / 1000)
                try:
                    await page.clock.run_for(max(1, round(step / slow)) if take.recording else step)
                except Exception:
                    return

        await page.goto(url)
        ticker = asyncio.ensure_future(tick())
        tracker = asyncio.ensure_future(track())
        try:
            await script(page, take, cdp, start_recording=lambda: begin(page, cdp, take, scale))
        except Exception:
            await page.screenshot(path=str(take.dir / "failure.png"))
            raise
        finally:
            ticking = False
            take.recording = False
            await ticker
            await tracker
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            await context.close()
            await browser.close()


async def begin(page, cdp, take: Take, scale: float) -> None:
    await cdp.send("Animation.setPlaybackRate", {"playbackRate": 1 / take.slow})
    await page.evaluate(OBSERVER)              # CSP strony nie pozwala na <script> inline; evaluate idzie przez CDP
    take.start = time.monotonic()
    take.wall = time.time()
    take.recording = True
    await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 93, "everyNthFrame": 1,
                                            "maxWidth": int(VIEWPORT["width"] * scale),
                                            "maxHeight": int(VIEWPORT["height"] * scale)})


async def wait(take: Take, seconds: float) -> None:
    await asyncio.sleep(seconds * take.slow)


async def type_text(page, take: Take, text: str, cps: float = 17.0) -> None:
    """Pisanie jak czlowiek: rowne tempo z lekkimi przerwami po spacjach i przecinkach; kazdy klawisz to znacznik."""
    import random
    rnd = random.Random(7)
    for ch in text:
        take.mark("key", char=ch)
        await page.keyboard.type(ch)
        gap = 1 / cps * rnd.uniform(0.75, 1.25)
        if ch in " ,/":
            gap += rnd.uniform(0.03, 0.09)
        await wait(take, gap)


async def chat_script(page, take: Take, cdp, start_recording) -> None:
    await page.evaluate("document.getElementById('input').spellcheck = false")   # bez czerwonych podkreslen
    await page.click("#st-map", timeout=15000)
    await page.wait_for_selector("#nodes g", timeout=15000)
    await asyncio.sleep(3)
    await start_recording()
    await wait(take, 0.8)
    take.mark("composer", await rect(page, "#input"))
    await page.click("#input")
    await wait(take, 0.3)
    take.mark("type1")
    await type_text(page, take, FIRST)
    await wait(take, 0.35)
    take.mark("send1", await rect(page, "#input"))
    await page.keyboard.press("Enter")
    await page.wait_for_selector("text=Fixed", timeout=60000 * take.slow)
    take.mark("fixed")
    await wait(take, 2.5)
    take.mark("type2", await rect(page, "#input"))
    await page.click("#input")
    await type_text(page, take, SECOND)
    await wait(take, 0.35)
    take.mark("send2")
    await page.keyboard.press("Enter")
    await page.wait_for_selector(".confirm", timeout=60000 * take.slow)
    take.mark("asked", await rect(page, ".confirm"))
    await wait(take, 4.0)
    take.mark("end")


async def dashboard_script(page, take: Take, cdp, start_recording) -> None:
    await page.wait_for_selector("#home", timeout=15000)
    await asyncio.sleep(1)
    await start_recording()
    await wait(take, 0.5)
    await page.evaluate("location.hash = ''")
    await page.click("#st-home")
    await wait(take, 1.5)
    take.mark("home", await rect(page, "#home"))
    await wait(take, 4.0)
    take.mark("end")


async def inbox_script(page, take: Take, cdp, start_recording) -> None:
    """Pulpit przewiniety do wiadomosci, ktora Pipe napisal sam w nocy ("What I found: ... 03:41")."""
    await page.wait_for_selector("#home", timeout=15000)
    await page.click("#st-home")
    await asyncio.sleep(1.5)
    await page.evaluate("""() => {
      const node = [...document.querySelectorAll('#home *')].find((n) => n.children.length === 0 && /What I found/.test(n.textContent));
      const card = node && (node.closest('article, li, section, .item, .feed-item') || node.parentElement.parentElement);
      if (card) card.scrollIntoView({block: 'start'});
    }""")
    await asyncio.sleep(1.0)
    await start_recording()
    await wait(take, 0.5)
    take.mark("night", await page.evaluate("""() => {
      const node = [...document.querySelectorAll('#home *')].find((n) => n.children.length === 0 && /What I found/.test(n.textContent));
      if (!node) return null; const r = node.getBoundingClientRect(); return [r.x, r.y, r.width, r.height];
    }"""))
    await wait(take, 3.0)
    take.mark("end")


def run_take(name: str, script, out: Path, slow: float, scale: float, port: int, full: bool) -> None:
    command = [sys.executable, str(Path(__file__).with_name("promo_backend.py")), "--port", str(port),
               "--lang", "en", "--slow", str(slow)] + (["--full"] if full else [])
    backend = subprocess.Popen(command, cwd=ROOT)
    try:
        record.wait_for_port(port)
        url = record.start_bridge(port, "en")
        take = Take(name, out, slow)
        asyncio.run(session(url, take, scale, script))
        take.save(scale)
    finally:
        backend.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture pipe web for the promo video")
    parser.add_argument("--out", required=True)
    parser.add_argument("--slow", type=float, default=10.0)
    parser.add_argument("--scale", type=float, default=3.0)
    parser.add_argument("--take", choices=["chat", "dashboard", "inbox", "all"], default="all")
    args = parser.parse_args()
    out = Path(args.out)
    if args.take in ("chat", "all"):
        run_take("chat", chat_script, out, args.slow, args.scale, 7395, False)
    if args.take in ("dashboard", "all"):
        run_take("dashboard", dashboard_script, out, args.slow, args.scale, 7396, True)
    if args.take in ("inbox", "all"):
        run_take("inbox", inbox_script, out, args.slow, args.scale, 7397, True)


if __name__ == "__main__":
    main()
