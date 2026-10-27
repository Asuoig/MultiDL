"""
engine/core.py — 高性能多线程分段下载引擎 v2

与 v1 的关键差异（都是为了打满带宽）：
  · 单文件预分配 + 各连接独立句柄 seek/write —— 不再分块临时文件后合并，
    省掉一整趟「读所有分块 + 写目标文件」的磁盘往返（大文件上是最大瓶颈之一）
  · read1(256KB) 单次 recv，减少 Python 层循环次数
  · 分块状态存旁车 JSON，进程重启可直接续传
  · 自适应增连：追最慢连接，剩余时间 > 建连成本×系数才追加
"""
from __future__ import annotations

import os
import re
import ssl
import json
import time
import shutil
import socket
import threading
import http.client
import urllib.parse as up

from .proxy import proxy_for


def _make_conn(scheme, host, port, timeout, allow_proxy=True):
    """按代理配置创建连接；有代理则走 CONNECT 隧道（HTTPS/HTTP 都支持）。"""
    px = proxy_for(host) if allow_proxy else None
    if px:
        ph, pp = px
        if scheme == "https":
            c = http.client.HTTPSConnection(ph, pp, timeout=timeout,
                                            context=ssl.create_default_context())
        else:
            c = http.client.HTTPConnection(ph, pp, timeout=timeout)
        c.set_tunnel(host, port)
        return c
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=ssl.create_default_context())
    return http.client.HTTPConnection(host, port, timeout=timeout)


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MIN_CHUNK = 1 << 20
MAX_CONN_HARD = 32
GROW_POLL = 0.5
HANDSHAKE_COST = 1.2
READ_BLOCK = 1 << 18          # 256 KiB
MIN_SANE_UNKNOWN = 4096       # 长度未知(chunked)时，小于此字节数视为"假响应"
PART_SUFFIX = ".dlpart"
META_SUFFIX = ".dlmeta"
CHUNK_MAX_TRIES = 3           # 单个分块最多重试次数（超过就判定任务失败，避免死循环）
MAX_DECLARED_SIZE = 8 << 40   # 服务器声明的文件大小硬上限（8 TiB），防异常值
PREALLOC_LIMIT = 64 << 30     # 超过 64 GiB 不再预分配（避免 NTFS 提前占满磁盘）

# 任务 ID：毫秒时间戳 + 进程内自增序号 + URL 散列。
# 旧实现只用「毫秒 + hash(url)%1000」，同一毫秒添加同一 URL 会撞 ID，
# 导致任务互相覆盖、order 出现重复项（界面出现重复行且删不掉）。
_id_lock = threading.Lock()
_id_seq = 0


def _new_task_id(url):
    global _id_seq
    with _id_lock:
        _id_seq = (_id_seq + 1) % 1000
        seq = _id_seq
    return "%09d%03d%03d" % (int(time.time() * 1000) % 10 ** 9,
                             abs(hash(url)) % 1000, seq)


class NoRangeError(Exception):
    pass


class FatalError(Exception):
    """不可恢复错误（尺寸异常 / 空间不足）：重试和兜底都不会成功，直接失败。"""


def _clean_name(name):
    """压成安全的单段文件名：去路径分隔符/非法字符/控制字符，去掉结尾的点与空白。"""
    s = "".join(("_" if (ord(c) < 32 or c in '\\/:*?"<>|') else c) for c in str(name or ""))
    s = s.strip().strip(".").strip()
    return s[:150] or "download.bin"


class Aborted(Exception):
    pass


# --------------------------------------------------------------------------
class SpeedLimiter:
    def __init__(self, bps: int = 0):
        self.bps = int(bps)
        self._lock = threading.Lock()
        self._allow = 0.0
        self._last = time.monotonic()

    def set_bps(self, bps: int):
        with self._lock:
            self.bps = max(0, int(bps))
            self._allow = 0.0
            self._last = time.monotonic()

    def consume(self, n: int, stop=lambda: False):
        if not self.bps:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._allow += (now - self._last) * self.bps
                self._last = now
                cap = self.bps * 0.5
                if self._allow > cap:
                    self._allow = cap
                if self._allow >= n:
                    self._allow -= n
                    return
                need = n - self._allow
            if stop():
                raise Aborted()
            time.sleep(min(0.2, max(0.01, need / self.bps)))


