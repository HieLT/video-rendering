"""Repeated safe loop: open dialog, compare OCR price, then cancel."""
from __future__ import annotations
import argparse, sys, time, ctypes
from pathlib import Path
import win32gui
import flow_probe

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline-bp", type=int, default=7390000000)
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--delay-ms", type=int, default=250)
    args = p.parse_args()
    game = win32gui.FindWindow(None, "FC ONLINE")
    if not game:
        print("FC ONLINE window not found", file=sys.stderr)
        return 2
    probe = Path(__file__).with_name("flow_probe.py")
    print("Press F8 to stop safely.", flush=True)
    for i in range(1, args.count + 1):
        if win32gui.GetForegroundWindow() != game:
            print("stopped_user_interaction=True", flush=True)
            return 0
        print(f"iteration={i}/{args.count}", flush=True)
        if ctypes.windll.user32.GetAsyncKeyState(0x77) & 0x8000:
            print("stopped_hotkey=F8", flush=True)
            return 0
        old_argv = sys.argv
        sys.argv = [str(probe), "--open-dialog", "--cancel-after", "--buy-once",
                    "--baseline-bp", str(args.baseline_bp)]
        try:
            result_code = flow_probe.main()
        finally:
            sys.argv = old_argv
        if result_code == 0 and getattr(flow_probe, "LAST_BUY_SENT", False):
            print("one_shot_complete=True", flush=True)
            return 0
        if result_code == 1:
            print("stopping_on_timeout=True", flush=True)
            return 1
        if result_code != 0:
            return result_code
        time.sleep(args.delay_ms / 1000)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
