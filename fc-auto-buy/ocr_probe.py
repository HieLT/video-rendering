"""One-shot read-only OCR probe for the current FC ONLINE window."""
from __future__ import annotations

import sys
from pathlib import Path

from window_capture import capture_window, find_window
from price_reader import read_price


def main() -> int:
    hwnd = find_window("FC ONLINE")
    if hwnd is None:
        print("FC ONLINE window not found", file=sys.stderr)
        return 2
    image = capture_window(hwnd)
    # Relative to the 1296x759 window layout currently visible in the game.
    # This is a probe only; the final tool will make the region configurable.
    crop = image.crop((975, 315, 1060, 350)).resize((680, 280))
    crop_path = Path(__file__).with_name("price_probe.png")
    crop.save(crop_path)
    text, value = read_price(crop)
    print(f"window={hwnd} size={image.size} text={text!r} bp={value} crop={crop_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
