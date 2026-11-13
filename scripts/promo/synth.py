"""
Muzyka reklamy — w calosci syntezowana (bez probek): dokladny stroj i jednolita barwa w calym utworze.

Klimat: noc, spokojna pewnosc, technika. 130 BPM w polrytmie (stopa na raz, klaskanie na trzy — odczuwalne 65),
d-moll: Dm9 - Bbmaj7 - Fmaj7 - Cadd9. Instrumenty: arpeggio "pluck" (pila z filtrem dolnoprzepustowym, ktorego
otwarcie idzie za fabula, echo kropkowanej osemki), szeroki pad (5 rozstrojonych pil), bas (sinus + harmoniczne,
pompowany od stopy), perkusja syntezowana (stopa, klaskanie, hi-hat), narastania z filtrowanego szumu.
Pily sa skladane z harmonicznych (synteza addytywna) — bez aliasingu i bez petli po probkach.

Struktura idzie za montazem (edit.py, takty po 1,846 s): 0 strona 502 | 1-4 prosba i szukanie przyczyny (filtr sie
otwiera) | 5 "Fixed" = wejscie | 6 ryzykowna prosba | 7 "It asks first" = cisza | 8-9 Telegram, narastanie |
10 dotkniecie "Fix it" = drugie wejscie | 11-16 terminal, pulpit, /undo, schemat, wiadomosc z nocy | 17-20 plansza,
akord F-dur i wybrzmienie.
"""

from __future__ import annotations

import numpy as np

SR = 48000
BPM = 130
BEAT = 60 / BPM
BAR = 4 * BEAT
S16 = BEAT / 4


def hz(note: float) -> float:
    return 440 * 2 ** ((note - 69) / 12)


def n(name: str) -> int:
    names = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    base = names[name[0]] + (1 if "#" in name else -1 if "b" in name[1:] else 0)
    return 12 * (int(name[-1]) + 1) + base


def db(x: float) -> float:
    return 10 ** (x / 20)


# --- barwy ---------------------------------------------------------------------------------------------------------

def saw(f0: float, t: np.ndarray, cutoff, reso: float = 0.0, top: float = 16000) -> np.ndarray:
    """Pila przez filtr 2-biegunowy: harmoniczne k*f0 z waga 1/k i tlumieniem filtra (cutoff moze zmieniac sie w czasie)."""
    out = np.zeros_like(t)
    cmax = float(np.max(cutoff))
    kmax = max(1, int(min(top, cmax * 5) / f0))
    for k in range(1, kmax + 1):
        f = k * f0
        gain = 1 / np.sqrt(1 + (f / cutoff) ** 4)
        if reso:
            gain = gain * (1 + reso * np.exp(-((f - cutoff) / (0.18 * cutoff)) ** 2))
        out += gain / k * np.sin(2 * np.pi * f * t + 0.7 * k)
    return out


def pluck(note: int, dur: float, open_: float, vel: float = 1.0) -> np.ndarray:
    """Krotki dzwiek arpeggia: filtr otwiera sie na ataku i szybko zamyka; `open_` 0..1 = jak jasno."""
    t = np.arange(int((dur + 0.25) * SR)) / SR
    base = 350 + 2600 * open_
    cutoff = base + (900 + 3800 * open_) * np.exp(-t / 0.07)
    s = saw(hz(note), t, cutoff, reso=0.6)
    env = np.minimum(1, t / 0.003) * np.exp(-t / (0.16 + 0.1 * open_))
    s = s * env * vel
    return np.stack([s, s], axis=1).astype(np.float32)


def pad(notes: list[int], dur: float, cutoff: float, seed: int = 0) -> np.ndarray:
    """Szeroki pad: kazda nuta to 5 rozstrojonych pil (lewo/prawo osobno), lagodny atak i wybrzmienie."""
    rng = np.random.default_rng(seed)
    t = np.arange(int((dur + 0.8) * SR)) / SR
    left, right = np.zeros_like(t), np.zeros_like(t)
    for note in notes:
        for cents, pan in ((-14, -0.8), (-7, 0.4), (0, 0.0), (7, -0.4), (14, 0.8)):
            f = hz(note) * 2 ** (cents / 1200)
            s = saw(f, t + rng.uniform(0, 1), cutoff, top=6000)
            left += s * (1 - pan) / 2
            right += s * (1 + pan) / 2
    env = np.minimum(1, t / 0.35) * np.clip((dur + 0.8 - t) / 0.8, 0, 1)
    out = np.stack([left, right], axis=1) * env[:, None] / (len(notes) * 5)
    return out.astype(np.float32)


