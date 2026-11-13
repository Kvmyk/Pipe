"""
Dzwiek reklamy: muzyka syntezowana w synth.py + dzwieki interfejsu zgrane z obrazem, miks i mastering.

Dzwieki interfejsu (czasy z timeline.json, edit.py — slychac je w klatce, w ktorej cos sie dzieje na ekranie):
klawisze — atak nagran z "Keyboard Soundpack #1" (OpenGameArt, CC0); klikniecie myszy (wcisniecie + puszczenie),
dotkniecie ekranu, powiadomienie i nowa wiadomosc — syntezowane, krotkie i ciche wzgledem muzyki.

    python scripts/promo/score.py --samples <dir z keys/x/> --timeline timeline.json --out score.wav
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth  # noqa: E402

SR = synth.SR


def db(x: float) -> float:
    return 10 ** (x / 20)


def load(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        ints = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        data = ints.astype(np.float32) / 2 ** 23
    elif width == 2:
        data = np.frombuffer(raw, np.int16).astype(np.float32) / 2 ** 15
    else:
        raise ValueError(f"{path}: {width * 8}-bit")
    data = data.reshape(-1, ch)
    if ch == 1:
        data = np.repeat(data, 2, axis=1)
    return data[:, :2].copy(), rate


def resample(x: np.ndarray, length: int) -> np.ndarray:
    """Pasmowo ograniczone przeprobkowanie przez FFT (dokladne, bez aliasingu interpolacji liniowej)."""
    n = len(x)
    if length == n:
        return x
    spec = np.fft.rfft(x, axis=0)
    keep = min(spec.shape[0], length // 2 + 1)
    out = np.zeros((length // 2 + 1, x.shape[1]), dtype=complex)
    out[:keep] = spec[:keep]
    return (np.fft.irfft(out, n=length, axis=0) * (length / n)).astype(np.float32)


def eq(x: np.ndarray, curve) -> np.ndarray:
    """Statyczny korektor w dziedzinie czestotliwosci: curve(f) -> wzmocnienie liniowe."""
    spec = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= curve(freqs)[:, None]
    return np.fft.irfft(spec, n=len(x), axis=0).astype(np.float32)


def lowpass(f0: float, order: int = 2):
    return lambda f: 1 / np.sqrt(1 + (f / f0) ** (2 * order))


def highpass(f0: float, order: int = 2):
    return lambda f: 1 / np.sqrt(1 + (f0 / np.maximum(f, 1e-3)) ** (2 * order))


def shelf(f0: float, gain_db: float, high: bool = True):
    g = db(gain_db)
    return lambda f: 1 + (g - 1) * (1 / (1 + (f0 / np.maximum(f, 1e-3)) ** 2) if high else 1 / (1 + (f / f0) ** 2))


def bell(f0: float, gain_db: float, q: float = 1.0):
    g = db(gain_db)
    return lambda f: 1 + (g - 1) * np.exp(-((np.log2(np.maximum(f, 1e-3) / f0)) * q * 1.6) ** 2)


def chain(*curves):
    return lambda f: np.prod([c(f) for c in curves], axis=0)


def impulse(seconds: float = 3.2, seed: int = 3) -> np.ndarray:
    """Odpowiedz impulsowa sali: rozpraszajacy sie szum, wysokie tony gasna szybciej, wczesne odbicia, predelay."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal((n, 2)).astype(np.float32)
    spec = np.fft.rfft(noise, axis=0)
    freqs = np.fft.rfftfreq(n, 1 / SR)
    out = np.zeros((n, 2), dtype=np.float32)
    bands = [(0, 250, 2.6), (250, 1200, 2.2), (1200, 4000, 1.5), (4000, 9000, 0.9), (9000, SR / 2, 0.45)]
    for lo, hi, rt60 in bands:
        mask = ((freqs >= lo) & (freqs < hi))[:, None]
        band = np.fft.irfft(spec * mask, n=n, axis=0)
        out += band * np.exp(-6.91 * t / rt60)[:, None]
    attack = np.clip(t / 0.012, 0, 1) ** 2
    out *= attack[:, None]
    pre = int(0.022 * SR)
    ir = np.zeros((n + pre, 2), dtype=np.float32)
    ir[pre:] = out
    for delay, gain, side in ((0.011, .5, 0), (0.017, .42, 1), (0.023, .36, 0), (0.031, .3, 1), (0.041, .22, 0)):
        ir[int(delay * SR), side] += gain * np.abs(out).max() * 6
    return ir / np.sqrt((ir ** 2).sum() / 2)


