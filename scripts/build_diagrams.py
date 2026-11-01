"""
Generuje grafiki koncepcyjne Pipe do docs/assets (strona, dokumentacja i README czytaja te same pliki):

    pipe-flow-<lang>.svg    jedna historia w rurze: pytanie -> model proponuje -> zawor -> bezpiecznik -> serwer -> wynik
    pipe-valve-<lang>.svg   komendy rozchodza sie w klasyfikatorze na trzy drogi

    python scripts/build_diagrams.py

Tylko SVG z animacja CSS: GitHub w README nie uruchamia JavaScriptu, a CSS w pliku SVG (w przeciwienstwie
do SMIL) mozna zatrzymac przez prefers-reduced-motion. Ruch to wyliczone klatki translate(), nie offset-path,
wiec dziala wszedzie tam, gdzie zwykla animacja. Wszystko w jednym pliku idzie na jednej osi czasu CYCLE: chwile
w procentach cyklu sa zapisane raz, a kapsuly i lampki czytaja te same liczby. Stan bez animacji jest pelnym,
statycznym rysunkiem: kazdy element ma pozycje w `style`. Kolory na sztywno z palety site/style.css, tlo wlasne,
bo README bywa jasne. Tylko biblioteka standardowa.
"""

from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
LANGS = ("en", "pl")
CYCLE = 12                            # sekundy, ten sam cykl na obu rysunkach

BG, PANEL, SUNKEN, LINE, STEEL = "#17181a", "#1e1f22", "#26272b", "#34353a", "#4a4b51"
INK, INK2, INK3 = "#ececea", "#c4c5c6", "#9a9c9f"
TEAL, GREEN, AMBER, RED = "#7cc7b6", "#5cc68a", "#e0a64a", "#f08a86"
DEEP_TEAL, DEEP_GREEN, DEEP_AMBER, DEEP_RED = "#1c6f63", "#1d7f49", "#9a5a08", "#b02b28"
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace"

Point = tuple[float, float]
Stop = tuple[float, Point]

FLOW = {
    "en": {"title": "Pipe: the pipe between you and your server",
           "desc": "Your question goes up to a language model, which proposes a command. The command goes through "
                   "the valve and the fuse to the server, and the result comes back past the gauge to you.",
           "you": "You", "you_sub": "CLI, web, Telegram",
           "server": "Your server", "server_sub": "services, containers",
           "model": "Language model", "model_sub": "proposes commands, swappable",
           "valve": "Valve", "valve_sub": "runs, asks, refuses",
           "fuse": "Fuse", "fuse_sub": "backup and undo",
           "gauge": "Gauge", "gauge_sub": "writes first",
           "down": "your request", "up": "result or alert"},
    "pl": {"title": "Pipe: rura między Tobą a serwerem",
           "desc": "Twoje pytanie idzie do modelu językowego, który proponuje komendę. Komenda przechodzi przez zawór "
                   "i bezpiecznik do serwera, a wynik wraca obok czujnika do Ciebie.",
           "you": "Ty", "you_sub": "CLI, web, Telegram",
           "server": "Twój serwer", "server_sub": "usługi, kontenery",
           "model": "Model językowy", "model_sub": "proponuje komendy, wymienny",
           "valve": "Zawór", "valve_sub": "sam, po TAK, nigdy",
           "fuse": "Bezpiecznik", "fuse_sub": "kopia i cofnięcie",
           "gauge": "Czujnik", "gauge_sub": "pisze pierwszy",
           "down": "Twoja prośba", "up": "wynik albo alert"},
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


def text(x: float, y: float, value: str, cls: str = "", anchor: str = "start", color: str = "") -> str:
    style = f' style="fill:{color}"' if color else ""
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}"{style}>{html.escape(value)}</text>'


def cubic(a: Point, b: Point, c: Point, d: Point, steps: int = 16) -> list[Point]:
    points = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        points.append((u**3 * a[0] + 3 * u * u * t * b[0] + 3 * u * t * t * c[0] + t**3 * d[0],
                       u**3 * a[1] + 3 * u * u * t * b[1] + 3 * u * t * t * c[1] + t**3 * d[1]))
    return points


def along(points: list[Point], samples: int = 16) -> list[Point]:
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


def spread(start: float, end: float, points: list[Point]) -> list[Stop]:
    return [(start + (end - start) * i / (len(points) - 1), p) for i, p in enumerate(points)]