def bass(note: int, dur: float, bright: float = 0.0) -> np.ndarray:
    """Bas: sinus (to slychac na sluchawkach) + kilka harmonicznych (to slychac na telefonie)."""
    t = np.arange(int((dur + 0.05) * SR)) / SR
    f = hz(note)
    s = np.sin(2 * np.pi * f * t) + saw(f, t, 220 + 500 * bright, top=1200) * 0.55
    env = np.minimum(1, t / 0.006) * np.clip((dur + 0.05 - t) / 0.05, 0, 1)
    s = np.tanh(s * env * 1.2) / np.tanh(1.2)
    return np.stack([s, s], axis=1).astype(np.float32)


def kick() -> np.ndarray:
    t = np.arange(int(0.45 * SR)) / SR
    f = 46 + 110 * np.exp(-t / 0.032)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.2)
    click = np.sin(2 * np.pi * 1800 * t) * np.exp(-t / 0.002) * 0.25
    s = np.tanh((body + click) * 1.6) / np.tanh(1.6)
    return np.stack([s, s], axis=1).astype(np.float32)


def noise_band(seconds: float, lo: float, hi: float, seed: int) -> np.ndarray:
    x = np.random.default_rng(seed).standard_normal(int(seconds * SR))
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= ((f > lo) & (f < hi))
    return np.fft.irfft(spec, n=len(x))


def clap(seed: int = 0) -> np.ndarray:
    """Klaskanie: trzy krotkie uderzenia szumu 0,9-5 kHz co 9 ms i ogon."""
    t = np.arange(int(0.35 * SR)) / SR
    nz = noise_band(0.35, 900, 5000, seed)
    env = np.exp(-t / 0.09) * 0.6
    for k in range(3):
        env = env + np.exp(-np.clip(t - k * 0.009, 0, None) / 0.005) * (t >= k * 0.009)
    s = nz / (np.abs(nz).max() or 1) * env
    return np.stack([s, s], axis=1).astype(np.float32)


def hat(open_: bool = False, seed: int = 0) -> np.ndarray:
    length = 0.22 if open_ else 0.05
    t = np.arange(int(length * SR)) / SR
    nz = noise_band(length, 7000, 16000, seed)
    s = nz / (np.abs(nz).max() or 1) * np.exp(-t / (0.07 if open_ else 0.012))
    return np.stack([s, s], axis=1).astype(np.float32)


def riser(seconds: float, seed: int = 3) -> np.ndarray:
    """Narastanie: szum, ktorego filtr otwiera sie od 300 Hz do 9 kHz (STFT, maska co ramke), glosniej ku koncowi."""
    x = np.random.default_rng(seed).standard_normal(int(seconds * SR))
    win, hop = 2048, 512
    frames = 1 + (len(x) - win) // hop
    out = np.zeros(len(x))
    norm = np.zeros(len(x))
    w = np.hanning(win)
    f = np.fft.rfftfreq(win, 1 / SR)
    for i in range(frames):
        a = i * hop
        k = i / max(1, frames - 1)
        fc = 300 * (9000 / 300) ** (k ** 1.6)
        spec = np.fft.rfft(x[a:a + win] * w) / np.sqrt(1 + (f / fc) ** 4)
        out[a:a + win] += np.fft.irfft(spec, n=win) * w
        norm[a:a + win] += w ** 2
    out /= np.maximum(norm, 1e-3)
    env = np.linspace(0, 1, len(out)) ** 2.2
    s = out / (np.abs(out).max() or 1) * env
    return np.stack([s, s * 0.92], axis=1).astype(np.float32)


# --- kompozycja ----------------------------------------------------------------------------------------------------