# --------------------------------------------------------------------------
class Chunk:
    __slots__ = ("idx", "start", "end", "pos", "done", "error", "claimed")

    def __init__(self, idx, start, end):
        self.idx = idx
        self.start = start
        self.end = end
        self.pos = start
        self.done = False
        self.error = None
        self.claimed = False        # 运行时状态：已被某个 worker 领走（不持久化）

    @property
    def size(self):
        return 0 if self.end < 0 else self.end - self.start + 1

    @property
    def remaining(self):
        if self.done:
            return 0
        if self.end < 0:            # end<0 = 长度未知（chunked），一直读到 EOF
            return 1
        return max(0, self.end - self.pos + 1)

    def d(self):
        return [self.idx, self.start, self.end, self.pos, 1 if self.done else 0]

    @classmethod
    def load(cls, a):
        c = cls(a[0], a[1], a[2])
        c.pos = a[3]
        c.done = bool(a[4])
        return c


# --------------------------------------------------------------------------
class Conn:
    """一条连接：负责一个分块的字节搬运。"""

    def __init__(self, task: "Task", chunk: Chunk):
        self.t = task
        self.c = chunk
        self.speed = 0.0
        self._n = 0
        self._t0 = time.monotonic()

    def _open(self):
        t = self.t
        url = t.final_url or t.url
        u = up.urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")

        h = dict(t.headers)
        h.setdefault("User-Agent", t.ua or DEFAULT_UA)
        h["Accept-Encoding"] = "identity"
        h["Connection"] = "close"
        if t.supports_range:
            h["Range"] = f"bytes={self.c.pos}-{self.c.end}"
            if t.etag:
                h["If-Range"] = t.etag
            elif t.last_modified:
                h["If-Range"] = t.last_modified

        # 按自动选路结果决定；未决定时代理优先、失败退直连
        attempts = t.proxy_attempts(host)
        conn = resp = None
        for i, use_proxy in enumerate(attempts):
            try:
                conn = _make_conn(u.scheme, host, port, 25, allow_proxy=use_proxy)
                conn.request("GET", path, headers=h)
                resp = conn.getresponse()
                # 代理返回 403/407/502/503 → 多半是代理 IP 被目标站拦（如国内镜像），换直连重试
                if use_proxy and resp.status in (403, 407, 502, 503) and i < len(attempts) - 1:
                    conn.close()
                    conn = resp = None
                    continue
                break
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = resp = None
                if i == len(attempts) - 1:
                    raise
        if resp is None:
            raise IOError("连接失败")
        self.conn = conn
        self.resp = resp

        if resp.status == 206:
            return
        if resp.status == 200:
            if t.supports_range:
                raise NoRangeError("ignored Range")
            return
        if resp.status == 416:
            self.c.pos = self.c.end + 1
            self.c.done = True
            raise Aborted()
        raise IOError(f"HTTP {resp.status} {resp.reason}")

    def run(self, stop: threading.Event, pause: threading.Event,
            limiter: SpeedLimiter, on_bytes):
        t = self.t
        path = t.part_path
        # 每连接独立句柄，seek 到自己的写位置 —— 互不干扰，无需加锁
        fh = open(path, "r+b", buffering=0)
        try:
            self._open()
            unknown = self.c.end < 0            # 长度未知（chunked）：读到 EOF 为止
            buf = bytearray(READ_BLOCK)
            while unknown or self.c.pos <= self.c.end:
                if stop.is_set() or pause.is_set():
                    raise Aborted()
                want = READ_BLOCK if unknown else min(READ_BLOCK, self.c.end - self.c.pos + 1)
                mv = memoryview(buf)[:want]
                n = self.resp.readinto(mv) if hasattr(self.resp, "readinto") else 0
                if n == 0:
                    data = self.resp.read1(want)
                    n = len(data)
                    if n == 0:
                        if unknown:
                            break                # 正常读完（chunked 以 EOF 结束）
                        raise IOError("unexpected EOF")
                    mv = data
                limiter.consume(n, stop=stop.is_set)
                fh.seek(self.c.pos)
                fh.write(mv if isinstance(mv, (bytes, bytearray)) else mv[:n])
                self.c.pos += n
                self._n += n
                on_bytes(n)
                now = time.monotonic()
                dt = now - self._t0
                if dt >= 0.35:
                    self.speed = self._n / dt
                    self._n = 0
                    self._t0 = now
            self.c.done = True
        finally:
            try:
                fh.close()
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass

    def eta(self):
        if self.c.done:
            return 0.0
        return self.c.remaining / max(self.speed, 2048.0)


