"""
Reklama Pipe od zera: nagranie interfejsu -> montaz -> muzyka -> obraz w trzech formatach.

    python scripts/promo/make.py --samples <dir> --work <dir> --out <dir> [--skip-capture] [--formats f45,f916,f169]

<samples> to katalog z probkami CC0 (score.py): vcsl/ (wybrane katalogi z github.com/sgossner/VCSL) i keys/x/
(rozpakowany "Keyboard Soundpack #1" z opengameart.org). Wymaga ffmpeg, Chrome i playwright.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAMES = {"f45": "pipe-promo-4x5.mp4", "f916": "pipe-promo-9x16.mp4", "f169": "pipe-promo-16x9.mp4"}


def run(*args: str) -> None:
    print("$", " ".join(args[1:]), flush=True)
    subprocess.run([sys.executable, *args], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Pipe promo video")
    parser.add_argument("--samples", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--skip-capture", action="store_true")
    parser.add_argument("--formats", default="f45,f916,f169")
    args = parser.parse_args()
    work, out = Path(args.work), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    capture, timeline, audio = work / "capture", work / "timeline.json", out / "pipe-promo-score.wav"
    surfaces = work / "surfaces.json"
    if not args.skip_capture:
        run(str(HERE / "capture.py"), "--out", str(capture))
    run(str(HERE / "edit.py"), "--capture", str(capture), "--out", str(timeline))
    run(str(HERE / "surfaces.py"), "--out", str(surfaces))
    run(str(HERE / "score.py"), "--samples", args.samples, "--timeline", str(timeline), "--out", str(audio))
    for fmt in args.formats.split(","):
        run(str(HERE / "render.py"), "--capture", str(capture), "--timeline", str(timeline), "--surfaces", str(surfaces),
            "--audio", str(audio),
            "--format", fmt, "--out", str(out / NAMES[fmt]))


if __name__ == "__main__":
    main()
