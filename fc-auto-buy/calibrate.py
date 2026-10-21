"""Interactive, read-only region calibration for the FC ONLINE window."""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import ctypes
import win32con
import win32gui

from window_capture import capture_window, find_window


REGIONS = [
    ("player_name", "vùng tên cầu thủ"),
    ("market_rows", "vùng danh sách giá"),
    ("market_price_input", "ô giá mua"),
    ("market_quantity", "ô số lượng"),
    ("buy_button", "nút mua"),
    ("status_success", "trạng thái mua thành công"),
    ("status_failure", "trạng thái lỗi/thất bại"),
]


def main() -> int:
    hwnd = find_window("FC ONLINE")
    if hwnd is None:
        raise SystemExit("FC ONLINE window not found")
    image = capture_window(hwnd)
    frame = cv2.cvtColor(__import__("numpy").asarray(image), cv2.COLOR_RGB2BGR)
    selected: dict[str, dict[str, int]] = {}
    for key, label in REGIONS:
        print(f"Chọn {label}: kéo chuột, Enter xác nhận, Esc bỏ qua")
        title = f"FC Auto Buy calibration - {key}"
        cv2.namedWindow(title, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(title, cv2.WND_PROP_TOPMOST, 1)
        cv2.imshow(title, frame)
        calibration_hwnd = win32gui.FindWindow(None, title)
        if calibration_hwnd:
            win32gui.SetWindowPos(calibration_hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        roi = cv2.selectROI(title, frame, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(title)
        x, y, w, h = (int(v) for v in roi)
        if w and h:
            selected[key] = {"left": x, "top": y, "width": w, "height": h}
    output = Path(__file__).with_name("regions.json")
    output.write_text(json.dumps({"window_title": "FC ONLINE", "regions": selected}, indent=2), encoding="utf-8")
    print(f"saved {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
