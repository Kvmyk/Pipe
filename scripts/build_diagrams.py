"""
Generuje grafiki koncepcyjne Pipe do docs/assets (strona, dokumentacja i README czytaja te same pliki):

    pipe-flow-<lang>.svg    rura miedzy Toba a serwerem: zawor, bezpiecznik, manometr, wymienny model
    pipe-valve-<lang>.svg   komendy rozchodza sie w klasyfikatorze na trzy drogi

    python scripts/build_diagrams.py

Tylko SVG z animacja CSS: GitHub w README nie uruchamia JavaScriptu, a CSS w pliku SVG (w przeciwienstwie
do SMIL) mozna zatrzymac przez prefers-reduced-motion. Ruch po krzywych to wyliczone klatki translate(), nie
offset-path, wiec dziala wszedzie tam, gdzie zwykla animacja. Stan bez animacji jest pelnym, statycznym rysunkiem:
kazdy element ma pozycje koncowa w `style`. Kolory na sztywno z palety site/style.css, tlo wlasne, bo README
bywa jasne. Tylko biblioteka standardowa.
"""

from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
LANGS = ("en", "pl")

BG, PANEL, SUNKEN, LINE, STEEL = "#0f1011", "#151618", "#1c1d20", "#2a2b2f", "#3a3b40"
INK, INK2, INK3 = "#ececea", "#b4b5b8", "#8a8c90"
BLUE, GREEN, AMBER, RED = "#8fb0ff", "#5cc68a", "#e0a64a", "#f08a86"
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace"

Point = tuple[float, float]

FLOW = {
    "en": {"title": "Pipe: the pipe between you and your server",
           "desc": "You and the server are joined by a pipe. Commands flow to the server, alerts flow back. "
                   "In the pipe sit a valve, a fuse and a gauge; a language model you can swap connects from above.",
           "you": "You", "you_sub": "CLI · web · Telegram",
           "server": "Your server", "server_sub": "services · containers",
           "model": "Language model", "model_sub": "Gemini, Claude, Ollama...",
           "valve": "Valve", "valve_sub": "runs / asks / refuses",
           "fuse": "Fuse", "fuse_sub": "backup, check, undo",
           "gauge": "Gauge", "gauge_sub": "watches, writes first",
           "down": "what you ask for", "up": "what the server tells you first"},
    "pl": {"title": "Pipe: rura między Tobą a serwerem",
           "desc": "Ty i serwer jesteście połączeni rurą. Polecenia płyną do serwera, alerty wracają. "
                   "W rurze są zawór, bezpiecznik i manometr; od góry podłączony jest wymienny model językowy.",
           "you": "Ty", "you_sub": "CLI · web · Telegram",
           "server": "Twój serwer", "server_sub": "usługi · kontenery",
           "model": "Model językowy", "model_sub": "Gemini, Claude, Ollama...",
           "valve": "Zawór", "valve_sub": "sam / po TAK / nigdy",
           "fuse": "Bezpiecznik", "fuse_sub": "kopia, kontrola, cofnij",
           "gauge": "Manometr", "gauge_sub": "pilnuje, pisze pierwszy",
           "down": "o co prosisz", "up": "co serwer mówi Ci pierwszy"},
}

VALVE = {
    "en": {"title": "Every command goes through the valve",
           "desc": "Commands reach the classifier and split three ways: a routine reload runs with a backup, "
                   "deleting data waits for your yes, a destructive command is refused.",
           "classifier": "classifier", "classifier_sub": "in code, not in the prompt",
           "ok": "runs now, with a backup", "wait": "waits for your yes", "no": "refused, always"},
    "pl": {"title": "Każda komenda przechodzi przez zawór",
           "desc": "Komendy docierają do klasyfikatora i rozchodzą się trzema drogami: rutynowe przeładowanie "
                   "rusza z kopią, usuwanie danych czeka na Twoje TAK, komenda niszcząca dostaje odmowę.",
           "classifier": "klasyfikator", "classifier_sub": "w kodzie, nie w prompcie",
           "ok": "rusza od razu, z kopią", "wait": "czeka na Twoje TAK", "no": "odmowa, zawsze"},
}