def convolve(x: np.ndarray, ir: np.ndarray) -> np.ndarray:
    n = len(x) + len(ir) - 1
    size = 1 << (n - 1).bit_length()
    out = np.fft.irfft(np.fft.rfft(x, size, axis=0) * np.fft.rfft(ir, size, axis=0), size, axis=0)
    return out[: len(x)].astype(np.float32)


# --- dzwieki interfejsu ------------------------------------------------------------------------------------------

def tone(freq: float, seconds: float, decay: float, attack: float = 0.004, glide: float = 0.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    f = freq * (1 + glide * np.exp(-t / 0.03))
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) + 0.18 * np.sin(4 * np.pi * np.cumsum(f) / SR)
    s *= np.minimum(1, t / attack) * np.exp(-t / decay)
    return np.stack([s, s], axis=1).astype(np.float32)


def mouse_click(seed: int = 0) -> np.ndarray:
    """Klikniecie myszy: twardy, krotki trzask wcisniecia i cichszy puszczenia 70 ms pozniej."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(0.12 * SR)) / SR
    out = np.zeros_like(t)
    for at, gain, pitch in ((0.0, 1.0, 3200), (0.07, 0.45, 3900)):
        tt = np.clip(t - at, 0, None)
        on = t >= at
        burst = rng.standard_normal(len(t)) * np.exp(-tt / 0.0012)
        ping = np.sin(2 * np.pi * pitch * tt) * np.exp(-tt / 0.006)
        out += (burst * 0.6 + ping) * gain * on
    out = eq(np.stack([out, out], axis=1).astype(np.float32), chain(highpass(1200, 2), lowpass(9000, 1)))
    return out / (np.abs(out).max() or 1) * 0.5


def finger_tap() -> np.ndarray:
    """Dotkniecie szkla: miekkie, krotkie stukniecie w srodku pasma."""
    t = np.arange(int(0.08 * SR)) / SR
    s = np.sin(2 * np.pi * 380 * t) * np.exp(-t / 0.012) + np.random.default_rng(4).standard_normal(len(t)) * np.exp(-t / 0.002) * 0.3
    out = eq(np.stack([s, s], axis=1).astype(np.float32), chain(highpass(180), lowpass(6000, 1)))
    return out / (np.abs(out).max() or 1) * 0.5


def load_keys(directory: Path) -> list[np.ndarray]:
    """Klawisz: sam atak nagrania (45 ms) w oryginalnej wysokosci, bez dudnienia obudowy."""
    out = []
    for path in sorted((directory / "Single Keys").glob("*.wav"))[:12]:
        data, rate = load(str(path))
        sig = resample(data, int(len(data) * SR / rate))
        onset = int(np.argmax(np.abs(sig[:, 0]) > np.abs(sig).max() * 0.2))
        sig = sig[max(0, onset - 48): onset + int(0.045 * SR)].copy()
        sig *= (np.linspace(1, 0, len(sig)) ** 1.5)[:, None]
        sig = eq(sig, chain(highpass(700, 2), bell(3500, -2, 1)))
        out.append(sig / (np.abs(sig).max() or 1) * 0.5)
    return out


def ui_sounds(track: synth.Track, events: list[dict], keys: list[np.ndarray], rng: random.Random) -> None:
    click, tap = mouse_click(), finger_tap()
    for e in events:
        t, kind = e["t"], e["kind"]
        if kind == "key":
            track.add("ui", t, keys[rng.randrange(len(keys))], db(-9 + rng.uniform(-2, 1)), pan=rng.uniform(-.1, .1))
        elif kind == "enter":
            track.add("ui", t, keys[3], db(-6))
        elif kind == "click":
            track.add("ui", t, click, db(-4))
        elif kind == "tap":
            track.add("ui", t, tap, db(-2))
        elif kind == "notify":                                 # dwa szklane dzwieki, jak powiadomienie
            track.add("ui", t, tone(synth.hz(synth.n("A5")), 0.5, 0.18), db(-10))
            track.add("ui", t + 0.1, tone(synth.hz(synth.n("E6")), 0.7, 0.25), db(-10))
        elif kind == "pop":
            track.add("ui", t, tone(synth.hz(synth.n("D6")), 0.3, 0.07, glide=0.15), db(-12))
        elif kind == "tick":
            track.add("ui", t, tone(synth.hz(synth.n("A6")), 0.12, 0.025), db(-18))


# --- miks --------------------------------------------------------------------------------------------------------

def render(samples: Path, timeline: dict) -> np.ndarray:
    rng = random.Random(11)
    seconds = float(timeline["duration"])
    tr = synth.compose(seconds + 0.5)
    ui_sounds(tr, timeline.get("sfx", []), load_keys(samples / "keys" / "x"), rng)

    n = tr.n
    zero = np.zeros((n, 2), np.float32)
    bus = lambda name: tr.buses.get(name, zero)
    # pompowanie od stopy (bas, pad, arpeggio)
    duck = np.ones(n, np.float32)
    tt = np.arange(int(0.3 * SR)) / SR
    shape = (1 - 0.5 * np.exp(-tt / 0.09)).astype(np.float32)
    for k in tr.kicks:
        a = int(k * SR)
        b = min(n, a + len(shape))
        if 0 <= a < n:
            duck[a:b] = np.minimum(duck[a:b], shape[: b - a])
    duck = duck[:, None]

    arp = synth.delay(bus("arp"), 3 * synth.S16 * 2, feedback=0.35, repeats=4) * duck   # echo kropkowanej osemki
    pad = eq(bus("pad"), chain(highpass(150), shelf(7000, -3))) * duck
    bass = eq(bus("bass"), chain(highpass(30, 2), lowpass(3000, 1))) * duck
    kick = eq(bus("kick"), highpass(30, 2))
    clap, hat = eq(bus("clap"), highpass(300)), eq(bus("hat"), highpass(6000))
    fx = eq(bus("fx"), highpass(250))
    ui = eq(bus("ui"), highpass(200))

    hall = eq(convolve(arp * 0.3 + pad * 0.35 + clap * 0.3 + ui * 0.12, impulse(2.8, seed=7)),
              chain(highpass(300), lowpass(8000, 1)))
    # proporcje z pomiaru szyn (RMS w czesciach z perkusja): stopa i bas niosa, arpeggio na wierzchu, pad pod spodem
    # cel (RMS calego utworu): stopa -16, bas -19, arpeggio -21, pad -24, klaskanie -30, hi-hat -33 dB; interfejs -26 dB,
    # zeby klikniecia i klawisze bylo wyraznie slychac nad muzyka (pomiar szyn: score.py --levels)
    music = (kick * db(-3) + bass * db(-15) + pad * db(1) + arp * db(-17) + clap * db(4) + hat * db(0.5)
             + fx * db(0) + hall * db(-10))
    out = music + ui * db(8)
    out = out[: int(seconds * SR)]
    peak = np.abs(out).max() or 1
    out = np.tanh(out / peak * 1.3) / np.tanh(1.3)                # lekkie nasycenie calosci
    tail = int(0.6 * SR)
    out[-tail:] *= (np.linspace(1, 0, tail) ** 2)[:, None]
    return (out / (np.abs(out).max() or 1) * db(-1.5)).astype(np.float32), tr


def write_wav(path: Path, x: np.ndarray) -> None:
    ints = np.clip(x * (2 ** 23 - 1), -2 ** 23, 2 ** 23 - 1).astype(np.int32)
    b = np.zeros((ints.size, 3), dtype=np.uint8)
    flat = ints.reshape(-1)
    b[:, 0], b[:, 1], b[:, 2] = flat & 0xFF, (flat >> 8) & 0xFF, (flat >> 16) & 0xFF
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(SR)
        w.writeframes(b.tobytes())


def master(src: Path, dst: Path, lufs: float = -14.0, peak: float = -3.5) -> None:
    """Glosnosc pod platformy spolecznosciowe: dwa przebiegi loudnorm (EBU R128); szczyt -3,5 dBTP — koder AAC podbija go o ~1,7 dB, w pliku MP4 wychodzi ok. -1,8."""
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src), "-af",
                            f"loudnorm=I={lufs}:TP={peak}:LRA=9:print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    stats = json.loads(probe[probe.rindex("{"):probe.rindex("}") + 1])
    af = (f"loudnorm=I={lufs}:TP={peak}:LRA=9:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
          f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:"
          "linear=true,aresample=48000")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", af, "-c:a", "pcm_s24le", str(dst)],
                   check=True)
    check = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(dst), "-af", "ebur128=peak=true", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    summary = check[check.rindex("Summary:"):]
    print(" ".join(summary.split()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the promo soundtrack")
    parser.add_argument("--samples", required=True, help="directory with keys/x/ (keyboard recordings)")
    parser.add_argument("--timeline", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--levels", action="store_true", help="print RMS of every bus")
    args = parser.parse_args()
    timeline = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
    out = Path(args.out)
    raw = out.with_suffix(".raw.wav")
    audio, tr = render(Path(args.samples), timeline)
    if args.levels:
        for name, x in sorted(tr.buses.items()):
            print(f"{name:5s} rms {20 * np.log10(np.sqrt((x ** 2).mean()) + 1e-12):6.1f} dB  peak {20 * np.log10(np.abs(x).max() + 1e-12):6.1f} dB")
    write_wav(raw, audio)
    master(raw, out)
    raw.unlink()
    print(out)


if __name__ == "__main__":
    main()
