"""Window discovery and capture helpers for the read-only prototype."""
from __future__ import annotations

from pathlib import Path

import win32gui
import win32ui
import win32con
from PIL import Image, ImageGrab
import ctypes
import time


def find_window(title: str = "FC ONLINE") -> int | None:
    found: list[int] = []
    def visit(hwnd: int, _extra: object) -> None:
        if win32gui.IsWindowVisible(hwnd) and title.casefold() in win32gui.GetWindowText(hwnd).casefold():
            found.append(hwnd)
    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def capture_window(hwnd: int):
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    if right <= left or bottom <= top:
        raise RuntimeError("window has no visible rectangle")
    width, height = right - left, bottom - top
    # PrintWindow asks the target to render itself. This works for game windows
    # that are not captured correctly by a desktop BitBlt.
    window_dc = win32gui.GetWindowDC(hwnd)
    dc = win32ui.CreateDCFromHandle(window_dc)
    compatible = dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(dc, width, height)
    compatible.SelectObject(bitmap)
    ok = ctypes.windll.user32.PrintWindow(hwnd, compatible.GetSafeHdc(), 2)
    if not ok:
        return ImageGrab.grab((left, top, right, bottom), include_layered_windows=True)
    info = bitmap.GetInfo()
    bits = bitmap.GetBitmapBits(True)
    image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    dc.DeleteDC()
    compatible.DeleteDC()
    win32gui.ReleaseDC(hwnd, window_dc)
    win32gui.DeleteObject(bitmap.GetHandle())
    return image


def main() -> int:
    hwnd = find_window()
    if hwnd is None:
        raise SystemExit("FC ONLINE window not found")
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    image = capture_window(hwnd)
    out = Path(__file__).with_name("window_probe.png")
    image.save(out)
    print(f"hwnd={hwnd} size={image.size} saved={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