def text(x: float, y: float, value: str, cls: str = "", anchor: str = "start") -> str:
    return (f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{html.escape(value)}</text>')


def cubic(a: Point, b: Point, c: Point, d: Point, steps: int = 16) -> list[Point]:
    points = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        points.append((u**3 * a[0] + 3 * u * u * t * b[0] + 3 * u * t * t * c[0] + t**3 * d[0],
                       u**3 * a[1] + 3 * u * u * t * b[1] + 3 * u * t * t * c[1] + t**3 * d[1]))
    return points


def along(points: list[Point], samples: int = 24) -> list[Point]:
    """Punkty rownomiernie po dlugosci lamanej, z lagodnym startem i hamowaniem."""
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1] + ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** .5)
    result = []
    for i in range(samples + 1):
        t = i / samples
        want = lengths[-1] * t * t * (3 - 2 * t)
        n = next((k for k, v in enumerate(lengths) if v >= want), len(lengths) - 1)
        if n == 0:
            result.append(points[0])
            continue
        span = (lengths[n] - lengths[n - 1]) or 1
        f = (want - lengths[n - 1]) / span
        a, b = points[n - 1], points[n]
        result.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return result


def journey(name: str, points: list[Point], start: float, arrive: float, leave: float) -> str:
    """Klatki kapsuly: niewidoczna do `start`, jedzie do `arrive`, stoi do `leave`, znika."""
    frames = [(0.0, points[0], 0)]
    if start > 0:
        frames.append((start, points[0], 0))
    for i, p in enumerate(points):
        frames.append((start + 0.5 + (arrive - start - 0.5) * i / (len(points) - 1), p, 1))
    frames += [(leave, points[-1], 1), (leave + 3, points[-1], 0), (100.0, points[-1], 0)]
    body = "".join(f"{pct:.2f}%{{transform:translate({p[0]:.1f}px,{p[1]:.1f}px);opacity:{o}}}" for pct, p, o in frames)
    return f"@keyframes {name}{{{body}}}"


def shell(width: int, height: int, title: str, desc: str, css: str, body: str) -> str:
    grid = "".join(f'<path d="M{x} 0V{height}"/>' for x in range(40, width, 40)) + \
        "".join(f'<path d="M0 {y}H{width}"/>' for y in range(40, height, 40))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-labelledby="t d">
