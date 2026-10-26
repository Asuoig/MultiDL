"""任务管理器：持久化、并发槽位、限速、生命周期。"""
from __future__ import annotations

import os
import json
import time
import shutil
import threading
import urllib.parse as up

from .core import Task, SpeedLimiter, DEFAULT_UA


def sanitize(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "download.bin"


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{root} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


class Manager:
    def __init__(self, data_dir: str, download_dir: str, max_parallel: int = 3,
                 speed_limit: int = 0, default_conn: int = 8):
        self.data_dir = os.path.abspath(data_dir)
        self.download_dir = os.path.abspath(download_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.download_dir, exist_ok=True)
        self.state_file = os.path.join(self.data_dir, "tasks.json")
        self.limiter = SpeedLimiter(speed_limit)
        self.max_parallel = max(1, int(max_parallel))
        self.default_conn = max(1, min(int(default_conn), 32))
        self.tasks: dict[str, Task] = {}
        self.order: list[str] = []
        self._lock = threading.RLock()
        self._sem = threading.Semaphore(self.max_parallel)
        self._save_lock = threading.Lock()
        self._dirty = False
        self._on_change = None
        self._last_notify = 0.0
        self.config_file = os.path.join(self.data_dir, "config.json")
        self.clipboard_watch = True
        self._load_config()
        self._load()
        threading.Thread(target=self._saver, daemon=True).start()

    # ---------------- 配置 ----------------
    def _load_config(self):
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                c = json.load(f)
            self.clipboard_watch = bool(c.get("clipboard_watch", True))
            if c.get("download_dir"):
                d = os.path.abspath(c["download_dir"])
                if d != self.download_dir:
                    self.download_dir = d
                    os.makedirs(d, exist_ok=True)
            self.max_parallel = max(1, int(c.get("max_parallel", self.max_parallel)))
            self.default_conn = max(1, min(int(c.get("default_conn", self.default_conn)), 32))
            self.limiter.set_bps(int(c.get("speed_limit", 0)))
            self._sem = threading.Semaphore(self.max_parallel)
        except Exception:
            pass

    def _save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump({"clipboard_watch": self.clipboard_watch,
                           "download_dir": self.download_dir,
                           "max_parallel": self.max_parallel,
                           "default_conn": self.default_conn,
                           "speed_limit": self.limiter.bps},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def set_clipboard_watch(self, on: bool):
        self.clipboard_watch = bool(on)
        self._save_config()

    # ---------------- 通知 ----------------
    def set_on_change(self, cb):
        self._on_change = cb

    def _notify(self, *_):
        """节流到 ~8Hz，避免 GUI 被刷爆。"""
        now = time.monotonic()
        if now - self._last_notify < 0.12:
            return
        self._last_notify = now
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    # ---------------- 持久化 ----------------
    def _load(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            for t in d.get("tasks", []):
                task = Task.from_dict(t)
                self.tasks[task.id] = task
                self.order.append(task.id)
        except Exception:
            pass

    def _saver(self):
        while True:
            time.sleep(1.5)
            if not self._dirty:
                continue
            self._dirty = False
            try:
                with self._save_lock:
                    payload = {"tasks": [self.tasks[i].to_dict() for i in self.order
                                         if i in self.tasks]}
                    tmp = self.state_file + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=1)
                    os.replace(tmp, self.state_file)
            except Exception:
                pass

    def save_now(self):
        self._dirty = True

    # ---------------- 查询 ----------------
    def snapshots(self):
        with self._lock:
            return [self.tasks[i].snapshot() for i in self.order if i in self.tasks]

    def get(self, tid):
        with self._lock:
            return self.tasks.get(tid)

    # ---------------- 新增 ----------------
    def add(self, url: str, *, target=None, referer=None, cookie=None, ua=None,
            headers=None, max_conn=None, filename_hint=None, autostart=True):
        url = (url or "").strip()
        if not url:
            raise ValueError("URL 为空")
        if not up.urlsplit(url).scheme:
            url = "https://" + url

        if not target:
            name = filename_hint or self._name_from_url(url)
            target = unique_path(os.path.join(self.download_dir, sanitize(name)))

        t = Task(url, target, headers=headers or {}, ua=ua or DEFAULT_UA,
                 referer=referer, cookie=cookie,
                 max_conn=max_conn or self.default_conn,
                 filename_hint=filename_hint)
        with self._lock:
            self.tasks[t.id] = t
            self.order.insert(0, t.id)
        self._dirty = True
        if autostart:
            self.start(t.id)
        self._notify()
        return t.snapshot()

    @staticmethod
    def _name_from_url(url: str) -> str:
        u = up.urlsplit(url)
        base = os.path.basename(up.unquote(u.path)) or ""
        if not base or base.endswith("/"):
            base = f"{u.hostname or 'download'}.bin"
        return base

    # ---------------- 生命周期 ----------------
    def start(self, tid):
        t = self.get(tid)
        if not t or t._runner or t.state == "done":
            return False
        t.stop.clear()
        t.pause.clear()
        t.state = "queued"
        threading.Thread(target=self._worker, args=(t,), daemon=True).start()
        return True

    def _worker(self, t: Task):
        with self._sem:
            if t.stop.is_set():
                return
            t.run(self.limiter, self._notify)
            self._dirty = True
            self._notify()

    def pause(self, tid):
        t = self.get(tid)
        if t and t.state in ("running", "probing", "queued"):
            t.pause_task()
            self._notify()
            return True
        return False

    def resume(self, tid):
        t = self.get(tid)
        if not t or t.state not in ("paused", "error"):
            return False
        t.resume_task()
        return self.start(tid)

    def cancel(self, tid):
        t = self.get(tid)
        if not t:
            return False
        t.cancel()
        return True

    def remove(self, tid, delete_file=False):
        with self._lock:
            t = self.tasks.pop(tid, None)
            if tid in self.order:
                self.order.remove(tid)
        if not t:
            return False
        t.cancel()
        time.sleep(0.2)
        shutil.rmtree(t.tmpdir, ignore_errors=True)
        for p in (t.part_path, t.meta_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        if delete_file:
            try:
                if os.path.exists(t.target):
                    os.remove(t.target)
            except Exception:
                pass
        self._dirty = True
        self._notify()
        return True

    def start_all(self):
        for tid in list(self.order):
            t = self.tasks.get(tid)
            if t and t.state in ("paused", "error"):
                self.resume(tid)
            elif t and t.state == "queued":
                self.start(tid)

    def pause_all(self):
        for tid in list(self.order):
            self.pause(tid)

    # ---------------- 设置 ----------------
    def set_limits(self, max_parallel=None, speed_limit=None, default_conn=None):
        if max_parallel is not None:
            self.max_parallel = max(1, int(max_parallel))
            self._sem = threading.Semaphore(self.max_parallel)
        if speed_limit is not None:
            self.limiter.set_bps(int(speed_limit))
        if default_conn is not None:
            self.default_conn = max(1, min(int(default_conn), 32))
        self._dirty = True
        self._save_config()
        return self.settings()

    def set_download_dir(self, path):
        self.download_dir = os.path.abspath(path)
        os.makedirs(self.download_dir, exist_ok=True)
        self._dirty = True
        self._save_config()

    def settings(self):
        return {
            "download_dir": self.download_dir,
            "data_dir": self.data_dir,
            "max_parallel": self.max_parallel,
            "speed_limit": self.limiter.bps,
            "default_conn": self.default_conn,
            "clipboard_watch": self.clipboard_watch,
        }

    def stats(self):
        snap = self.snapshots()
        active = [s for s in snap if s["state"] in ("running", "probing")]
        return {
            "total": len(snap),
            "active": len(active),
            "speed": sum(s["speed"] for s in active),
            "waiting": len([s for s in snap if s["state"] in ("queued", "paused")]),
            "done": len([s for s in snap if s["state"] == "done"]),
        }
