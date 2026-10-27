"""任务管理器：持久化、并发槽位、限速、生命周期。"""
from __future__ import annotations

import os
import json
import time
import shutil
import threading
import urllib.parse as up

from .core import Task, SpeedLimiter, DEFAULT_UA


ALLOWED_SCHEMES = ("http", "https", "ftp")


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                   *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}


def sanitize(name: str) -> str:
    """把服务端/用户给的文件名压成安全的单段文件名。

    - 去掉路径分隔符与 Windows 非法字符
    - 去掉控制字符（含 NUL）、首尾空白与结尾的点（Windows 上 "a." 无法创建）
    - "." / ".." / 空 / 保留设备名（CON、NUL、COM1…）→ 回退默认名
    """
    name = str(name or "")
    name = "".join(("_" if (ord(c) < 32 or c in '\\/:*?"<>|') else c) for c in name)
    name = name.strip().strip(".").strip()
    if not name or name in (".", ".."):
        return "download.bin"
    # Windows 保留设备名（不分大小写、带扩展名也生效，如 NUL.txt）
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    return name


def inside_dir(root: str, path: str) -> bool:
    """path 是否位于 root 目录内。

    用 realpath 解析符号链接/目录联接（junction）后再比较 —— 否则下载目录里放一个
    指向 C:\\Windows 的 junction，就能借道把文件写到目录外。
    """
    try:
        r = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        return os.path.commonpath([r, p]) == r
    except Exception:
        return False


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


def _norm_conn(v) -> int:
    """0 / auto / None → 0（自动）；否则夹到 1~32。"""
    if v is None:
        return 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "auto", "自动", "none"):
            return 0
        try:
            v = int(s)
        except ValueError:
            return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return 0 if n <= 0 else max(1, min(n, 32))


class _DynSem:
    """可在线调整并发上限的信号量。

    threading.Semaphore 的计数没法改小，`self._sem = Semaphore(n)` 会让已经
    拿过旧信号量的线程永远还不到新对象上 → 并发上限实际上失效。这里用条件变量。
    """

    def __init__(self, n=1):
        self._limit = max(1, int(n))
        self._cur = 0
        self._cv = threading.Condition()

    def set_limit(self, n):
        with self._cv:
            self._limit = max(1, int(n))
            self._cv.notify_all()

    def acquire(self):
        with self._cv:
            while self._cur >= self._limit:
                self._cv.wait()
            self._cur += 1

    def release(self):
        with self._cv:
            self._cur = max(0, self._cur - 1)
            self._cv.notify()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *a):
        self.release()
        return False