<title id="t">{html.escape(title)}</title>
<desc id="d">{html.escape(desc)}</desc>
<style>
text{{font-family:{MONO};fill:{INK2};font-size:13px}}
.h{{fill:{INK};font-size:17px;font-weight:600}}
.n{{fill:{INK};font-size:15px;font-weight:600}}
.s{{fill:{INK3};font-size:12.5px}}
{css}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}}}
</style>
<rect width="{width}" height="{height}" rx="14" fill="{BG}"/>
<g stroke="#ffffff" stroke-opacity=".035">{grid}</g>
<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="13.5" fill="none" stroke="{LINE}"/>
{body}
</svg>
"""


def box(x: float, y: float, w: float, h: float, name: str, sub: str) -> str:
    cx = x + w / 2
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{PANEL}" stroke="{STEEL}"/>'
            + text(cx, y + h / 2 - 2, name, "n", "middle") + text(cx, y + h / 2 + 18, sub, "s", "middle"))


def flow(lang: str) -> str:
    t = FLOW[lang]
    w, h = 960, 370
    left, right = 196, 764            # koniec skrzynki "Ty", poczatek skrzynki serwera
    top, bottom = 170, 230            # scianki rury
    lane_down, lane_up = 186, 214     # pasy pakietow
    stations = (260, 445, 630)
    tap = 715                         # przylacze modelu
    duration = 6

    css = [f"@keyframes down{{from{{transform:translate({left}px,{lane_down}px)}}to{{transform:translate({right}px,{lane_down}px)}}}}",
           f"@keyframes up{{from{{transform:translate({right}px,{lane_up}px)}}to{{transform:translate({left}px,{lane_up}px)}}}}",
           f".d{{animation:down {duration}s linear infinite}}",
           f".u{{animation:up {duration}s linear infinite}}",
           # zawor: pol obrotu, chwila, drugie pol
           "@keyframes turn{0%,35%{transform:rotate(0)}50%,85%{transform:rotate(90deg)}100%{transform:rotate(180deg)}}",
           f".wheel{{transform-origin:{stations[0]}px 200px;animation:turn 6s ease-in-out infinite}}",
           "@keyframes glow{0%,100%{opacity:.35}50%{opacity:1}}",
           ".wire{animation:glow 3s ease-in-out infinite}",
           "@keyframes needle{0%,100%{transform:rotate(-38deg)}30%{transform:rotate(24deg)}55%{transform:rotate(-8deg)}80%{transform:rotate(44deg)}}",
           f".needle{{transform-origin:{stations[2]}px 208px;transform:rotate(10deg);animation:needle 7s ease-in-out infinite}}",
           f"@keyframes tap{{0%,100%{{transform:translate({tap}px,92px)}}50%{{transform:translate({tap}px,{top - 2}px)}}}}",
           ".m{animation:tap 5s ease-in-out infinite}"]

    packets = []
    for i in range(3):
        delay = -duration * i / 3
        x = left + (right - left) * (i + .5) / 3
        packets.append(f'<circle class="d" r="5" fill="{BLUE}" style="transform:translate({x:.0f}px,{lane_down}px);animation-delay:{delay:.1f}s"/>')
        packets.append(f'<circle class="u" r="5" fill="{AMBER}" style="transform:translate({x:.0f}px,{lane_up}px);animation-delay:{delay - 1:.1f}s"/>')

    cx1, cx2, cx3 = stations
    y = (top + bottom) / 2
    body = [text(32, 50, t["title"], "h"),
            # przylacze modelu pod rura, zeby kropka wchodzila w rure
            f'<path d="M{tap} 92V{top}" stroke="{INK3}" stroke-width="2" stroke-dasharray="3 6"/>',
            f'<circle class="m" r="4" fill="{INK}" style="transform:translate({tap}px,130px)"/>',
            box(tap - 112, 32, 224, 60, t["model"], t["model_sub"]),
            # rura
            f'<rect x="{left}" y="{top}" width="{right - left}" height="{bottom - top}" fill="{SUNKEN}"/>',
            f'<path d="M{left} {top}H{right}M{left} {bottom}H{right}" stroke="{STEEL}" stroke-width="2"/>',
            f'<path d="M{left} {y}H{right}" stroke="{LINE}" stroke-dasharray="2 8"/>',
            *packets,
            box(32, 150, left - 32, 100, t["you"], t["you_sub"]),
            box(right, 150, w - 32 - right, 100, t["server"], t["server_sub"])]
    for cx, name, sub in ((cx1, t["valve"], t["valve_sub"]), (cx2, t["fuse"], t["fuse_sub"]), (cx3, t["gauge"], t["gauge_sub"])):
        body += [f'<circle cx="{cx}" cy="{y}" r="44" fill="{PANEL}" stroke="{STEEL}" stroke-width="2"/>',
                 text(cx, 136, name, "n", "middle"), text(cx, 272, sub, "s", "middle")]
    # zawor: pokretlo
    body.append(f'<g class="wheel" fill="none" stroke="{GREEN}" stroke-width="4"><circle cx="{cx1}" cy="{y}" r="24"/>'
                f'<path d="M{cx1 - 24} {y}H{cx1 + 24}M{cx1} {y - 24}V{y + 24}"/></g>')
    # bezpiecznik: wklad z drucikiem
    body.append(f'<g fill="none" stroke="{AMBER}" stroke-width="3"><rect x="{cx2 - 28}" y="{y - 13}" width="56" height="26" rx="13"/>'
                f'<path d="M{cx2 - 38} {y}h10M{cx2 + 28} {y}h10"/>'
                f'<path class="wire" d="M{cx2 - 28} {y}c8 -14 14 14 28 0s20 14 28 0" stroke-width="2.5"/></g>')
    # manometr: tarcza i wskazowka
    body.append(f'<g fill="none" stroke="{BLUE}" stroke-width="3"><path d="M{cx3 - 28} {y + 14}a30 30 0 1 1 56 0"/>'
                f'<path d="M{cx3 - 28} {y + 14}l6 -3M{cx3 + 28} {y + 14}l-6 -3M{cx3} {y - 22}v6"/>'
                f'<path class="needle" d="M{cx3} {y + 8}V{y - 18}" stroke="{INK}"/></g>'
                f'<circle cx="{cx3}" cy="{y + 8}" r="4" fill="{INK}"/>')
    # legenda
    body += [f'<circle cx="202" cy="324" r="5" fill="{BLUE}"/>', text(216, 329, t["down"], "s"),
             f'<circle cx="470" cy="324" r="5" fill="{AMBER}"/>', text(484, 329, t["up"], "s")]
    return shell(w, h, t["title"], t["desc"], "\n".join(css), "\n".join(body))


def capsule(cls: str, label: str, fill: str, width: float, end: Point) -> str:
    return (f'<g class="{cls}" style="transform:translate({end[0]:.0f}px,{end[1]:.0f}px)">'
            f'<rect x="{-width / 2}" y="-14" width="{width}" height="28" rx="6" fill="{fill}"/>'
            f'<text y="5" text-anchor="middle" style="fill:#fff;font-size:14px">{html.escape(label)}</text></g>')


def valve(lang: str) -> str:
    t = VALVE[lang]
    w, h = 960, 340
    node: Point = (330, 186)
    upper, middle, lower = 78, 186, 294
    pipe_width = 34

    # drogi kapsul
    trunk = [(-110, node[1]), node]
    up_end, mid_end, low_end = (770, upper), (514, middle), (540, lower)
    up = along(trunk + cubic(node, (420, node[1]), (390, upper), (500, upper))[1:] + [up_end])
    mid = along(trunk + [mid_end])
    low = along(trunk + cubic(node, (420, node[1]), (390, lower), (500, lower))[1:] + [low_end])

    css = [journey("c1", up, 0, 26, 30), journey("c2", mid, 33, 52, 62), journey("c3", low, 66, 86, 92),
           ".c1{animation:c1 12s linear infinite}", ".c2{animation:c2 12s linear infinite}", ".c3{animation:c3 12s linear infinite}",
           # lampka klasyfikatora: kolor drogi, ktora wlasnie wybral
           f"@keyframes lamp{{0%,10%{{stroke:{INK3}}}14%,31%{{stroke:{GREEN}}}34%,43%{{stroke:{INK3}}}47%,64%{{stroke:{AMBER}}}67%,76%{{stroke:{INK3}}}80%,96%{{stroke:{RED}}}100%{{stroke:{INK3}}}}}",
           f".lamp{{stroke:{INK};animation:lamp 12s linear infinite}}",
           "@keyframes gate{0%,50%,63%,100%{opacity:.45}54%,60%{opacity:1}}", ".gate{animation:gate 12s linear infinite}",
           "@keyframes stop{0%,84%,93%,100%{opacity:.45}86%,91%{opacity:1}}", ".stop{animation:stop 12s linear infinite}"]

    pipes = (f'<g fill="none" stroke="{SUNKEN}" stroke-width="{pipe_width}">'
             f'<path d="M0 {node[1]}H{node[0]}"/>'
             f'<path d="M{node[0]} {node[1]}C420 {node[1]} 390 {upper} 500 {upper}H960"/>'
             f'<path d="M{node[0]} {node[1]}H640"/>'
             f'<path d="M{node[0]} {node[1]}C420 {node[1]} 390 {lower} 500 {lower}H654"/></g>')
    body = [text(32, 50, t["title"], "h"), pipes,
            capsule("c1", "systemctl reload nginx", "#1d7f49", 222, up_end),
            capsule("c2", "rm -r reports/2025", "#9a5a08", 186, mid_end),
            capsule("c3", "rm -rf /", "#b02b28", 104, low_end),
            f'<circle cx="{node[0]}" cy="{node[1]}" r="32" fill="{PANEL}" stroke-width="3" class="lamp"/>',
            f'<path d="M{node[0] - 16} {node[1]}H{node[0] + 16}M{node[0]} {node[1] - 16}V{node[1] + 16}" stroke="{INK}" stroke-width="3"/>',
            text(node[0] + 10, node[1] + 56, t["classifier"], "n", "end"),
            text(node[0] + 10, node[1] + 76, t["classifier_sub"], "s", "end"),
            text(928, upper - 28, t["ok"], "", "end").replace('class=""', f'style="fill:{GREEN}"'),
            f'<rect class="gate" x="620" y="{middle - 26}" width="10" height="52" rx="3" fill="{AMBER}"/>',
            text(652, middle + 5, t["wait"]).replace('class=""', f'style="fill:{AMBER}"'),
            f'<path class="stop" d="M614 {lower - 14}l28 28M642 {lower - 14}l-28 28" stroke="{RED}" stroke-width="5"/>',
            text(672, lower + 5, t["no"]).replace('class=""', f'style="fill:{RED}"')]
    return shell(w, h, t["title"], t["desc"], "\n".join(css), "\n".join(body))


def files() -> dict[Path, str]:
    result = {}
    for lang in LANGS:
        result[ASSETS / f"pipe-flow-{lang}.svg"] = flow(lang)
        result[ASSETS / f"pipe-valve-{lang}.svg"] = valve(lang)
    return result


if __name__ == "__main__":
    for path, content in files().items():
        path.write_text(content, encoding="utf-8", newline="\n")
        print(path.relative_to(ROOT).as_posix())
