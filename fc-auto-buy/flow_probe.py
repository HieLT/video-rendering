"""Probe LIST -> BUY_DIALOG timing without submitting a purchase."""
from __future__ import annotations

import argparse
import json
import time
import re
from pathlib import Path

import cv2
import numpy as np
import win32api
import win32con
import win32gui

from window_capture import capture_window, find_window
from price_reader import read_price

LAST_BUY_SENT = False


def crop(image, region):
    return image.crop((region["left"], region["top"],
                       region["left"] + region["width"],
                       region["top"] + region["height"]))


def digest(image):
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    return cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)[1].tobytes()


def main() -> int:
    global LAST_BUY_SENT
    LAST_BUY_SENT = False
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-dialog", action="store_true",
                        help="click configured list buy button; never clicks purchase")
    parser.add_argument("--cancel-after", action="store_true",
                        help="after detecting the dialog, click the configured Cancel button")
    parser.add_argument("--baseline-bp", type=int, default=None,
                        help="only cancel when OCR price differs from this BP value")
    parser.add_argument("--buy-once", action="store_true",
                        help="send one buy click when price differs; stops immediately")
    parser.add_argument("--max-bp", type=int, default=None,
                        help="buy guard: detected price must be <= this BP")
    parser.add_argument("--learn-only", action="store_true",
                        help="read first max price, print it, cancel, never buy")
    parser.add_argument("--dry-buy-timing", action="store_true",
                        help="click max price, measure buy dispatch point, then cancel; never buy")
    args = parser.parse_args()
    root = Path(__file__).parent
    cfg = json.loads((root / "regions.json").read_text(encoding="utf-8-sig"))
    hwnd = find_window(cfg.get("window_title", "FC ONLINE"))
    if hwnd is None:
        raise SystemExit("FC ONLINE window not found")
    if not args.open_dialog:
        print("LIST state detected; dry-run only. Pass --open-dialog to open the buy dialog.")
        return 0
    # The game ignores synthetic clicks while it is not the foreground window.
    # Bring it forward before the list-button click.
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.15)
    except Exception:
        pass
    button = cfg["regions"].get("list_buy_button") or cfg["regions"].get("market_buy_button")
    if not button:
        raise SystemExit("market_buy_button is not calibrated")
    left, top, _, _ = win32gui.GetWindowRect(hwnd)
    x = left + button["left"] + button["width"] // 2
    y = top + button["top"] + button["height"] // 2
    before = capture_window(hwnd)
    started = time.perf_counter()
    print("t_click_start_ms=0.0", flush=True)
    # Prefer a window message so this also works when the game runs elevated
    # and Windows denies synthetic desktop cursor input.
    try:
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    except Exception:
        rel_x = button["left"] + button["width"] // 2
        rel_y = button["top"] + button["height"] // 2
        lparam = (rel_y << 16) | rel_x
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    dialog = cfg["regions"].get("market_max_price")
    if not dialog:
        raise SystemExit("market_max_price is not calibrated")
    old = digest(crop(before, dialog))
    # Allow slow server/game responses before declaring the dialog stuck.
    while time.perf_counter() - started < 10:
        current = capture_window(hwnd)
        if digest(crop(current, dialog)) != old:
            changed_at = time.perf_counter()
            print(f"dialog_change_ms={(changed_at - started) * 1000:.1f}", flush=True)
            detected_bp = None
            if args.baseline_bp is not None or args.learn_only:
                stable = 0
                previous_bp = None
                adaptive_started = time.perf_counter()
                while time.perf_counter() - adaptive_started < 1.0:
                    capture_started = time.perf_counter()
                    current = capture_window(hwnd)
                    capture_done = time.perf_counter()
                    sample = crop(current, dialog).resize((680, 280))
                    ocr_started = time.perf_counter()
                    detected_text, candidate_bp = read_price(sample)
                    ocr_done = time.perf_counter()
                    cleaned_text = re.sub(r"(?<=\d)\s*[|Il!1:]\s*(?=[BMK])", "", detected_text, flags=re.I)
                    cleaned_text = re.sub(r"\s+", " ", cleaned_text).strip()
                    if cleaned_text != detected_text:
                        from windows_ocr import normalize_price
                        repaired = normalize_price(cleaned_text)
                        if repaired is not None:
                            detected_text, candidate_bp = cleaned_text, repaired
                    if not re.fullmatch(r"\d+(?:\.\d+)?\s*[BMK]", detected_text.strip(), re.I):
                        candidate_bp = None
                    if candidate_bp is not None:
                        stable += 1
                    else:
                        stable = 0
                    previous_bp = candidate_bp
                    detected_bp = candidate_bp
                    # Fast mode: one syntactically valid OCR result is enough.
                    # Malformed OCR is still rejected before this point.
                    if stable >= 1:
                        break
                    time.sleep(0.03)
                print(f"adaptive_wait_ms={(time.perf_counter()-adaptive_started)*1000:.1f} capture_ms={(capture_done-capture_started)*1000:.1f} ocr_ms={(ocr_done-ocr_started)*1000:.1f}", flush=True)
                if not re.fullmatch(r"\d+(?:\.\d+)?\s*[BMK]", detected_text.strip(), re.I):
                    print(f"ocr_rejected=True raw={detected_text!r}")
                    detected_bp = None
                # Wide plausibility guard: very large jumps are usually OCR
                # losing a decimal separator (e.g. 3.27B -> 3027B). Confirm
                # only suspicious values so normal changes stay one-pass.
                if detected_bp is not None and args.baseline_bp:
                    ratio = detected_bp / args.baseline_bp
                    if ratio < 0.2 or ratio > 5.0:
                        print(f"suspicious_ratio={ratio:.3f} confirm_ocr=True", flush=True)
                        time.sleep(0.03)
                        confirm_img = crop(capture_window(hwnd), dialog).resize((680, 280))
                        confirm_text, confirm_bp = read_price(confirm_img)
                        print(f"confirm_text={confirm_text!r} confirm_bp={confirm_bp}", flush=True)
                        if confirm_bp != detected_bp:
                            print("ocr_confirmation_failed=True", flush=True)
                            detected_bp = None
                debug_path = Path(__file__).with_name("last_price_debug.png")
                sample.save(debug_path)
                print(f"detected_text={detected_text!r} detected_bp={detected_bp} baseline_bp={args.baseline_bp} debug={debug_path}")
                if args.learn_only:
                    print(f"learned_price_bp={detected_bp}")
                    args.cancel_after = True
                    args.baseline_bp = detected_bp
            # Never treat an OCR failure as a price change. This is a hard
            # safety gate for the one-shot purchase mode.
            changed = detected_bp is not None and (args.baseline_bp is None or detected_bp != args.baseline_bp)
            allowed = args.max_bp is None or (detected_bp is not None and detected_bp <= args.max_bp)
            if args.buy_once and not changed and not args.dry_buy_timing:
                # Refresh cycle: unchanged price must close the dialog so the
                # caller can reopen it and request a fresh market snapshot.
                cancel = cfg["regions"].get("cancel_button")
                if cancel:
                    cx = cancel["left"] + cancel["width"] // 2
                    cy = cancel["top"] + cancel["height"] // 2
                    lp = (cy << 16) | cx
                    try:
                        win32api.SetCursorPos((left + cx, top + cy))
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    except Exception:
                        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
                        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
                    print("unchanged_price_cancelled=True")
                return 0
            if args.buy_once and (changed or args.dry_buy_timing) and allowed:
                # Select the displayed maximum-price value first so the game
                # copies it into the editable buy-price field.
                max_box = cfg["regions"].get("market_max_price")
                if max_box:
                    mx = max_box["left"] + max_box["width"] // 2
                    my = max_box["top"] + max_box["height"] // 2
                    mlp = (my << 16) | mx
                    try:
                        win32api.SetCursorPos((left + mx, top + my))
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    except Exception:
                        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, mlp)
                        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, mlp)
                    print(f"max_click_ms={(time.perf_counter()-ocr_done)*1000:.1f}", flush=True)
                if args.dry_buy_timing:
                    print(f"buy_click_ready_ms={(time.perf_counter()-started)*1000:.1f}", flush=True)
                    print("buy_sent=False dry_run=True", flush=True)
                    cancel = cfg["regions"].get("cancel_button")
                    if cancel:
                        cx = cancel["left"] + cancel["width"] // 2
                        cy = cancel["top"] + cancel["height"] // 2
                        clp = (cy << 16) | cx
                        try:
                            win32api.SetCursorPos((left + cx, top + cy))
                            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                        except Exception:
                            win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, clp)
                            win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, clp)
                        print("dry_cancel_sent=True", flush=True)
                    return 0
                buy = cfg["regions"].get("buy_button")
                if not buy:
                    raise SystemExit("buy_button is not calibrated")
                bx = buy["left"] + buy["width"] // 2
                by = buy["top"] + buy["height"] // 2
                lp = (by << 16) | bx
                try:
                    win32api.SetCursorPos((left + bx, top + by))
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                except Exception:
                    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
                    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
                print("buy_sent=True")
                LAST_BUY_SENT = True
                print(f"total_to_buy_ms={(time.perf_counter()-started)*1000:.1f}", flush=True)
                return 0
            if args.learn_only or (args.cancel_after and changed):
                cancel = cfg["regions"].get("cancel_button")
                if not cancel:
                    raise SystemExit("cancel_button is not calibrated")
                cx = cancel["left"] + cancel["width"] // 2
                cy = cancel["top"] + cancel["height"] // 2
                lp = (cy << 16) | cx
                try:
                    win32api.SetCursorPos((left + cx, top + cy))
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                except Exception:
                    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
                    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
                print("cancel_sent=True")
            return 0
        time.sleep(0.05)
    print("dialog_change_timeout_ms=10000")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
