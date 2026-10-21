"""Prevent interactive browsers from being launched on an invisible desktop."""
import sys


def current_desktop():
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user32.GetThreadDesktop.restype = wintypes.HANDLE
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL
    desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    name = ctypes.create_unicode_buffer(1024)
    needed = wintypes.DWORD()
    if not desktop or not user32.GetUserObjectInformationW(
        desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return name.value


def require_visible_desktop():
    desktop = current_desktop()
    if desktop is not None and desktop.casefold() != "default":
        raise RuntimeError(
            f"Cannot open visible browsers from Windows desktop {desktop!r}. "
            "Start run_server.py from the user's Default desktop. "
            "When launching through Codex, use an approved unsandboxed command; "
            "a hidden console does not fix a hidden desktop."
        )
