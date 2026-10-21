"""Read-only watcher for the TTCN 'Giá tối đa' region."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from window_capture import capture_window, find_window


def main() -> int:
    root = Path(__file__).parent
    cfg = json.loads((root / "regions.json").read_text(encoding="utf-8-sig"))
    region = cfg["regions"]["market_max_price"]
    hwnd = find_window(cfg.get("window_title", "FC ONLINE"))
    if hwnd is None:
        raise SystemExit("FC ONLINE window not found")
    previous = None
    candidate = None
    candidate_count = 0
    print("watching market_max_price; Ctrl+C to stop")
    try:
        while True:
            image = capture_window(hwnd).crop((region["left"], region["top"],
                                               region["left"] + region["width"],
                                               region["top"] + region["height"]))
            # Ignore animated background/anti-aliasing; compare a thresholded
            # grayscale image of the number region and require two frames.
            import cv2
            import numpy as np
            gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
            small = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)[1]
            digest = hashlib.blake2s(small.tobytes(), digest_size=8).hexdigest()
            if digest == candidate:
                candidate_count += 1
            else:
                candidate, candidate_count = digest, 1
            if previous is None and candidate_count >= 2:
                previous = digest
            elif previous is not None and digest != previous and candidate_count >= 2:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                path = root / f"max-price-change-{stamp}.png"
                image.save(path)
                print(f"changed={stamp} snapshot={path}", flush=True)
                previous = digest
            time.sleep(0.05)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