def track(name: str, stops: list[Stop], ease: bool = True) -> str:
    """Klatki kapsuly: pojawia sie w pierwszym przystanku, jedzie przez kolejne, znika po ostatnim."""
    first, last = stops[0], stops[-1]
    timing = "animation-timing-function:ease-in-out" if ease else "animation-timing-function:linear"
    frames = [(0.0, first[1], 0), (first[0], first[1], 0), (first[0] + .6, first[1], 1)]
    frames += [(pct, p, 1) for pct, p in stops[1:]]
    frames += [(last[0] + 2, last[1], 0), (100.0, last[1], 0)]
    body = "".join(f"{pct:.2f}%{{transform:translate({p[0]:.1f}px,{p[1]:.1f}px);opacity:{o};{timing}}}"
                   for pct, p, o in frames)
    return f"@keyframes {name}{{{body}}}"


def windows(name: str, prop: str, off: str, items: list[tuple[float, float, str]]) -> str:
    """Wartosc `prop` w oknach czasu (start, koniec, wartosc); poza nimi `off`."""
    frames = [(0.0, off)]
    for start, end, value in items:
        frames += [(start - 1.5, off), (start, value), (end, value), (end + 2, off)]
    frames.append((100.0, off))
    return f"@keyframes {name}{{" + "".join(f"{pct:.2f}%{{{prop}:{value}}}" for pct, value in frames) + "}"


def run(cls: str, name: str | None = None, base: str = "") -> str:
    return f".{cls}{{{base}animation:{name or cls} {CYCLE}s linear infinite}}"


