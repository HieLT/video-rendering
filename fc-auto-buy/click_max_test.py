"""Test only: click the displayed maximum-price value; never clicks Buy."""
import time
import win32api, win32con, win32gui

h = win32gui.FindWindow(None, "FC ONLINE")
if not h:
    raise SystemExit("FC ONLINE window not found")
left, top, right, bottom = win32gui.GetWindowRect(h)
# Current fixed 1296x759 layout: value beside 'Giá tối đa'.
x, y = left + 1012, top + 331
win32gui.ShowWindow(h, win32con.SW_RESTORE)
win32gui.SetForegroundWindow(h)
time.sleep(0.25)
win32api.SetCursorPos((x, y))
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
print(f"clicked_max_value={x},{y}")
