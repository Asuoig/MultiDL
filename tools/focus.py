"""把指定标题的窗口提到前台（纯 ctypes），用于截图。"""
import sys
import time
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

TITLE = sys.argv[1] if len(sys.argv) > 1 else "MultiDL"


def find(title_part):
    hits = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_part in buf.value:
                hits.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return hits


hits = find(TITLE)
if not hits:
    print("not found")
    sys.exit(1)
hwnd, name = hits[0]
SW_RESTORE = 9
user32.ShowWindow(hwnd, SW_RESTORE)
user32.SetForegroundWindow(hwnd)
user32.BringWindowToTop(hwnd)
time.sleep(0.6)
rect = wintypes.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
print(name, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
