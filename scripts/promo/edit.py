"""
Montaz reklamy: sceny (strona z bledem, przegladarka, Telegram, terminal, duze slowa, plansza), ruch kamery, napisy,
dzwieki interfejsu i takty muzyki. Wynik — timeline.json — czytaja scena (stage/stage.js, obraz) i score.py (dzwiek),
wiec kazdy klawisz, dotkniecie i wiadomosc slychac dokladnie w klatce, w ktorej pojawia sie na ekranie.

Siatka: takty muzyki z synth.py (130 BPM, takt 1,846 s). Najpierw przypadek uzycia, potem pozostale sposoby pracy.
Ciecia na kreskach taktowych: "Fixed" = pierwsze wejscie (takt 5), karta zgody = cisza (takt 7), dotkniecie "Fix it"
w Telegramie = drugie wejscie (takt 10), plansza = koncowy akord (takt 17).

    python scripts/promo/edit.py --capture <dir> --out timeline.json
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from pathlib import Path

FPS = 60
BPM = 130                       # to samo tempo co synth.BPM
B = 240 / BPM                   # takt
BEAT = B / 4
END = 17 * B
DURATION = round(21 * B, 3)
# koniec pisania i klik w "Send": po ostatniej literze kursor czeka chwile, potem jedzie (ok. 0,6 s) i klika
TYPED1, CLICK1 = 2.6 * B, 3 * B
TYPED2, CLICK2 = 6.42 * B, 6.78 * B

# Sceny: (nazwa, poczatek, koniec, rodzaj, ujecie, [(czas filmu, punkt nagrania)], wejscie?)
# Punkt nagrania: (nazwa z anchors(), przesuniecie w sekundach) albo liczba (sekunda nagrania wprost).
SCENES = [
    ("hook", 0.0, 1 * B, "hook", None, [], True),
    ("ask", 1 * B, 3 * B, "web", "chat", [(1 * B, ("typing", 0)), (TYPED1, ("lastkey1", 0.04)), (3 * B, ("send1", 0))], True),
    # tuz po wyslaniu strona przez ok. 0,1 s przebudowuje sie (znika ekran powitalny) — te klatki pomijamy
    ("find", 3 * B, 4.25 * B, "web", "chat", [(3 * B, ("send1", 0.15)), (4.25 * B, ("fix", 0))], False),
    ("fuse", 4.25 * B, 5 * B, "web", "chat", [(4.25 * B, ("fix", 0)), (5 * B, ("fixed", -0.02))], False),
    ("fixed", 5 * B, 6 * B, "web", "chat", [(5 * B, ("fixed", -0.02)), (6 * B, ("fixed", 2.2))], False),
    ("risky", 6 * B, 7 * B, "web", "chat", [(6 * B, ("lastkey2", -0.9)), (TYPED2, ("lastkey2", 0.04)), (CLICK2, ("send2", 0)),
                                            (7 * B, ("confirm", 0))], True),
    ("asks", 7 * B, 8 * B, "web", "chat", [(7 * B, ("confirm", 0)), (8 * B, ("confirm", 1.8))], False),
    ("telegram", 8 * B, 11.5 * B, "phone", None, [], True),
    ("terminal", 11.5 * B, 13 * B, "term", None, [], True),
    ("ahead", 13 * B, 14 * B, "web", "dashboard", [(13 * B, 0.40), (13 * B + 0.35, 0.75), (14 * B, 2.0)], True),
    ("undo", 14 * B, 15 * B, "web", "chat", [(14 * B, ("type2", -1.2)), (15 * B, ("type2", -0.2))], True),
    ("map", 15 * B, 16 * B, "web", "chat", [(15 * B, ("fix", 0.2)), (16 * B, ("fix", 2.0))], True),
    ("night", 16 * B, 17 * B, "web", "inbox", [(16 * B, 0.0), (17 * B, 0.4)], True),
    ("end", END, DURATION, "none", None, [], False),
]

# Kamera w pikselach CSS okna 960x800: (czas, x, y, widoczna szerokosc); x/y moze sledzic element z nagrania.
WIDE = (480, 400, 1010)
CHAT_X = 238                    # srodek kolumny czatu (x 24-455); dymek uzytkownika jest wyrownany do prawej
TURN_FOLLOW = {"track": "turn", "edge": "follow", "pad": 18}
TURN_BOTTOM = {"track": "turn", "edge": "bottom", "pad": 26}
REPLY_X = {"track": "reply", "edge": "left", "pad": 14}
REPLY_TOP = {"track": "reply", "edge": "top", "pad": 22}
INPUT_X = {"track": "input", "edge": "left", "pad": 14}
INPUT = {"track": "input", "edge": "bottom", "pad": 26}
CARD_X = {"track": "confirm", "edge": "left", "pad": 12}
CARD_Y = {"track": "confirm", "edge": "center", "pad": 0}
CAMERA = {
    "ask": [(1 * B, *WIDE), (1 * B + 0.35, *WIDE), (1 * B + 1.2, INPUT_X, INPUT, 472), (3 * B, INPUT_X, INPUT, 465)],
    "find": [(3 * B, CHAT_X, TURN_FOLLOW, 450), (4.25 * B, CHAT_X, TURN_FOLLOW, 445)],
    "fuse": [(4.25 * B, CHAT_X, TURN_FOLLOW, 445), (4.25 * B + 0.45, 716, 700, 455), (5 * B, 716, 700, 445)],
    "fixed": [(5 * B, REPLY_X, REPLY_TOP, 452), (6 * B, REPLY_X, REPLY_TOP, 444)],
    "risky": [(6 * B, INPUT_X, INPUT, 472), (CLICK2, INPUT_X, INPUT, 472), (7 * B, CHAT_X, TURN_FOLLOW, 450)],
    "asks": [(7 * B, CARD_X, CARD_Y, 452), (8 * B, CARD_X, CARD_Y, 428)],
    "ahead": [(13 * B, 722, 330, 470), (14 * B, 722, 350, 452)],
    "undo": [(14 * B, CHAT_X, TURN_BOTTOM, 452), (15 * B, CHAT_X, TURN_BOTTOM, 442)],
    # schemat serwera: wezly Internet -> nginx -> ssh, agent podswietla to, nad czym pracuje
    "map": [(15 * B, 815, 352, 340), (16 * B, 815, 352, 292)],
    # wiadomosc, ktora Pipe napisal sam o 03:41 (pulpit przewiniety, nagranie "inbox")
    "night": [(16 * B, 722, 220, 476), (17 * B, 722, 215, 452)],
}

# Telegram: Pipe pisze sam, palec wciska "Fix it" dokladnie na drugim wejsciu muzyki (takt 10).
TAP = 10 * B
T0 = 8 * B
PHONE = {
    "msgs": [
        {"kind": "msg", "at": T0 + 0.05, "html": "alert", "time": "03:41"},
        {"kind": "msg", "at": T0 + 2 * BEAT, "html": "investigation", "time": "03:42"},
        {"kind": "kbd", "at": T0 + 2 * BEAT + 0.1, "until": TAP + 0.3, "buttons": ["Fix it", "Later"]},
        {"kind": "msg", "at": TAP + 0.25, "html": "progress.0", "time": "03:42",
         "states": [{"at": TAP + 1 * BEAT, "html": "progress.1"}, {"at": TAP + 2 * BEAT, "html": "progress.2"},
                    {"at": TAP + 3 * BEAT, "html": "progress.3"}, {"at": TAP + 4 * BEAT, "html": "progress_done"}]},
        {"kind": "msg", "at": TAP + 4 * BEAT, "html": "answer", "time": "03:42"},
    ],
    "tap": {"at": TAP, "button": 0},
}

# Terminal: indeksy stanow z surfaces.terminal (0 "$ pipe", 1 baner, 2..9 wpisywanie "/status", 10 wynik)
TERM_START = 11.5 * B
TERM_TYPE_START, TERM_KEY_GAP, TERM_ENTER = TERM_START + 2 * BEAT, 0.07, TERM_START + 4 * BEAT
TERM = [{"at": TERM_START, "index": 0}, {"at": TERM_START + 0.2, "index": 1}, {"at": TERM_START + 0.4, "index": 2}] + \
    [{"at": round(TERM_TYPE_START + i * TERM_KEY_GAP, 4), "index": 3 + i} for i in range(7)] + \
    [{"at": TERM_ENTER, "index": 10, "final": True}]

WORDS: dict = {}

# Napisy: linie jako tekst (wchodza razem) albo {"text", "at"}. <b> jasno, <g> zielono, <m> monospace.
CAPTIONS = [
    (0.05, 1 * B - 0.05, ["<m>03:41</m>", "Your shop is down."]),
    (1 * B, 3 * B - 0.08, ["Tell Pipe", "<b>what's wrong.</b>"]),
    (3 * B, 5 * B - 0.08, ["It reads the logs", "<b>and finds the cause.</b>"]),
    (5 * B, 6 * B - 0.08, ["<g>Fixed,</g> with a backup."]),
    (6 * B, 7 * B - 0.08, ["Something risky?"]),
    (7 * B, 8 * B - 0.08, ["<b>It asks first.</b>"]),
    (8 * B, 10 * B - 0.08, ["It also writes first", "<b>on Telegram.</b>"]),
    (10 * B, 11.5 * B - 0.08, ["One tap.", "<g>Fixed.</g>"]),
    (11.5 * B, 13 * B - 0.08, ["Or in", "<b>your terminal.</b>"]),
    (13 * B, 14 * B - 0.08, ["Warns you", "<b>days ahead.</b>"]),
    (14 * B, 15 * B - 0.08, ["Every change logged.", "<b><m>/undo</m> reverts it.</b>"]),
    (15 * B, 16 * B - 0.08, ["Knows your server", "<b>and shows what it touches.</b>"]),
    (16 * B, 17 * B - 0.08, ["Investigates on its own,", "<b>even at night.</b>"]),
]

# Kursor w przegladarce: (czas, cel, klik?). Cel to punkt w sledzonym elemencie: ulamek szerokosci/wysokosci + przesuniecie
# w px CSS. "Send" klika dokladnie w chwili wyslania; nad karta zgody kursor tylko czeka przy "Approve" — decyzja nalezy
# do czlowieka.
SEND = {"track": "input", "fx": 1.0, "dx": -27, "fy": 0.5, "dy": 0}
COMPOSER = {"track": "input", "fx": 0.62, "dx": 0, "fy": 1.0, "dy": 30}
APPROVE = {"track": "confirm", "fx": 0.0, "dx": 58, "fy": 1.0, "dy": -40}
CARD_TOP = {"track": "confirm", "fx": 0.55, "dx": 0, "fy": 0.25, "dy": 0}
CURSOR = [
    (TYPED1 + 0.12, COMPOSER, False), (CLICK1 - 0.16, SEND, False), (CLICK1, SEND, True), (CLICK1 + 0.35, SEND, False),
    (TYPED2 + 0.1, COMPOSER, False), (CLICK2 - 0.14, SEND, False), (CLICK2, SEND, True), (CLICK2 + 0.3, SEND, False),
    (7 * B + 0.5, CARD_TOP, False), (8 * B - 0.45, APPROVE, False), (8 * B - 0.02, APPROVE, False),
]


def anchors(take: dict) -> dict[str, float]:
    marks = take["marks"]
    first = lambda name: next(m["t"] for m in marks if m["name"] == name)
    steps = [m["t"] for m in marks if m["name"] == "step"]
    return {
        "typing": first("type1") - 0.15, "send1": first("send1"), "fix": steps[5], "type2": first("type2"),
        "lastkey1": max(m["t"] for m in marks if m["name"] == "key" and m["t"] < first("send1")),
        "lastkey2": max(m["t"] for m in marks if m["name"] == "key" and m["t"] < first("send2")),
        "send2": first("send2"),
        "fixed": next(m["t"] for m in marks if m["name"] == "msg" and m.get("text", "").startswith("Fixed")),
        "confirm": first("confirm") - 0.01,
    }


def to_source(points, t: float) -> float:
    for (t0, s0), (t1, s1) in zip(points, points[1:]):
        if t <= t1:
            return s0 + (s1 - s0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def to_output(points, src: float) -> float | None:
    for (t0, s0), (t1, s1) in zip(points, points[1:]):
        if s0 <= src <= s1 and s1 > s0:
            return t0 + (t1 - t0) * (src - s0) / (s1 - s0)
    return None


def entrances(scenes) -> list[dict]:
    """Scena bez wlasnego wejscia ciagnie ujecie dalej: dziedziczy czas wejscia poprzedniej (bez drugiego "punch")."""
    out, last = [], 0.0
    for n, a, b, kind, _, _, enter in scenes:
        last = a if enter else last
        out.append({"name": n, "start": a, "end": b, "kind": kind, "enter": last})
    return out


def build(capture: Path) -> dict:
    takes = {name: json.loads((capture / name / "take.json").read_text(encoding="utf-8"))
             for name in ("chat", "dashboard", "inbox")}
    times = {name: [f[1] for f in take["frames"]] for name, take in takes.items()}
    points_of = anchors(takes["chat"])

    def resolve(v):
        return points_of[v[0]] + v[1] if isinstance(v, tuple) else v

    scenes = [(n, a, b, kind, take, [(t, resolve(v)) for t, v in pts], enter)
              for n, a, b, kind, take, pts, enter in SCENES]

    frames = []
    for i in range(int(DURATION * FPS)):
        t = i / FPS
        scene = next((s for s in scenes if s[1] <= t < s[2]), scenes[-1])
        if scene[3] != "web":
            frames.append(None)
            continue
        take = takes[scene[4]]
        k = max(0, bisect_right(times[scene[4]], to_source(scene[5], t)) - 1)
        frames.append(f"{scene[4]}/{take['frames'][k][0]}")

    sfx, tracks = [], {}
    for name, a, b, kind, take_name, pts, _ in scenes:
        if kind != "web":
            continue
        marks = takes[take_name]["marks"]
        samples = []
        for mark in marks:
            out = to_output(pts, mark["t"])
            if out is None or not a <= out < b:
                continue
            if mark["name"] == "key":
                sfx.append({"t": round(out, 4), "kind": "key", "char": mark.get("char", "")})
            elif mark["name"] == "track":
                samples.append([round(out, 4), mark["boxes"]])
        # kamera potrzebuje polozenia elementow takze na samych krawedziach sceny
        track_marks = [m for m in marks if m["name"] == "track"]
        for edge in (a, b):
            src = to_source(pts, edge)
            near = min(track_marks, key=lambda m: abs(m["t"] - src), default=None)
            if near:
                samples.append([edge, near["boxes"]])
        tracks[name] = sorted(samples, key=lambda s: s[0])

    progress = PHONE["msgs"][3]
    sfx += [{"t": PHONE["msgs"][0]["at"], "kind": "notify"}, {"t": PHONE["msgs"][1]["at"], "kind": "pop"},
            {"t": TAP, "kind": "tap"}, {"t": PHONE["msgs"][4]["at"], "kind": "pop"}]
    sfx += [{"t": s["at"], "kind": "tick"} for s in progress["states"][:-1]]
    sfx += [{"t": s["at"], "kind": "key", "char": "x"} for s in TERM if 3 <= s["index"] <= 9]
    sfx.append({"t": TERM_ENTER, "kind": "enter"})
    sfx += [{"t": round(t, 4), "kind": "click"} for t, _, click in CURSOR if click]
    sfx.sort(key=lambda e: e["t"])

    return {
        "fps": FPS, "duration": DURATION, "end": END, "bpm": BPM,
        "cursor": [{"t": round(t, 4), "target": target, "click": click} for t, target, click in CURSOR],
        "viewport": takes["chat"]["viewport"], "scale": takes["chat"]["scale"], "frames": frames,
        "scenes": entrances(scenes),
        "camera": {name: [list(p) for p in points] for name, points in CAMERA.items()},
        "tracks": tracks, "phone": PHONE, "term": {"states": TERM}, "words": WORDS,
        "captions": [{"start": a, "end": b, "lines": lines} for a, b, lines in CAPTIONS],
        "sfx": sfx,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the promo timeline")
    parser.add_argument("--capture", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    data = build(Path(args.capture))
    Path(args.out).write_text(json.dumps(data), encoding="utf-8")
    kinds = {}
    for e in data["sfx"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"{args.out}: {len(data['frames'])} frames, {data['duration']} s, sfx {kinds}")


if __name__ == "__main__":
    main()
