"""MultiDL 诊断与崩溃上报。

- 把日志写到 %LOCALAPPDATA%\\MultiDL\\logs\\multidl.log（滚动，最多 3 份 × 2MB）
- 捕获主线程 / 子线程 / tkinter 回调里未处理的异常，写日志并弹窗
- 崩溃单独再记一份 crash.log，方便定位
- 提供"复制诊断信息"（环境 + 最近日志）给用户贴到 Issue
- 窗口版 exe 没有控制台，这套是用户出错时唯一能拿到线索的通道
"""
from __future__ import annotations

import os
import sys
import platform
import logging
import datetime
import traceback
import threading
from logging.handlers import RotatingFileHandler

APP_NAME = "MultiDL"
APP_VERSION = "1.0.5"

_root = None          # tkinter 根窗口（装好界面后由 install(root) 赋值）
_log_file = None
_setup_done = False


# ---------------- 路径 ----------------
def _base_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_NAME)


def log_dir():
    d = os.path.join(_base_dir(), "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def log_file():
    return os.path.join(log_dir(), "multidl.log")


# ---------------- 环境信息 ----------------
def collect_info():
    frozen = bool(getattr(sys, "frozen", False))
    lines = [
        f"{APP_NAME} {APP_VERSION}",
        f"OS: {platform.platform()}",
        f"Python: {sys.version.split()[0]} ({'frozen/exe' if frozen else 'source'})",
        f"Executable: {sys.executable}",
        f"Log file: {log_file()}",
    ]
    return "\n".join(lines)


# ---------------- 日志 ----------------
def setup_logging(level=logging.INFO):
    global _log_file, _setup_done
    if _setup_done:
        return _log_file
    _log_file = log_file()
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # 文件
    try:
        fh = RotatingFileHandler(_log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass
    # 有控制台时也打一份（开发/命令行模式）
    if sys.stderr is not None and sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    _setup_done = True
    logging.getLogger(APP_NAME).info("==== 启动 ====\n%s", collect_info())
    return _log_file


def get_logger(name=APP_NAME):
    return logging.getLogger(name)


# ---------------- 崩溃处理 ----------------
def _write_crash(kind, text):
    try:
        with open(os.path.join(log_dir(), "crash.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} [{kind}] =====\n")
            f.write(text.rstrip() + "\n")
    except Exception:
        pass


def _show_dialog(title, body):
    """优先用 tkinter（可选中复制）；界面还没起来时退回 Win32 MessageBox。"""
    if _root is not None:
        try:
            from tkinter import messagebox
            messagebox.showerror(title, body, parent=_root)
            return
        except Exception:
            pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, body, title, 0x10)  # MB_ICONERROR
    except Exception:
        # 最后兜底：写到 stderr（命令行模式可见）
        try:
            sys.stderr.write(f"{title}: {body}\n")
        except Exception:
            pass


def handle_exception(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    get_logger().error("未捕获异常:\n%s", text)
    _write_crash("main", text)
    body = (
        "程序遇到一个错误，已写入本地日志。\n\n"
        f"错误：{exc_type.__name__}: {exc}\n\n"
        f"日志目录：\n{log_dir()}\n\n"
        "请把 logs 目录里的 multidl.log / crash.log 发给开发者（或打开「帮助 → 复制诊断信息」）。"
    )
    _show_dialog(f"{APP_NAME} 出错了", body)


def handle_thread_exception(args):
    name = args.thread.name if getattr(args, "thread", None) else "?"
    text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    get_logger().error("子线程异常 (%s):\n%s", name, text)
    _write_crash("thread", text)


def _tk_callback_exception(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    get_logger().error("界面回调异常:\n%s", text)
    _write_crash("tk", text)
    try:
        from tkinter import messagebox
        messagebox.showerror(
            f"{APP_NAME} 出错了",
            f"界面操作出错，已记录日志。\n\n{exc_type.__name__}: {exc}\n\n日志目录：\n{log_dir()}",
            parent=_root,
        )
    except Exception:
        pass


def install(root=None):
    """装全局异常钩子。root 为 tkinter 根窗口时，一并接管界面回调异常。"""
    global _root
    if root is not None:
        _root = root
        try:
            root.report_callback_exception = _tk_callback_exception
        except Exception:
            pass
    sys.excepthook = handle_exception
    if hasattr(threading, "excepthook"):
        threading.excepthook = handle_thread_exception


# ---------------- 给用户复制 / 打开日志 ----------------
def read_recent_log(max_bytes=60000):
    p = log_file()
    try:
        if not os.path.exists(p):
            return "(暂无日志)"
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read()
    except Exception as e:
        return f"(读取日志失败: {e})"


def report_text():
    """「复制诊断信息」用：环境 + 最近日志，直接贴到 GitHub Issue。"""
    return (
        "===== 环境信息 =====\n"
        f"{collect_info()}\n\n"
        "===== 最近日志 =====\n"
        f"{read_recent_log()}\n"
    )


def open_log_folder():
    d = log_dir()
    try:
        if sys.platform == "win32":
            os.startfile(d)  # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["xdg-open", d])
    except Exception as e:
        get_logger().warning("打开日志目录失败: %s", e)