DM9 = [n("D3"), n("F3"), n("A3"), n("C4"), n("E4")]
BBMAJ7 = [n("Bb2"), n("F3"), n("A3"), n("D4")]
FMAJ7 = [n("F3"), n("A3"), n("C4"), n("E4")]
CADD9 = [n("C3"), n("G3"), n("D4"), n("E4")]
GM9 = [n("G2"), n("D3"), n("F3"), n("A3"), n("Bb3")]
FMAJ9 = [n("F2"), n("C3"), n("E3"), n("G3"), n("A3"), n("C4")]
# takt -> (akord, pryma basu, czesc)
SONG = {
    0: (DM9, n("D2"), "hook"),
    1: (DM9, n("D2"), "build"), 2: (BBMAJ7, n("Bb1"), "build"), 3: (FMAJ7, n("F1"), "build"), 4: (CADD9, n("C2"), "lift"),
    5: (DM9, n("D2"), "drop"), 6: (BBMAJ7, n("Bb1"), "drop"),
    7: (GM9, n("G1"), "still"),
    8: (DM9, n("D2"), "build"), 9: (CADD9, n("C2"), "lift"),
    10: (DM9, n("D2"), "drop"), 11: (BBMAJ7, n("Bb1"), "drop"), 12: (FMAJ7, n("F1"), "drop"), 13: (CADD9, n("C2"), "drop"),
    14: (DM9, n("D2"), "drop"), 15: (BBMAJ7, n("Bb1"), "drop"), 16: (CADD9, n("C2"), "accent"),
    17: (FMAJ9, n("F1"), "end"), 18: (FMAJ9, n("F1"), "tail"), 19: (FMAJ9, n("F1"), "tail"), 20: (FMAJ9, n("F1"), "tail"),
}
# jak otwarty filtr arpeggia na poczatku i koncu taktu
OPEN = {0: (0, 0), 1: (0.0, 0.15), 2: (0.15, 0.3), 3: (0.3, 0.45), 4: (0.45, 0.65), 5: (0.85, 0.85), 6: (0.85, 0.8),
        7: (0.2, 0.2), 8: (0.25, 0.45), 9: (0.45, 0.7), 10: (0.9, 0.9), 11: (0.9, 0.9), 12: (0.9, 0.9), 13: (0.9, 0.95),
        14: (0.95, 0.95), 15: (0.95, 0.95), 16: (0.95, 1.0), 17: (0.8, 0.6), 18: (0.5, 0.3), 19: (0.3, 0.2), 20: (0.2, 0.1)}
ARP_ORDER = [0, 1, 2, 3, 4, 3, 2, 1]


def arp_tones(chord: list[int]) -> list[int]:
    """Dzwieki arpeggia: skladniki akordu przeniesione w rejestr D4-D6, rosnaco, piec dzwiekow."""
    tones = sorted({(p - n("D4")) % 12 + n("D4") for p in chord})
    while len(tones) < 5:
        tones.append(tones[len(tones) - 4] + 12)
    return tones[:5]


class Track:
    def __init__(self, seconds: float):
        self.n = int(seconds * SR)
        self.buses: dict[str, np.ndarray] = {}
        self.kicks: list[float] = []

    def add(self, bus: str, t: float, sig: np.ndarray, gain: float = 1.0, pan: float = 0.0) -> None:
        buf = self.buses.setdefault(bus, np.zeros((self.n, 2), np.float32))
        a = int(round(t * SR))
        if a >= self.n:
            return
        sig = sig[: self.n - a]
        l, r = np.cos((pan + 1) * np.pi / 4) * np.sqrt(2), np.sin((pan + 1) * np.pi / 4) * np.sqrt(2)
        buf[a:a + len(sig)] += sig * gain * np.array([l, r], np.float32)


