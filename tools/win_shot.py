"""聚焦窗口并截图（一步到位）。"""
import os
import sys
import time
import ctypes
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from shot import grab  # noqa: E402

user32 = ctypes.windll.user32
P = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

title_part = sys.argv[1] if len(sys.argv) > 1 else "MultiDL"
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "..", "test", "win.png")

hits = []


def cb(h, _):
    if user32.IsWindowVisible(h):
        n = user32.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, b, n + 1)
            if title_part in b.value:
                hits.append((h, b.value))
    return True


user32.EnumWindows(P(cb), 0)
if not hits:
    print("not found")
    sys.exit(1)

h, name = hits[0]
user32.ShowWindow(h, 9)
user32.SetForegroundWindow(h)
time.sleep(0.8)
r = wintypes.RECT()
user32.GetWindowRect(h, ctypes.byref(r))
try:
    dpi = user32.GetDpiForWindow(h) or 96
except Exception:
    dpi = 96
k = dpi / 96.0
x, y = int(r.left * k), int(r.top * k)
w, ht = int((r.right - r.left) * k), int((r.bottom - r.top) * k)
print("shot", name, "dpi", dpi, (x, y, w, ht))
print(grab(out, x, y, w, ht))