def shell(width: int, height: int, title: str, desc: str, css: str, body: str) -> str:
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
<defs>
<linearGradient id="wall" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#303136"/><stop offset=".5" stop-color="#1e1f22"/><stop offset="1" stop-color="#303136"/></linearGradient>
<linearGradient id="wallv" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#303136"/><stop offset=".5" stop-color="#1e1f22"/><stop offset="1" stop-color="#303136"/></linearGradient>
</defs>
<rect width="{width}" height="{height}" fill="{BG}"/>
{body}
</svg>
"""


def box(x: float, y: float, w: float, h: float, name: str, sub: str, cls: str) -> str:
    cx = x + w / 2
    return (f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{PANEL}" stroke="{STEEL}" stroke-width="2"/>'
            + text(cx, y + h / 2 - 3, name, "n", "middle") + text(cx, y + h / 2 + 17, sub, "s", "middle"))


def capsule(cls: str, label: str, fill: str, width: float, at: Point, size: int = 14, height: int = 28) -> str:
    return (f'<g class="{cls}" style="transform:translate({at[0]:.0f}px,{at[1]:.0f}px)">'
            f'<rect x="{-width / 2}" y="{-height / 2}" width="{width}" height="{height}" rx="6" fill="{fill}"/>'
            f'<text y="{size * .36:.1f}" text-anchor="middle" style="fill:#fff;font-size:{size}px">{html.escape(label)}</text></g>')


def flow(lang: str) -> str:
    t = FLOW[lang]
    w, h = 960, 370
    left, right = 196, 764            # koniec skrzynki "Ty", poczatek skrzynki serwera
    top, bottom = 170, 230            # scianki rury
    lane_down, lane_up = 186, 214     # pasy kapsul
    tap, tap_half = 265, 36           # odgalezienie do modelu, przed zaworem: model proponuje, zawor decyduje
    model_y = 92                      # dol skrzynki modelu
    cx1, cx2, cx3 = 390, 540, 690     # zawor, bezpiecznik, czujnik
    y = (top + bottom) / 2
    behind_you, behind_server = 150, 810

    # Jedna os czasu (% cyklu). Pytanie -> model -> komenda -> wynik.
    ask: list[Stop] = [(1, (behind_you, lane_down)), (8, (tap, lane_down)), (16, (tap, model_y - 12))]
    think = (16, 27)
    cmd: list[Stop] = [(27, (tap, model_y - 12)), (34, (tap, lane_down)), (40, (cx1, lane_down)), (45, (cx1, lane_down)),
                       (51, (cx2, lane_down)), (55, (cx2, lane_down)), (63, (behind_server, lane_down))]
    result: list[Stop] = [(68, (behind_server, lane_up)), (73, (cx3, lane_up)), (77, (cx3, lane_up)),
                          (85, (cx1, lane_up)), (96, (behind_you, lane_up))]

    css = [track("ask", ask), track("cmd", cmd), track("res", result),
           run("ask"), run("cmd"), run("res"),
           windows("lmodel", "stroke", STEEL, [(*think, TEAL)]), run("lmodel"),
           windows("lvalve", "stroke", STEEL, [(39, 46, GREEN)]), run("lvalve"),
           windows("lfuse", "stroke", STEEL, [(50, 56, AMBER)]), run("lfuse"),
           windows("lgauge", "stroke", STEEL, [(72, 78, TEAL)]), run("lgauge"),
           windows("lserver", "stroke", STEEL, [(62, 69, GREEN)]), run("lserver"),
           windows("lyou", "stroke", STEEL, [(2, 6, TEAL), (93, 97.5, AMBER)]), run("lyou"),
           windows("wire", "opacity", ".35", [(50, 56, "1")]), run("wire", base=f"opacity:.35;"),
           # zawor: cwierc obrotu przy przejsciu komendy (krzyz po obrocie wyglada tak samo, cykl sie nie urywa)
           "@keyframes turn{0%,39%{transform:rotate(0)}45%,100%{transform:rotate(90deg)}}",
           f".wheel{{transform-origin:{cx1}px {y}px;animation:turn {CYCLE}s ease-in-out infinite}}",
           # czujnik: wskazowka drgnie, gdy mija ja wynik
           "@keyframes needle{0%,71%{transform:rotate(-24deg)}74%{transform:rotate(34deg)}78%{transform:rotate(8deg)}100%{transform:rotate(-24deg)}}",
           f".needle{{transform-origin:{cx3}px {y + 8}px;transform:rotate(8deg);animation:needle {CYCLE}s ease-in-out infinite}}",
           # model mysli
           *(f"@keyframes dot{i}{{0%,{think[0] + i * 2}%,{think[1]}%,100%{{opacity:.15}}{think[0] + 2 + i * 2}%,{think[1] - 2}%{{opacity:1}}}}" for i in range(3))]
    css += [f".dot{i}{{opacity:.15;animation:dot{i} {CYCLE}s linear infinite}}" for i in range(3)]

    def fitting(x: float) -> str:
        return f'<rect x="{x - 3}" y="{top - 6}" width="6" height="{bottom - top + 12}" rx="2" fill="{STEEL}"/>'

    tx0, tx1 = tap - tap_half, tap + tap_half
    body = [text(928, 50, t["title"], "h", "end"),
            # odgalezienie do modelu i rura
            f'<rect x="{tx0}" y="{model_y}" width="{tap_half * 2}" height="{top - model_y + 2}" fill="url(#wallv)"/>',
            f'<rect x="{left}" y="{top}" width="{right - left}" height="{bottom - top}" fill="url(#wall)"/>',
            f'<path d="M{tx0} {model_y}V{top}H{left}M{tx1} {model_y}V{top}H{right}M{left} {bottom}H{right}" fill="none" stroke="{STEEL}" stroke-width="2"/>',
            capsule("ask", "502?", DEEP_TEAL, 54, (tap, 132), 12.5, 22),
            capsule("cmd", "reload", DEEP_TEAL, 64, (465, lane_down), 12.5, 22),
            capsule("res", "OK", DEEP_AMBER, 42, (615, lane_up), 12.5, 22),
            *(fitting(x) for x in (cx1 - 46, cx1 + 46, cx2 - 46, cx2 + 46, cx3 - 46, cx3 + 46)),
            box(tap - 130, 32, 260, 60, t["model"], t["model_sub"], "lmodel"),
            *(f'<circle class="dot{i}" cx="{tap + 92 + i * 11}" cy="56" r="3" fill="{TEAL}"/>' for i in range(3)),
            box(32, 150, left - 32, 100, t["you"], t["you_sub"], "lyou"),
            box(right, 150, w - 32 - right, 100, t["server"], t["server_sub"], "lserver")]
    for cx, name, sub, cls in ((cx1, t["valve"], t["valve_sub"], "lvalve"), (cx2, t["fuse"], t["fuse_sub"], "lfuse"),
                               (cx3, t["gauge"], t["gauge_sub"], "lgauge")):
        body += [f'<circle class="{cls}" cx="{cx}" cy="{y}" r="42" fill="{PANEL}" stroke="{STEEL}" stroke-width="2.5"/>',
                 text(cx, 142, name, "n", "middle"), text(cx, 270, sub, "s", "middle")]
    # zawor: pokretlo
    body.append(f'<g class="wheel" fill="none" stroke="{GREEN}" stroke-width="4"><circle cx="{cx1}" cy="{y}" r="24"/>'
                f'<path d="M{cx1 - 24} {y}H{cx1 + 24}M{cx1} {y - 24}V{y + 24}"/></g>')
    # bezpiecznik: wklad z drucikiem
    body.append(f'<g fill="none" stroke="{AMBER}" stroke-width="3"><rect x="{cx2 - 26}" y="{y - 13}" width="52" height="26" rx="13"/>'
                f'<path d="M{cx2 - 36} {y}h10M{cx2 + 26} {y}h10"/>'
                f'<path class="wire" d="M{cx2 - 26} {y}c8 -14 13 14 26 0s18 14 26 0" stroke-width="2.5"/></g>')
    # czujnik: tarcza i wskazowka
    body.append(f'<g fill="none" stroke="{TEAL}" stroke-width="3"><path d="M{cx3 - 26} {y + 14}a30 30 0 1 1 52 0"/>'
                f'<path class="needle" d="M{cx3} {y + 8}V{y - 18}" stroke="{INK}"/></g>'
                f'<circle cx="{cx3}" cy="{y + 8}" r="4" fill="{INK}"/>')
    # legenda
    body += [f'<rect x="196" y="316" width="14" height="14" rx="4" fill="{DEEP_TEAL}"/>', text(218, 327, t["down"], "s"),
             f'<rect x="430" y="316" width="14" height="14" rx="4" fill="{DEEP_AMBER}"/>', text(452, 327, t["up"], "s")]
    return shell(w, h, t["title"], t["desc"], "\n".join(css), "\n".join(body))


def valve(lang: str) -> str:
    t = VALVE[lang]
    w, h = 960, 340
    node: Point = (330, 186)
    upper, middle, lower = 78, 186, 294
    gate_x, stop_x = 620, 628

    start: Point = (-120, node[1])
    trunk = along([start, node], 10)
    up_end, mid_end, low_end = (770, upper), (510, middle), (540, lower)
    up = along(cubic(node, (420, node[1]), (390, upper), (500, upper)) + [up_end])
    mid = along([node, mid_end], 8)
    low = along(cubic(node, (420, node[1]), (390, lower), (500, lower)) + [low_end])

    # Trzy tury po 4 s. Kazda kapsula staje w klasyfikatorze, lampka pokazuje droge, potem kapsula rusza.
    def turn(begin: float, route: list[Point], arrive: float, hold: float) -> tuple[list[Stop], float, float]:
        at_node, leave = begin + 8, begin + 11
        return spread(begin, at_node, trunk) + spread(leave, arrive, route) + [(hold, route[-1])], at_node, hold

    c1, n1, end1 = turn(1, up, 24, 30)
    c2, n2, end2 = turn(34, mid, 52, 62)
    c3, n3, end3 = turn(67, low, 86, 94)

    css = [track("c1", c1, ease=False), track("c2", c2, ease=False), track("c3", c3, ease=False),
           run("c1"), run("c2"), run("c3"),
           windows("lamp", "stroke", STEEL, [(n1, end1, GREEN), (n2, end2, AMBER), (n3, end3, RED)]),
           run("lamp", base=f"stroke:{INK};"),
           windows("gate", "opacity", ".4", [(52, end2, "1")]), run("gate"),
           windows("stop", "opacity", ".4", [(86, end3, "1")]), run("stop"),
           windows("tok", "opacity", ".5", [(24, end1, "1")]), run("tok"),
           windows("twait", "opacity", ".5", [(52, end2, "1")]), run("twait"),
           windows("tno", "opacity", ".5", [(86, end3, "1")]), run("tno")]

    routes = (f'M0 {node[1]}H{node[0]}', f'M{node[0]} {node[1]}C420 {node[1]} 390 {upper} 500 {upper}H960',
              f'M{node[0]} {node[1]}H{gate_x + 10}', f'M{node[0]} {node[1]}C420 {node[1]} 390 {lower} 500 {lower}H{stop_x + 26}')
    pipes = (f'<g fill="none" stroke="{STEEL}" stroke-width="38">' + "".join(f'<path d="{d}"/>' for d in routes) + "</g>"
             f'<g fill="none" stroke="{SUNKEN}" stroke-width="34">' + "".join(f'<path d="{d}"/>' for d in routes) + "</g>")
    body = [text(32, 50, t["title"], "h"), pipes,
            capsule("c1", "systemctl reload nginx", DEEP_GREEN, 222, up_end),
            capsule("c2", "rm -r reports/2025", DEEP_AMBER, 186, mid_end),
            capsule("c3", "rm -rf /", DEEP_RED, 104, low_end),
            f'<circle class="lamp" cx="{node[0]}" cy="{node[1]}" r="34" fill="{PANEL}" stroke-width="3"/>',
            f'<path d="M{node[0] - 16} {node[1]}H{node[0] + 16}M{node[0]} {node[1] - 16}V{node[1] + 16}" stroke="{INK}" stroke-width="3"/>',
            text(node[0] + 10, node[1] + 58, t["classifier"], "n", "end"),
            text(node[0] + 10, node[1] + 78, t["classifier_sub"], "s", "end"),
            f'<g class="tok">{text(928, upper - 28, t["ok"], "", "end", GREEN)}</g>',
            f'<rect class="gate" x="{gate_x}" y="{middle - 28}" width="10" height="56" rx="3" fill="{AMBER}"/>',
            f'<g class="twait">{text(652, middle + 5, t["wait"], "", "start", AMBER)}</g>',
            f'<path class="stop" d="M{stop_x - 14} {lower - 14}l28 28M{stop_x + 14} {lower - 14}l-28 28" stroke="{RED}" stroke-width="5" stroke-linecap="round"/>',
            f'<g class="tno">{text(672, lower + 5, t["no"], "", "start", RED)}</g>']
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