class Manager:
    def __init__(self, data_dir: str, download_dir: str, max_parallel: int = 3,
                 speed_limit: int = 0, default_conn: int = 0):
        self.data_dir = os.path.abspath(data_dir)
        self.download_dir = os.path.abspath(download_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.download_dir, exist_ok=True)
        self.state_file = os.path.join(self.data_dir, "tasks.json")
        self.limiter = SpeedLimiter(speed_limit)
        self.max_parallel = max(1, int(max_parallel))
        self.default_conn = _norm_conn(default_conn)
        self.tasks: dict[str, Task] = {}
        self.order: list[str] = []
        self._lock = threading.RLock()
        self._sem = _DynSem(self.max_parallel)
        self._save_lock = threading.Lock()
        self._dirty = False
        self._on_change = None
        self._last_notify = 0.0
        self.config_file = os.path.join(self.data_dir, "config.json")
        self.clipboard_watch = True
        self.proxy_mode = "auto"            # auto | direct | proxy
        self.proxy_addr = ""                # 自定义代理地址 host:port（可选）
        self._proxy_from_cli = False        # 命令行指定过 --proxy 时，配置不再覆盖
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
            self.default_conn = _norm_conn(c.get("default_conn", self.default_conn))
            self.limiter.set_bps(int(c.get("speed_limit", 0)))
            self._sem = _DynSem(self.max_parallel)
            mode = str(c.get("proxy_mode", "auto")).lower()
            self.proxy_mode = mode if mode in ("auto", "direct", "proxy") else "auto"
            self.proxy_addr = str(c.get("proxy_addr") or "").strip()
        except Exception:
            pass
        # 命令行没显式指定 --proxy 时，用配置里的网络模式
        if not self._proxy_from_cli:
            try:
                from .proxy import set_override
                set_override(self._effective_proxy())
            except Exception:
                pass

    def _effective_proxy(self):
        """代理模式下有自定义地址就用它，否则交给 proxy 模块自动解析。"""
        if self.proxy_mode == "proxy" and self.proxy_addr:
            return self.proxy_addr
        return self.proxy_mode

    def _save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump({"clipboard_watch": self.clipboard_watch,
                           "download_dir": self.download_dir,
                           "max_parallel": self.max_parallel,
                           "default_conn": self.default_conn,
                           "speed_limit": self.limiter.bps,
                           "proxy_mode": self.proxy_mode,
                           "proxy_addr": self.proxy_addr},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def set_proxy_mode(self, mode: str):
        """界面切换网络模式：auto（自动）/ direct（直连）/ proxy（强制代理）。"""
        mode = str(mode or "auto").lower()
        if mode not in ("auto", "direct", "proxy"):
            mode = "auto"
        self.proxy_mode = mode
        self._proxy_from_cli = False
        try:
            from .proxy import set_override
            set_override(self._effective_proxy())
        except Exception:
            pass
        self._save_config()

    def set_proxy_addr(self, addr: str):
        """设置自定义代理地址（host:port）；空字符串表示清除。"""
        self.proxy_addr = (addr or "").strip()
        self.proxy_mode = "proxy" if self.proxy_addr else self.proxy_mode
        self._proxy_from_cli = False
        try:
            from .proxy import set_override
            set_override(self._effective_proxy())
        except Exception:
            pass
        self._save_config()

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

    def _write_state(self):
        with self._save_lock:
            payload = {"tasks": [self.tasks[i].to_dict() for i in self.order
                                 if i in self.tasks]}
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)

    def _saver(self):
        while True:
            time.sleep(1.5)
            if not self._dirty:
                continue
            try:
                self._write_state()
            except Exception:
                continue              # 写失败就保持 dirty，下一轮重试（原来会先清标志，直接丢这次变更）
            self._dirty = False

    def save_now(self):
        """退出前同步落盘 —— 只置 dirty 标志的话，进程退出时可能还没写进磁盘。"""
        self._dirty = True
        try:
            self._write_state()
            self._dirty = False
        except Exception:
            pass

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
        u0 = up.urlsplit(url)
        if not u0.scheme:
            url = "https://" + url
            u0 = up.urlsplit(url)
        scheme = (u0.scheme or "").lower()
        if scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"不支持的协议：{scheme or '(空)'}（仅支持 http / https / ftp）")
        if scheme in ("http", "https", "ftp") and not u0.hostname:
            raise ValueError("URL 缺少主机名")

        auto_named = not target
        if target:
            # 落盘位置必须在下载目录内：挡住「任意网页让下载器写任意路径」
            target = os.path.abspath(target)
            if not inside_dir(self.download_dir, target):
                raise ValueError("保存路径必须在下载目录内")
        if not target:
            name = filename_hint or self._name_from_url(url)
            target = unique_path(os.path.join(self.download_dir, sanitize(name)))

        t = Task(url, target, headers=headers or {}, ua=ua or DEFAULT_UA,
                 referer=referer, cookie=cookie,
                 max_conn=max_conn if max_conn is not None else self.default_conn,
                 filename_hint=sanitize(filename_hint) if filename_hint else None)
        # 名字是按 URL 猜的 → 探测拿到真实文件名后允许修正
        t._auto_named = auto_named and not filename_hint
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
        if not t or t.state == "done":
            return False
        # 兜底校验：历史遗留（或被人塞进 tasks.json）的越权路径一律不执行
        if not inside_dir(self.download_dir, t.target):
            t.state = "error"
            t.message = "保存路径不在下载目录内，请重新添加该任务"
            self._dirty = True
            self._notify()
            return False
        # 幂等：连点两次「开始」不能起两套线程去抢同一个 part 文件
        with t._lock:
            if t._started:
                return False
            t._started = True
        t.stop.clear()
        t.pause.clear()
        t.state = "queued"
        th = threading.Thread(target=self._worker, args=(t,), daemon=True)
        try:
            th.start()
        except Exception as e:
            # 线程/内存耗尽时 Thread.start() 会抛异常。若不回滚，_started 永久为 True，
            # 这个任务就再也起不来了（只有重启进程能救）—— 这是 _started 闸门带来的新风险。
            with t._lock:
                t._started = False
            t.state = "error"
            t.message = f"无法启动下载线程：{e}"
            self._dirty = True
            self._notify()
            return False
        t._thread = th
        return True

    def _worker(self, t: Task):
        try:
            with self._sem:
                if t.stop.is_set():
                    return
                t.run(self.limiter, self._notify)
                self._dirty = True
                self._notify()
        finally:
            with t._lock:
                t._started = False
                pending = getattr(t, "_resume_pending", False)
                if pending:
                    t._resume_pending = False
            # 「暂停后立刻继续」时旧线程还在收尾：由它自己退出后重启，
            # 这样 resume() 不必在界面线程里 join（那会冻结 GUI）
            if pending and not t.stop.is_set():
                t.resume_task()
                self.start(t.id)

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
        th = getattr(t, "_thread", None)
        if th is not None and th.is_alive():
            # 旧线程还在收尾。绝不能在这里 join —— resume() 跑在 Tk 主线程，
            # join 会冻结界面，「全部开始」还会按任务数累乘成几十秒假死。
            # 打个标记，交给 worker 的 finally 自己重启。
            t._resume_pending = True
            t.message = "正在停止，稍后自动继续"
            self._notify()
            return True
        prev = t.state
        t.resume_task()                       # 置 running（会清 pause 标志）
        if not self.start(tid):
            # 起线程失败 → 回滚状态，别留下「running 但没有线程」的假活任务
            cur = getattr(t, "_thread", None)
            if not (cur is not None and cur.is_alive()):
                t.state = prev if prev in ("paused", "error") else "paused"
                if t.state == "paused":
                    t.message = "已暂停"
            return False
        return True

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
        self._dirty = True
        self._notify()

        def _cleanup():
            # 等旧线程退出再删，否则 Windows 上句柄未释放会删不掉
            th = getattr(t, "_thread", None)
            if th is not None:
                th.join(timeout=10)
            # 临时产物也必须在下载目录内才删（防止 target 被指到别处时误删他人文件）
            if inside_dir(self.download_dir, t.tmpdir):
                shutil.rmtree(t.tmpdir, ignore_errors=True)
            for p in (t.part_path, t.meta_path):
                try:
                    if inside_dir(self.download_dir, p) and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            if delete_file and inside_dir(self.download_dir, t.target):
                try:
                    if os.path.exists(t.target):
                        os.remove(t.target)
                except Exception:
                    pass

        th = getattr(t, "_thread", None)
        if th is not None and th.is_alive():
            threading.Thread(target=_cleanup, daemon=True).start()   # 不阻塞界面线程
        else:
            _cleanup()
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
            try:
                n = int(max_parallel)
            except (TypeError, ValueError):
                raise ValueError("max_parallel 必须是整数")
            self.max_parallel = max(1, min(n, 32))
            self._sem.set_limit(self.max_parallel)      # 原地调整，已持锁的线程不受影响
        if speed_limit is not None:
            try:
                bps = int(speed_limit)
            except (TypeError, ValueError):
                raise ValueError("speed_limit 必须是整数")
            self.limiter.set_bps(max(0, bps))
        if default_conn is not None:
            self.default_conn = _norm_conn(default_conn)
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