def compose(seconds: float) -> Track:
    tr = Track(seconds)
    K, CLAP, HATS = kick(), [clap(s) for s in range(3)], [hat(False, s) for s in range(4)]
    OHAT = hat(True, 9)
    for bar, (chord, root, part) in SONG.items():
        t0 = bar * BAR
        o0, o1 = OPEN[bar]
        # pad: caly takt (koncowy akord trzyma do konca)
        cutoff = {"hook": 700, "build": 1100, "lift": 1500, "drop": 2200, "still": 900, "accent": 2400,
                  "end": 2600, "tail": 1600}[part]
        if part != "tail":
            length = BAR * (4 if part == "end" else 1)
            tr.add("pad", t0, pad(chord, length, cutoff, seed=bar), db({"hook": -8, "still": -4, "end": -3}.get(part, -6)))
        # arpeggio osemkami; w ciszy po karcie zgody tylko cwiartki
        tones = arp_tones(chord)
        step = BEAT if part in ("still", "tail") else BEAT / 2
        count = int(round(BAR / step))
        if part != "hook" and not (part == "tail" and bar > 19):
            for i in range(count):
                k = i / count
                op = o0 + (o1 - o0) * k
                note = tones[ARP_ORDER[i % 8] if step < BEAT else [0, 2, 4, 2][i % 4]]
                vel = (1.0 if i % 2 == 0 else 0.78) * (0.7 if part in ("still", "tail") else 1.0)
                tr.add("arp", t0 + i * step, pluck(note, step * 1.6, op, vel), 1.0, pan=0.18 if i % 2 else -0.18)
        # bas
        if part in ("drop", "accent", "lift") or (part == "build" and bar >= 3):
            for i, (s, length) in enumerate([(0, 6), (6, 2), (8, 6), (14, 2)]):
                note = root + (12 if s in (6, 14) else 0)
                tr.add("bass", t0 + s * S16, bass(note, length * S16 * 0.95, 0.6 if part in ("drop", "accent") else 0.2),
                       db(-1 if s in (0, 8) else -5))
        elif part in ("still", "end", "hook"):
            tr.add("bass", t0, bass(root, BAR * (3 if part == "end" else 1) * 0.98, 0.0), db(-6 if part == "hook" else -4))
        # perkusja
        if part in ("drop", "accent"):
            for s in (0, 7, 10):
                tr.add("kick", t0 + s * S16, K, db(0 if s == 0 else -3))
                tr.kicks.append(t0 + s * S16)
            tr.add("clap", t0 + 8 * S16, CLAP[bar % 3], db(-2))
            tr.add("clap", t0 + 15 * S16, CLAP[(bar + 1) % 3], db(-16))
            for s in range(16):
                tr.add("hat", t0 + s * S16, HATS[s % 4], db(-6 if s % 2 == 0 else -13), pan=0.3)
            for s in (6, 14):
                tr.add("hat", t0 + s * S16, OHAT, db(-10), pan=-0.25)
        elif part in ("build", "lift"):
            tr.add("kick", t0, K, db(-4))
            tr.kicks.append(t0)
            for s in range(0, 16, 2):
                tr.add("hat", t0 + s * S16, HATS[s % 4], db(-12 + 3 * (part == "lift")), pan=0.3)
            if part == "lift":                                 # zaklaskanie szesnastkami w drugiej polowie, cisza na koncu
                for s in range(8, 15):
                    tr.add("clap", t0 + s * S16, CLAP[s % 3], db(-22 + (s - 8) * 2.6))
        elif part == "hook":
            for s in range(4):                                 # zegar: ciche tykanie na cwiartki pod "03:41"
                tr.add("hat", t0 + s * BEAT, HATS[s % 4], db(-16))
        if part == "accent":                                   # ostatni takt przed plansza: akord na kazda cwiartke
            for s in range(4):
                for k, p in enumerate(arp_tones(chord)[:4]):
                    tr.add("arp", t0 + s * BEAT + k * 0.004, pluck(p, BEAT, 0.95, 0.55), 1.0)
    # narastania do wejsc i uderzenia na wejsciach
    for drop, length in ((5 * BAR, 2 * BAR), (10 * BAR, 2 * BAR), (17 * BAR, BAR)):
        r = riser(length, seed=int(drop))
        tr.add("fx", drop - len(r) / SR, r, db(-9))
    for t in (5 * BAR, 10 * BAR, 17 * BAR):
        tr.add("fx", t, crash_like(seed=int(t)), db(-12))
    tr.add("kick", 17 * BAR, K, 1.0)
    tr.kicks.append(17 * BAR)
    return tr


def crash_like(seed: int = 0) -> np.ndarray:
    """Talerz na wejsciu: jasny szum z dlugim wybrzmieniem (bez probek)."""
    length = 2.5
    t = np.arange(int(length * SR)) / SR
    nz = noise_band(length, 4500, 16000, seed)
    s = nz / (np.abs(nz).max() or 1) * np.exp(-t / 0.7) * np.minimum(1, t / 0.002)
    return np.stack([s, np.roll(s, 37)], axis=1).astype(np.float32)


def delay(x: np.ndarray, seconds: float, feedback: float = 0.38, repeats: int = 4, darken: float = 3500) -> np.ndarray:
    """Echo ping-pong: kolejne powtorzenia na przemian w lewym i prawym kanale, coraz ciemniejsze."""
    out = x.copy()
    d = int(seconds * SR)
    spec_f = np.fft.rfftfreq(len(x), 1 / SR)
    cur = x
    for i in range(1, repeats + 1):
        cur = np.fft.irfft(np.fft.rfft(cur, axis=0) / np.sqrt(1 + (spec_f / darken) ** 2)[:, None], n=len(x), axis=0)
        shifted = np.zeros_like(x)
        shifted[d * i:] = cur[: len(x) - d * i] * feedback ** i
        if i % 2:
            shifted = shifted[:, ::-1]
        out += shifted.astype(np.float32)
    return out