# --------------------------------------------------------------------------
class Task:
    def __init__(self, url, target, *, headers=None, ua=None, referer=None,
                 cookie=None, max_conn=8, task_id=None, filename_hint=None):
        self.id = task_id or _new_task_id(url)
        self.url = url
        self.final_url = None
        self.target = target
        self.headers = dict(headers or {})
        self.ua = ua or DEFAULT_UA
        if referer:
            self.headers.setdefault("Referer", referer)
        if cookie:
            self.headers.setdefault("Cookie", cookie)
        # max_conn: None / 0 / "auto" 表示按文件大小自动决定（4MB/连接，上限 32）
        if max_conn in (None, 0, "auto", "自动"):
            self.max_conn = None
        else:
            self.max_conn = max(1, min(int(max_conn), MAX_CONN_HARD))
        self.filename_hint = filename_hint

        self.size = 0
        self.etag = None
        self.last_modified = None
        self.content_type = None
        self.content_disposition = None
        self.supports_range = False
        self.chunks: list[Chunk] = []

        # 自动选路结果：None=未决定（按 proxy_for 走），True=走代理，False=只走直连
        self.use_proxy = None
        self.route_reason = ""
        self._auto_named = False        # target 是否是按 URL 自动取的名字（可被最终 URL 修正）

        self.state = "queued"
        self.message = ""
        self.downloaded = 0
        self.speed = 0.0
        self.created = time.time()
        self.finished = None

        self.stop = threading.Event()
        self.pause = threading.Event()
        self._runner = False
        self._started = False        # 是否已有线程在跑（start() 幂等用，防止重复起线程）
        self._thread = None          # 当前工作线程（resume 时要等它退出）
        self._resume_pending = False  # 旧线程还在收尾：等它退出后自己重启（不在界面线程 join）
        self._lock = threading.Lock()
        self._conns: list[Conn] = []
        self._bytes = 0
        self._t0 = None

    # ---------- 路径 ----------
    @property
    def part_path(self):
        return self.target + PART_SUFFIX

    @property
    def meta_path(self):
        return self.target + META_SUFFIX

    @property
    def tmpdir(self):
        """FTP / HLS 用：独立的临时目录。"""
        return self.target + ".dltmp"

    def to_dict(self):
        return {
            "id": self.id, "url": self.url, "final_url": self.final_url,
            "target": self.target, "headers": self.headers, "ua": self.ua,
            "max_conn": self.max_conn, "filename_hint": self.filename_hint,
            "size": self.size, "etag": self.etag, "last_modified": self.last_modified,
            "content_type": self.content_type, "supports_range": self.supports_range,
            "state": self.state, "created": self.created, "finished": self.finished,
        }

    @classmethod
    def from_dict(cls, d):
        t = cls(d["url"], d["target"], headers=d.get("headers") or {},
                ua=d.get("ua"), max_conn=d.get("max_conn", 8),
                task_id=d.get("id"), filename_hint=d.get("filename_hint"))
        t.final_url = d.get("final_url")
        t.size = d.get("size", 0)
        t.etag = d.get("etag")
        t.last_modified = d.get("last_modified")
        t.content_type = d.get("content_type")
        t.supports_range = d.get("supports_range", False)
        t.state = d.get("state", "paused")
        if t.state in ("running", "probing", "queued"):
            t.state = "paused"
        t.created = d.get("created", time.time())
        t.finished = d.get("finished")
        t._load_meta()
        return t

    # ---------- 分块状态旁车 ----------
    def _save_meta(self):
        if self.size <= 0:
            return                       # 长度未知（chunked）无法安全续传，不写进度
        try:
            # 原子写：先写临时文件再 replace。进程被强杀时不会留下半截 JSON
            # （半截 meta 会被 _load_meta 的 except 吞掉 → 退化成"找不到进度"→ 从头下）
            tmp = self.meta_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"url": self.url, "size": self.size, "etag": self.etag,
                           "lm": self.last_modified,
                           "chunks": [c.d() for c in self.chunks]}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.meta_path)
        except Exception:
            pass

    def _load_meta(self):
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("url") != self.url:
                return False
            self.size = d.get("size") or 0
            if self.size <= 0:
                return False             # 长度未知的旧进度不可信，重新下载
            self.etag = d.get("etag")
            self.last_modified = d.get("lm")
            self.chunks = [Chunk.load(a) for a in d.get("chunks") or []]
            return bool(self.chunks) and os.path.exists(self.part_path)
        except Exception:
            return False

    def _drop_progress(self):
        self.chunks = []
        self.downloaded = 0
        for p in (self.part_path, self.meta_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    def _preallocate(self, path):
        """按声明大小预分配（稀疏），但对异常尺寸与空间不足提前拒绝。

        NTFS 上 truncate 会真的分配簇（实测 truncate(100GB) 吃掉了 42GB 实盘），
        所以恶意/错误服务器声明一个巨大 Content-Length 就能把用户磁盘写满。
        这里：超硬上限直接失败；超可用空间直接失败；超大文件不预分配（边下边扩）。
        """
        size = self.size or 0
        if size <= 0:
            with open(path, "wb"):
                pass
            return
        if size > MAX_DECLARED_SIZE:
            raise FatalError("服务器声明的文件大小异常（%d 字节），已拒绝下载" % size)
        free = None
        try:
            free = shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".").free
        except Exception:
            pass
        if free is not None and size > free * 0.95:
            raise FatalError("磁盘空间不足：需要 %.1f GB，可用 %.1f GB"
                             % (size / 1073741824.0, free / 1073741824.0))
        with open(path, "wb") as f:
            if size <= PREALLOC_LIMIT and (free is None or size <= free * 0.6):
                f.truncate(size)

    # ---------- 探测 ----------
    def probe(self):
        self.state = "probing"
        last = None
        for attempt in range(3):
            try:
                return self._probe_once()
            except Exception as e:
                if str(e) in ("FTP", "redirect without Location", "bad Content-Range"):
                    raise
                last = e
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
        raise last

    _WEB_EXTS = (".html", ".htm", ".txt", ".xml", ".json", ".md", ".csv", ".m3u8", ".shtml")

    def _looks_like_error_page(self, url):
        """服务器返回的是「小体积 HTML」→ 多半是错误页/登录页/拦截页，不是真文件。

        典型场景：下载链接失效后 302 到某个镜像的 404 页面（200 + text/html + 几 KB），
        旧版会把它当文件存下来并标记「已完成」，用户拿到一个假文件。
        """
        ct = (self.content_type or "").lower()
        if "text/html" not in ct:
            return False
        if self.size <= 0 or self.size > 64 * 1024:
            return False
        ext = os.path.splitext(up.urlsplit(url).path)[1].lower()
        return ext not in self._WEB_EXTS

    def _probe_once(self):
        url = self.url
        for _ in range(8):
            u = up.urlsplit(url)
            if u.scheme == "ftp":
                raise IOError("FTP")
            host = u.hostname
            port = u.port or (443 if u.scheme == "https" else 80)
            path = (u.path or "/") + (("?" + u.query) if u.query else "")
            h = dict(self.headers)
            h.setdefault("User-Agent", self.ua or DEFAULT_UA)
            h["Accept-Encoding"] = "identity"
            h["Range"] = "bytes=0-0"
            attempts = self.proxy_attempts(host)
            conn = resp = None
            for i, use_proxy in enumerate(attempts):
                try:
                    # 探测超时别太长：走代理时若节点不通，20 秒会让人以为程序卡死
                    conn = _make_conn(u.scheme, host, port, 10, allow_proxy=use_proxy)
                    conn.request("GET", path, headers=h)
                    resp = conn.getresponse()
                    if use_proxy and resp.status in (403, 407, 502, 503) and i < len(attempts) - 1:
                        conn.close()
                        conn = resp = None
                        continue
                    break
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = resp = None
                    if i == len(attempts) - 1:
                        raise
            if resp is None:
                raise IOError("连接失败")

            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                conn.close()
                if not loc:
                    raise IOError("redirect without Location")
                url = up.urljoin(url, loc)
                continue

            self.final_url = url
            self.content_type = resp.getheader("Content-Type")
            self.content_disposition = resp.getheader("Content-Disposition")
            self.etag = resp.getheader("ETag")
            self.last_modified = resp.getheader("Last-Modified")

            if resp.status == 206:
                m = re.search(r"/(\d+)", resp.getheader("Content-Range") or "")
                if not m:
                    raise IOError("bad Content-Range")
                self.size = int(m.group(1))
                self.supports_range = True
            elif resp.status == 200:
                cl = resp.getheader("Content-Length")
                te = (resp.getheader("Transfer-Encoding") or "").lower()
                if cl:
                    self.size = int(cl)
                elif "chunked" in te:
                    self.size = 0              # 长度未知，流式单连接下载
                else:
                    conn.close()
                    raise IOError("服务器返回空响应（无 Content-Length），文件可能不存在或被镜像拦截")
                self.supports_range = False
            else:
                conn.close()
                raise IOError(f"HTTP {resp.status} {resp.reason}")

            if self._looks_like_error_page(url):
                conn.close()
                raise IOError(
                    "服务器返回的是网页（%s，%d 字节），不是文件：链接可能已失效、"
                    "需要登录，或该站点在拦截下载"
                    % ((self.content_type or "").split(";")[0], self.size))
            conn.close()
            return
        raise IOError("too many redirects")

    def guess_name(self):
        if self.filename_hint:
            return _clean_name(self.filename_hint)
        # 优先用服务器给的 Content-Disposition 文件名
        cd = self.content_disposition or ""
        if cd:
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
            if m:
                name = _clean_name(up.unquote(m.group(1).strip()))
                if name:
                    return name
        # 其次用「重定向后的最终 URL」——原始 URL 常常是 /download/latest/win32/x64 这种
        u = up.urlsplit(self.final_url or self.url)
        return _clean_name(os.path.basename(up.unquote(u.path)) or "download.bin")

    def _fix_target_name(self):
        """探测拿到最终 URL 后，修正自动取的文件名。

        例：https://lmstudio.ai/download/latest/win32/x64 → 原名 "x64"（无扩展名），
        重定向后是 LM-Studio-0.4.26-4-x64.exe，应改用后者。
        """
        if not self._auto_named or not self.target:
            return
        cur = os.path.basename(self.target)
        better = self.guess_name()
        if not better or better == cur:
            return
        if os.path.splitext(cur)[1] or not os.path.splitext(better)[1]:
            return                       # 当前已有扩展名，或新名字也没扩展名 → 不动
        try:
            from .manager import unique_path
            self.target = unique_path(os.path.join(os.path.dirname(self.target), better))
        except Exception:
            self.target = os.path.join(os.path.dirname(self.target), better)

    def proxy_attempts(self, host):
        """本任务该走哪条路：返回尝试序列（True=走代理）。

        自动选路已决定时按结果走；未决定时退回「系统代理优先、失败退直连」。
        """
        if self.use_proxy is True:
            return (True, False)         # 代理优先，代理挂了再试直连
        if self.use_proxy is False:
            return (False,)              # 只走直连
        return (True, False) if proxy_for(host) else (False,)

    def _auto_route(self, on_change=None):
        """自动模式：实测「直连」与「代理」，选更快的那条。

        直连不通才用代理；直连能通但代理快 30% 以上，也切代理（很多海外站对国内直连限速）。
        """
        try:
            from .proxy import current_mode
            if current_mode() != "auto":
                return
        except Exception:
            return
        try:
            self.state = "probing"
            self.message = "自动选路中…"
            if on_change:
                on_change(self)
        except Exception:
            pass
        try:
            from .route import decide
            use, reason = decide(self.url)
            self.use_proxy = bool(use)
            self.route_reason = reason
            try:
                import diag
                diag.get_logger().info("自动选路 %s → %s（%s）", diag.mask_url(self.url),
                                       "代理" if use else "直连", reason)
            except Exception:
                pass
        except Exception as e:
            self.use_proxy = None
            self.route_reason = f"选路失败，按默认策略：{e}"
        finally:
            if self.message == "自动选路中…":
                self.message = "探测中"

    # ---------- 主流程 ----------
    def run(self, limiter: SpeedLimiter, on_change):
        try:
            scheme = up.urlsplit(self.url).scheme.lower()
            if scheme == "ftp":
                from .ftpdl import ftp_download
                return ftp_download(self, limiter, on_change)
            from .hls import is_hls, hls_download
            if is_hls(self.url):
                return hls_download(self, limiter, on_change)

            # 自动模式：先实测直连与代理，选更快的那条（结果按 host 缓存）
            self._auto_route(on_change)

            resumed = self._load_meta()
            if not resumed:
                self.probe()
                self._fix_target_name()        # 用重定向后的真实文件名（原始 URL 常没有扩展名）
                self._drop_progress()          # 清掉旧进度（会清空 chunks）
                os.makedirs(os.path.dirname(os.path.abspath(self.target)) or ".", exist_ok=True)
                # 预分配目标大小（稀疏），并校验尺寸是否合理
                self._preallocate(self.part_path)
                self._resolve_auto_conn()
                self.chunks = self._initial_chunks()

            if not os.path.basename(self.target):
                self.target = os.path.join(os.path.dirname(self.target), self.guess_name())

            self._resolve_auto_conn()          # 续传路径：确保 max_conn 已确定
            self.state = "running"
            self.message = "下载中"
            self._t0 = time.monotonic()
            self._bytes = 0
            self.downloaded = sum(c.pos - c.start for c in self.chunks)
            self._save_meta()
            on_change(self)

            self._runner = True
            try:
                errs = self._dispatch(limiter, on_change)
            finally:
                self._runner = False

            if all(c.done for c in self.chunks):
                if self.size <= 0 and self.downloaded < MIN_SANE_UNKNOWN:
                    # chunked 无长度却只拿到极少字节：服务器反爬的"假响应"（如 USTC 返回 0~1 字节）
                    raise IOError("镜像站拒绝访问（返回空响应）")
                self._finalize()
            elif self.stop.is_set():
                self.state = "canceled"
                self.message = "已取消"
            elif self.pause.is_set() or "paused" in errs:
                self.state = "paused"
                self.message = "已暂停"
            elif any(e == "no-range" for e in errs):
                self.state = "error"
                self.message = "服务器不支持分段下载"
            else:
                if not self.stop.is_set() and self._try_fallback(on_change):
                    return
                self.state = "error"
                # errs 为空时沿用兜底写入的消息（如"镜像站拒绝访问"），比"下载未完成"更有用
                self.message = self._final_error(
                    errs[0] if errs else (self.message or "下载未完成"))
        except Aborted:
            self.state = "canceled" if self.stop.is_set() else "paused"
            self.message = "已取消" if self.stop.is_set() else "已暂停"
        except FatalError as e:
            self.state = "error"
            self.message = str(e)              # 尺寸/空间问题，兜底也没意义
        except Exception as e:
            if self.stop.is_set():
                self.state = "canceled"
                self.message = "已取消"
            elif self._try_fallback(on_change):
                return
            else:
                self.state = "error"
                self.message = self._final_error(f"{type(e).__name__}: {e}")
        finally:
            self._runner = False
            self.speed = 0.0
            try:
                if self.chunks and not all(c.done for c in self.chunks):
                    self._save_meta()
            except Exception:
                pass
            on_change(self)

    def _try_fallback(self, on_change):
        """原生引擎失败时，尝试 curl_cffi（Chrome 指纹）兜底；没装则跳过。"""
        # FTP 走 ftplib、HLS 是播放列表：都不适合用 HTTP 兜底。
        # 尤其 HLS —— 兜底会把 m3u8 文本当成视频文件存下来并标记"已完成"。
        if up.urlsplit(self.url).scheme.lower() == "ftp":
            return False
        try:
            from .hls import is_hls
            if is_hls(self.url):
                return False
        except Exception:
            pass
        try:
            from .fallback import curl_cffi_download
            import curl_cffi
            try:
                import diag
                diag.get_logger().info("curl_cffi 可用 %s，尝试兜底", getattr(curl_cffi, "__version__", "?"))
            except Exception:
                pass
        except Exception as e:
            try:
                import diag
                diag.get_logger().warning("curl_cffi 不可用，跳过兜底：%s", e)
            except Exception:
                pass
            return False
        try:
            return curl_cffi_download(self, on_change)
        except Exception:
            return False

    def _final_error(self, err):
        low = (err or "").lower()
        # 站点明确拒绝（返回空 body / 极少量数据）：IP 或网络被限流、封禁，与客户端无关
        if "异常小的响应" in err or "拒绝访问" in err:
            return ("镜像站拒绝了本机访问（返回空响应或极少数据）：IP / 网络被限流或封禁，"
                    "请换一个镜像站或换网络（如手机热点）")
        m = re.search(r"HTTP (5\d\d)", err or "")
        if m:
            return (f"服务器错误（HTTP {m.group(1)}）：站点可能临时故障或限制下载，"
                    "稍后重试或换个镜像/线路")
        netish = any(k in low for k in (
            "403", "407", "连接失败", "空响应", "timed out", "timeout", "eof",
            "reset", "ssl", "urlopen", "10060", "10054", "connection", "aborted"))
        if netish:
            return "被墙（代理 / 直连 / 浏览器指纹均无法访问，可能被网络或站点拦截）"
        return err

    def _finalize(self):
        self.message = "正在校验..."
        self.speed = 0.0
        # 完整性校验：不能用「文件大小 == size」判断 —— _preallocate 已经把 part
        # truncate 到 size，大小恒等于 size，零填充的残缺文件也能通过。
        # 正确判据是每个分块的写游标是否真的走过了 end。
        if self.size > 0:
            bad = [c for c in self.chunks if c.end >= 0 and c.pos <= c.end]
            if bad:
                raise IOError("文件不完整（%d 个分块未写完），已保留进度以便续传" % len(bad))
        # 无合并步骤：part 已经是完整文件，直接改名
        if os.path.exists(self.target):
            try:
                os.remove(self.target)
            except Exception:
                pass
        os.replace(self.part_path, self.target)
        try:
            os.remove(self.meta_path)
        except Exception:
            pass
        self.state = "done"
        self.finished = time.time()
        self.message = "已完成"
        self.downloaded = self.size or self.downloaded

    # ---------- 动态分配（work stealing） ----------
    def _claim_next(self):
        """领一块未完成、未被认领的活；优先领剩余最大的（减少长尾）。"""
        with self._lock:
            best = None
            for c in self.chunks:
                if c.done or c.claimed or c.remaining <= 0:
                    continue
                if best is None or c.remaining > best.remaining:
                    best = c
            if best is not None:
                best.claimed = True
            return best

    def _steal_split(self):
        """没整块可领时，从剩余最大的块里切一半出来给空闲连接。

        这是"快的连接帮慢的连接"的关键：慢块被切小后，原来那条连接只需负责前半段，
        后半段交给空闲连接并行下，尾部等待时间大幅缩短。
        """
        if not self.supports_range:
            return None
        with self._lock:
            target = None
            for c in self.chunks:
                if c.done or c.remaining < MIN_CHUNK * 2:
                    continue
                if target is None or c.remaining > target.remaining:
                    target = c
            if target is None:
                return None
            rem = target.remaining
            mid = target.pos + rem // 2
            if mid <= target.pos or mid > target.end:
                return None
            new = Chunk(len(self.chunks), mid, target.end)
            target.end = mid - 1
            new.claimed = True
            self.chunks.append(new)
            return new

    def _release(self, chunk):
        with self._lock:
            chunk.claimed = False

    def _dispatch(self, limiter, on_change):
        errors: list[str] = []
        tries: dict[int, int] = {}     # id(chunk) -> 已失败次数
        fatal = threading.Event()      # 某块重试超限 → 整体停下，避免无限重试

        for attempt in range(3):
            pending = [c for c in self.chunks if not c.done and c.remaining > 0]
            if self.stop.is_set() or self.pause.is_set():
                if self.pause.is_set() and not self.stop.is_set():
                    errors.append("paused")     # 暂停退出：不能让 run() 误判成失败去走兜底
                break
            if not pending:
                break

            self._conns = []
            stop_flag = self.stop
            pause_flag = self.pause

            def worker():
                """一个 worker 反复领活，直到没活可干。"""
                while (not stop_flag.is_set() and not pause_flag.is_set()
                       and not fatal.is_set()):
                    chunk = self._claim_next()
                    if chunk is None:
                        chunk = self._steal_split()
                    if chunk is None:
                        return                    # 没活了，退出
                    conn = Conn(self, chunk)
                    with self._lock:
                        self._conns.append(conn)
                    backoff = 0.0
                    try:
                        conn.run(stop_flag, pause_flag, limiter, self._add)
                        chunk.done = True
                    except Aborted:
                        pass
                    except NoRangeError:
                        chunk.error = "no-range"
                        errors.append("no-range")
                    except Exception as e:
                        chunk.error = f"{type(e).__name__}: {e}"
                        errors.append(chunk.error)
                        # 失败计数 + 指数退避：旧版失败后立刻被重新认领，
                        # 实测对持续 5xx 的服务器 12 秒发了 14989 个请求（1249 req/s）
                        n = tries.get(id(chunk), 0) + 1
                        tries[id(chunk)] = n
                        if n >= CHUNK_MAX_TRIES:
                            fatal.set()
                        else:
                            backoff = min(3.0, 0.3 * (2 ** (n - 1)))
                    finally:
                        self._release(chunk)
                        with self._lock:
                            try:
                                self._conns.remove(conn)
                            except ValueError:
                                pass
                    if backoff:                   # 可中断退避
                        end = time.monotonic() + backoff
                        while (time.monotonic() < end
                               and not stop_flag.is_set() and not pause_flag.is_set()):
                            time.sleep(0.1)

            threads = [threading.Thread(target=worker, daemon=True)
                       for _ in range(max(1, self.max_conn))]
            for t in threads:
                t.start()

            while True:
                if self.stop.is_set() or self.pause.is_set():
                    if self.pause.is_set() and not self.stop.is_set():
                        errors.append("paused")
                    break
                if not any(t.is_alive() for t in threads):
                    break
                self._speed()
                on_change(self)
                time.sleep(GROW_POLL)

            for t in threads:
                t.join(timeout=30)

            self._save_meta()
            if all(c.done for c in self.chunks):
                break
            if fatal.is_set():
                errors.append(f"分块重试超过上限（{CHUNK_MAX_TRIES} 次）")
                break
            if self.stop.is_set() or self.pause.is_set() or "no-range" in errors:
                break
            time.sleep(0.6 * (attempt + 1))

        if ("no-range" in errors and not self.stop.is_set() and not self.pause.is_set()
                and not all(c.done for c in self.chunks)):
            if not self._fallback_single(limiter, on_change):
                errors.append("降级单连接失败")
        return errors

    def _fallback_single(self, limiter, on_change):
        self.supports_range = False
        self._drop_progress()
        os.makedirs(os.path.dirname(os.path.abspath(self.target)) or ".", exist_ok=True)
        self._preallocate(self.part_path)
        self.chunks = [Chunk(0, 0, self.size - 1 if self.size > 0 else -1)]
        self.downloaded = 0
        self._bytes = 0
        c = Conn(self, self.chunks[0])
        self._conns = [c]
        try:
            c.run(self.stop, self.pause, limiter, self._add)
        except Exception as e:
            self.chunks[0].error = str(e)
        self._speed()
        on_change(self)
        return self.chunks[0].done

    def _resolve_auto_conn(self):
        """自动连接数：至少 8 条（IDM 默认也是 8），大文件最多 16 条。"""
        if self.max_conn is None:
            n = (self.size // (8 * MIN_CHUNK)) if self.size else 0
            self.max_conn = min(16, max(8, n))

    def _initial_chunks(self):
        """按文件大小切块。

        块数取连接数的若干倍（而不是刚好等于连接数）—— 这样谁先下完谁去领下一块，
        天然负载均衡；只切 8 块的话，某块遇到慢链路就会拖住整个任务。
        """
        if self.size <= 0:
            return [Chunk(0, 0, -1)]          # end=-1：长度未知，流式读到 EOF
        if not self.supports_range:
            return [Chunk(0, 0, self.size - 1)]
        conns = max(1, self.max_conn or 8)
        want = min(conns * 4, self.size // MIN_CHUNK, 64)
        n = max(1, int(want))
        if n <= 1:
            return [Chunk(0, 0, self.size - 1)]
        step = self.size // n
        out = []
        for i in range(n):
            a = i * step
            b = (self.size - 1) if i == n - 1 else (a + step - 1)
            out.append(Chunk(i, a, b))
        return out

    def _add(self, n):
        with self._lock:
            self._bytes += n
            self.downloaded += n

    def _speed(self):
        el = max(0.001, time.monotonic() - (self._t0 or time.monotonic()))
        self.speed = self._bytes / el

    # ---------- 控制 ----------
    def pause_task(self):
        self.pause.set()
        if self.state in ("running", "probing"):
            self.state = "paused"
            self.message = "已暂停"

    def resume_task(self):
        self.pause.clear()
        if self.state == "paused":
            self.state = "running"
            self.message = "下载中"

    def cancel(self):
        self.stop.set()
        self.pause.clear()

    def snapshot(self):
        total = self.size or 0
        done = sum((c.size if c.done else max(0, c.pos - c.start)) for c in self.chunks)
        if total <= 0:
            total = sum(c.size for c in self.chunks)
        # 兜底下载（curl_cffi）不走 chunks，只累加 self.downloaded，取两者较大值
        done = max(done, self.downloaded or 0)
        if self.state == "done" and total > 0:
            done = total          # 已完成任务重启后分块旁车可能已清，按 100% 显示
        known = 0 < total < (1 << 50)
        pct = round(min(100.0, done / total * 100.0), 2) if known else None
        return {
            "id": self.id, "url": self.url, "target": self.target,
            "name": os.path.basename(self.target or ""),
            "state": self.state, "message": self.message,
            "size": total if known else 0, "downloaded": done, "percent": pct,
            "speed": round(self.speed, 1),
            "connections": len(self._conns),      # 活跃连接数（worker pool）
            "max_conn": self.max_conn or 0,
            "content_type": self.content_type,
            "supports_range": self.supports_range,
            "created": self.created, "finished": self.finished,
        }
