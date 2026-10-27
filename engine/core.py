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


class NoRangeError(Exception):
    pass


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

        # 先按代理连；代理失败则退回直连（保证不会因为代理配错而让下载失败）
        attempts = (True, False) if proxy_for(host) else (False,)
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
        self.id = task_id or ("%d%03d" % (int(time.time() * 1000) % 10 ** 9,
                                          abs(hash(url)) % 1000))
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
        self.supports_range = False
        self.chunks: list[Chunk] = []

        self.state = "queued"
        self.message = ""
        self.downloaded = 0
        self.speed = 0.0
        self.created = time.time()
        self.finished = None

        self.stop = threading.Event()
        self.pause = threading.Event()
        self._runner = False
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
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump({"url": self.url, "size": self.size, "etag": self.etag,
                           "lm": self.last_modified,
                           "chunks": [c.d() for c in self.chunks]}, f)
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
            attempts = (True, False) if proxy_for(host) else (False,)
            conn = resp = None
            for i, use_proxy in enumerate(attempts):
                try:
                    conn = _make_conn(u.scheme, host, port, 20, allow_proxy=use_proxy)
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
            conn.close()
            return
        raise IOError("too many redirects")

    def guess_name(self):
        if self.filename_hint:
            return self.filename_hint
        u = up.urlsplit(self.final_url or self.url)
        name = os.path.basename(up.unquote(u.path)) or "download.bin"
        return re.sub(r'[\\/:*?"<>|]', "_", name)[:150]

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

            resumed = self._load_meta()
            if not resumed:
                self.probe()
                self._drop_progress()          # 清掉旧进度（会清空 chunks）
                os.makedirs(os.path.dirname(os.path.abspath(self.target)) or ".", exist_ok=True)
                # 预分配目标大小（稀疏），后续各连接直接定位写
                with open(self.part_path, "wb") as f:
                    if self.size:
                        f.truncate(self.size)
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
            elif self.pause.is_set():
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
        netish = any(k in low for k in (
            "403", "407", "连接失败", "空响应", "timed out", "timeout", "eof",
            "reset", "ssl", "urlopen", "10060", "10054", "connection", "aborted"))
        if netish:
            return "被墙（代理 / 直连 / 浏览器指纹均无法访问，可能被网络或站点拦截）"
        return err

    def _finalize(self):
        self.message = "正在校验..."
        self.speed = 0.0
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

        for attempt in range(3):
            pending = [c for c in self.chunks if not c.done and c.remaining > 0]
            if not pending or self.stop.is_set() or self.pause.is_set():
                break

            self._conns = []
            stop_flag = self.stop
            pause_flag = self.pause

            def worker():
                """一个 worker 反复领活，直到没活可干。"""
                while not stop_flag.is_set() and not pause_flag.is_set():
                    chunk = self._claim_next()
                    if chunk is None:
                        chunk = self._steal_split()
                    if chunk is None:
                        return                    # 没活了，退出
                    conn = Conn(self, chunk)
                    with self._lock:
                        self._conns.append(conn)
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
                    finally:
                        self._release(chunk)
                        with self._lock:
                            try:
                                self._conns.remove(conn)
                            except ValueError:
                                pass

            threads = [threading.Thread(target=worker, daemon=True)
                       for _ in range(max(1, self.max_conn))]
            for t in threads:
                t.start()

            while True:
                if self.stop.is_set() or self.pause.is_set():
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
        with open(self.part_path, "wb") as f:
            if self.size:
                f.truncate(self.size)
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
