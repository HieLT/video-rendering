"""Read-only FC Online price-region monitor."""
from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int


def parse_region(value: str) -> Region:
    try:
        left, top, width, height = (int(x.strip()) for x in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("region must be left,top,width,height") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("region width and height must be positive")
    return Region(left, top, width, height)


def normalize_price(text: str) -> int | None:
    cleaned = re.sub(r"[^0-9BMK.,]", "", text.upper()).replace(",", ".")
    if not cleaned:
        return None
    suffix = cleaned[-1] if cleaned[-1] in "BMK" else ""
    number = cleaned[:-1] if suffix else cleaned
    try:
        # A suffixed value uses one decimal separator (1.09B); a plain BP
        # value commonly uses thousands separators (4.770.000.000).
        value = float(number if suffix else number.replace(".", ""))
    except ValueError:
        return None
    return round(value * ({"B": 1_000_000_000, "M": 1_000_000, "K": 1_000}.get(suffix, 1)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", type=parse_region, required=True)
    parser.add_argument("--interval-ms", type=float, default=50)
    parser.add_argument("--tesseract", action="store_true")
    parser.add_argument("--window-title", default="", help="capture relative to a visible window")
    args = parser.parse_args()
    try:
        import mss
    except ImportError:
        parser.error("install mss first: python -m pip install mss")
    if args.tesseract:
        try:
            import cv2
            import numpy as np
            import pytesseract
        except ImportError:
            parser.error("install pillow/opencv-python/pytesseract first")
    if args.window_title:
        from window_capture import capture_window, find_window
        hwnd = find_window(args.window_title)
        if hwnd is None:
            parser.error(f"window not found: {args.window_title}")
        box = None
    else:
        hwnd = None
        box = {"left": args.region.left, "top": args.region.top,
               "width": args.region.width, "height": args.region.height}
    previous = None
    changed = 0
    # MSS() is the current API; mss.mss() is retained by older releases only.
    with mss.MSS() as screen:
        print("read-only monitor started; press Ctrl+C to stop")
        try:
            while True:
                started = time.perf_counter_ns()
                try:
                    if hwnd is not None:
                        image = capture_window(hwnd).crop((args.region.left, args.region.top,
                                                           args.region.left + args.region.width,
                                                           args.region.top + args.region.height))
                        raw = image.convert("RGB").tobytes()
                        shot = image
                    else:
                        shot = screen.grab(box)
                        raw = bytes(shot.rgb)
                except Exception as exc:
                    print(f"capture_error={type(exc).__name__}: {exc}", flush=True)
                    time.sleep(max(args.interval_ms / 1000, 0.1))
                    continue
                if previous is not None and raw != previous:
                    changed += 1
                    line = f"change={changed} capture_ms={(time.perf_counter_ns() - started) / 1e6:.2f}"
                    if args.tesseract:
                        image = np.asarray(shot)
                        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
                        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                        text = pytesseract.image_to_string(
                            gray, config="--psm 7 -c tessedit_char_whitelist=0123456789.BMK"
                        ).strip()
                        line += f" ocr={text!r} bp={normalize_price(text)}"
                    print(line, flush=True)
                previous = raw
                remaining = args.interval_ms / 1000 - (time.perf_counter_ns() - started) / 1e9
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            print(f"stopped; changed_frames={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
