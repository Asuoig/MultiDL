"""Windows 系统托盘图标（纯 ctypes 实现，零第三方依赖）。

- 独立守护线程创建隐藏窗口、注册托盘图标、跑 Win32 消息循环
- 左键单击/双击 → 显示主窗口
- 右键 → 弹出菜单（显示主窗口 / 暂停全部 / 继续全部 / 退出）
- 动作通过 queue 交给主线程（Tk）执行，避免跨线程操作 Tk 控件
"""
from __future__ import annotations

import os
import sys
import ctypes
import queue
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# 消息
WM_DESTROY = 0x0002
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

# Shell_NotifyIcon
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD), ("pt", wintypes.POINT),
    ]


# ---- 关键签名（避免 64 位句柄被截断）----
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreatePopupMenu.restype = wintypes.HANDLE
user32.AppendMenuW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wintypes.HANDLE]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

_UID = 1


def _log(msg, *args):
    try:
        import diag
        diag.get_logger().info(msg, *args)
    except Exception:
        pass


class Tray:
    """menu: [(label, action) 或 (None, None) 表示分隔线]；action ∈ show/pause_all/start_all/quit"""

    def __init__(self, icon_path, tooltip="MultiDL", menu=None):
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.menu = menu or [("显示主窗口", "show"), (None, None), ("退出", "quit")]
        self.actions = queue.Queue()
        self._hwnd = None
        self._hicon = None
        self._menu = None
        self._ids = {}            # menu_id -> action
        self._ready = threading.Event()
        self._thread = None
        self._wndproc = None      # 保持引用防 GC
        self._class_name = f"MultiDLTrayWnd_{id(self)}"

    # ---------- 对外 ----------
    def start(self, timeout=4.0):
        self._thread = threading.Thread(target=self._run, daemon=True, name="MultiDL-tray")
        self._thread.start()
        self._ready.wait(timeout)

    def stop(self):
        if self._hwnd:
            try:
                user32.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                pass

    def set_tooltip(self, text):
        """任意线程可调：更新托盘悬停提示。"""
        if not self._hwnd:
            return
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = _UID
            nid.uFlags = NIF_TIP
            nid.szTip = text[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    # ---------- 线程内部 ----------
    def _load_icon(self):
        h = 0
        if self.icon_path and os.path.exists(self.icon_path):
            h = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not h:
            h = user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
        return h

    def _build_menu(self):
        m = user32.CreatePopupMenu()
        mid = 1
        for label, action in self.menu:
            if label is None:
                user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
                continue
            user32.AppendMenuW(m, MF_STRING, mid, label)
            self._ids[mid] = action
            mid += 1
        return m

    def _on_tray(self, mouse_msg):
        if mouse_msg in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            self.actions.put("show")
        elif mouse_msg == WM_RBUTTONUP:
            self._popup_menu()

    def _popup_menu(self):
        if not self._menu:
            return
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)   # 否则点别处菜单不消失
        cmd = user32.TrackPopupMenu(self._menu,
                                    TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                    pt.x, pt.y, 0, self._hwnd, None)
        if cmd and cmd in self._ids:
            self.actions.put(self._ids[cmd])

    def _wndproc_impl(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                # lParam 低字为鼠标消息
                self._on_tray(int(lparam) & 0xFFFF)
                return 0
            if msg == WM_DESTROY:
                try:
                    nid = NOTIFYICONDATAW()
                    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                    nid.hWnd = hwnd
                    nid.uID = _UID
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                except Exception:
                    pass
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            self._wndproc = WNDPROC(self._wndproc_impl)
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = self._class_name
            user32.RegisterClassW(ctypes.byref(wc))

            self._hwnd = user32.CreateWindowExW(0, self._class_name, "MultiDL Tray", 0,
                                                0, 0, 0, 0, None, None, wc.hInstance, None)
            if not self._hwnd:
                self._ready.set()
                return

            self._hicon = self._load_icon()
            self._menu = self._build_menu()

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = _UID
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._hicon
            nid.szTip = self.tooltip[:127]
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            _log("托盘图标注册: %s", "OK" if ok else "FAIL")
            self._ready.set()

            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            import traceback
            _log("托盘线程异常:\n%s", traceback.format_exc())
            try:
                self._ready.set()
            except Exception:
                pass
