"""本地 HTTP API + 内置说明页。只监听 127.0.0.1。

安全模型（上线前审计加固）：
- 只绑定回环地址；Host 头必须是 127.0.0.1 / localhost / ::1（防 DNS rebinding）
- 带 Origin 的请求必须是浏览器扩展来源（chrome-extension:// 等），
  任意网页（https://evil.com）发起的跨站请求一律 403
- 不再返回 `Access-Control-Allow-Origin: *`
- 请求体大小上限，防止超大 Content-Length 打爆内存
- 落盘路径必须位于下载目录内（由 Manager 强制，realpath 解析链接/联接后比较）
- `/api/sniff` 禁止访问回环/内网地址，避免被当成 SSRF 跳板
"""
from __future__ import annotations

import os
import re
import json
import ssl
import ipaddress
import socket
import http.client
import urllib.parse as up
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine.core import DEFAULT_UA
from engine.manager import Manager

MAX_BODY = 256 * 1024            # 请求体上限（任务/设置都远小于此）
_EXT_ORIGIN_PREFIX = ("chrome-extension://", "moz-extension://", "safari-web-extension://")
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _inside(root: str, path: str) -> bool:
    """path 是否在 root 目录内（realpath 解析链接/联接后比较，防 ../ 与 junction 逃逸）。"""
    try:
        r = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        return os.path.commonpath([r, p]) == r
    except Exception:
        return False


def _origin_allowed(origin: str) -> bool:
    """空 Origin（curl / 本地工具）放行；浏览器请求必须是扩展来源。"""
    o = (origin or "").strip().lower()
    if not o:
        return True
    return o.startswith(_EXT_ORIGIN_PREFIX)


def _json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    _cors(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _cors(handler):
    """只对扩展来源回显 ACAO；对网页来源不下发任何 CORS 头。"""
    o = (handler.headers.get("Origin") or "").strip()
    if o and _origin_allowed(o):
        handler.send_header("Access-Control-Allow-Origin", o)
        handler.send_header("Vary", "Origin")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def make_server(mgr: Manager, port: int = 8765):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MultiDL/1.0"
        sys_version = ""

        def log_message(self, fmt, *a):
            pass

        # ---------- 准入 ----------
        def _host_ok(self):
            raw = (self.headers.get("Host") or "").strip()
            if not raw:
                return True                       # HTTP/1.0 客户端可能不带 Host
            try:
                h = (up.urlsplit("//" + raw).hostname or "").lower()
            except Exception:
                return False
            return h in _LOCAL_HOSTS

        def _gate(self):
            """Host / Origin 双重校验。返回 False 表示已回 403，调用方直接返回。"""
            if not self._host_ok():
                _json(self, {"ok": False, "error": "host not allowed"}, 403)
                return False
            o = (self.headers.get("Origin") or "").strip()
            if not _origin_allowed(o):
                _json(self, {"ok": False,
                             "error": "cross-site request blocked"}, 403)
                return False
            return True

        # ---------- 工具 ----------
        def _body(self):
            self._body_error = None
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                self._body_error = "Content-Length 非法"
                return {}
            if n > MAX_BODY:
                # 不读 body，直接断开，避免被塞满内存
                self.close_connection = True
                self._body_error = f"请求体过大（上限 {MAX_BODY} 字节）"
                return {}
            # 强制 JSON：跨站「简单请求」（text/plain / 表单）不触发预检，
            # 若这里不校验 Content-Type，Origin 校验就能被沙箱 iframe 绕过。
            ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct != "application/json":
                self.close_connection = True
                self._body_error = "Content-Type 必须是 application/json"
                return {}
            if n <= 0:
                return {}
            try:
                raw = self.rfile.read(n)
            except Exception:
                self._body_error = "读取请求体失败"
                return {}
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                return {}
            return obj if isinstance(obj, dict) else {}

        def _static(self, path):
            # 桌面版不再提供网页界面；只有根路径给一个说明页，其余路径正常 404
            if path not in ("/", "/index.html"):
                return _json(self, {"ok": False, "error": "not found"}, 404)
            html = ("<!doctype html><meta charset=utf-8>"
                    "<title>MultiDL</title>"
                    "<body style='font:14px/1.6 Segoe UI,Microsoft YaHei;padding:28px'>"
                    "<h3>MultiDL 本地服务运行中</h3>"
                    "<p>下载界面在桌面客户端里。此端口仅用于浏览器扩展投递链接。</p>"
                    "<p>接口：<code>POST /api/tasks</code>、<code>GET /api/tasks</code></p>"
                    "</body>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(html)

        # ---------- 路由 ----------
        def do_OPTIONS(self):
            if not self._host_ok() or not _origin_allowed(self.headers.get("Origin") or ""):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(204)
            _cors(self)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._gate():
                return
            u = up.urlsplit(self.path)
            p = u.path

            if p == "/api/tasks":
                return _json(self, {"tasks": mgr.snapshots(), "settings": mgr.settings()})
            if p == "/api/settings":
                return _json(self, mgr.settings())
            return self._static(p)

        def do_POST(self):
            if not self._gate():
                return
            u = up.urlsplit(self.path)
            p = u.path
            d = self._body()
            if getattr(self, "_body_error", None):
                code = 415 if "Content-Type" in self._body_error else 413
                return _json(self, {"ok": False, "error": self._body_error}, code)

            if p == "/api/tasks":
                try:
                    snap = mgr.add(
                        d.get("url", ""),
                        target=d.get("target") or None,
                        referer=d.get("referer") or None,
                        cookie=d.get("cookie") or None,
                        ua=d.get("ua") or None,
                        headers=d.get("headers") or None,
                        max_conn=d.get("max_conn") or None,
                        filename_hint=d.get("filename") or None,
                        autostart=bool(d.get("autostart", True)),
                    )
                    return _json(self, {"ok": True, "task": snap})
                except Exception as e:
                    return _json(self, {"ok": False, "error": str(e)}, 400)

            if p == "/api/action":
                tid = str(d.get("id") or "")
                act = d.get("action")
                fn = {"pause": mgr.pause, "resume": mgr.resume,
                      "cancel": mgr.cancel}.get(act)
                if fn:
                    return _json(self, {"ok": bool(fn(tid))})
                if act == "remove":
                    return _json(self, {"ok": mgr.remove(tid, bool(d.get("delete_file")))})
                return _json(self, {"ok": False, "error": "unknown action"}, 400)

            if p == "/api/settings":
                try:
                    s = mgr.set_limits(
                        max_parallel=d.get("max_parallel"),
                        speed_limit=d.get("speed_limit"),
                        default_conn=d.get("default_conn"),
                    )
                except (TypeError, ValueError) as e:
                    return _json(self, {"ok": False, "error": f"参数非法: {e}"}, 400)
                return _json(self, {"ok": True, "settings": s})

            if p == "/api/sniff":
                return _json(self, sniff(d.get("url", ""), d.get("referer") or None))

            return _json(self, {"ok": False, "error": "not found"}, 404)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


MEDIA_RE = re.compile(
    r"""https?://[^\s"'<>\\)]+?\.(?:mp4|m4v|webm|mkv|mov|flv|ts|mp3|m4a|aac|ogg|wav|m3u8|zip|rar|7z|iso|exe|msi|pdf|apk)(?:\?[^\s"'<>\\)]*)? """,
    re.I | re.X,
)
SRC_RE = re.compile(r"""<(?:video|source|audio)[^>]+src=["']([^"']+)["']""", re.I)
A_RE = re.compile(r"""<a[^>]+href=["']([^"']+)["']""", re.I)


def _resolve_public(host: str):
    """解析一次并返回公网 IP；任一结果为私网/回环/保留或解析失败 → None。

    只解析一次、并把解析结果交给连接层使用，避免「校验时解析成公网、
    连接时又解析成 127.0.0.1」的 DNS rebinding 时间差。
    """
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return None
    first = None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return None
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return None
        if first is None:
            first = str(ip)
    return first


class _PinnedHTTPS(http.client.HTTPSConnection):
    """用已解析的 IP 建连，SNI 与证书校验仍按原域名。"""

    def __init__(self, host, ip, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pin_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        ctx = getattr(self, "_context", None) or ssl.create_default_context()
        self.sock = ctx.wrap_socket(sock, server_hostname=self.host)


def sniff(url: str, referer: str | None = None):
    """抓取页面里的媒体/文件直链（禁止访问本机与内网地址）。"""
    if not url:
        return {"ok": False, "error": "url 为空"}
    try:
        u = up.urlsplit(url)
        if u.scheme not in ("http", "https"):
            return {"ok": False, "error": "只支持 http/https"}
        host = u.hostname
        ip = _resolve_public(host)
        if not ip:
            return {"ok": False, "error": "拒绝访问本机或内网地址"}
        port = u.port or (443 if u.scheme == "https" else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        host_hdr = host if port in (80, 443) else f"{host}:{port}"
        if u.scheme == "https":
            c = _PinnedHTTPS(host, ip, port, 20, ssl.create_default_context())
        else:
            c = http.client.HTTPConnection(ip, port, timeout=20)
        h = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Host": host_hdr}
        if referer:
            h["Referer"] = referer
        c.request("GET", path, headers=h)
        r = c.getresponse()
        raw = r.read(4 << 20)
        c.close()
        text = raw.decode("utf-8", "replace")

        found = []
        for m in MEDIA_RE.finditer(text):
            found.append(m.group(0).strip())
        for m in SRC_RE.finditer(text):
            found.append(up.urljoin(url, m.group(1)))
        for m in A_RE.finditer(text):
            href = m.group(1)
            if re.search(r"\.(mp4|m4v|webm|mkv|mov|flv|mp3|m4a|aac|ogg|wav|m3u8|zip|rar|7z|iso|exe|msi|pdf|apk)(\?|$)",
                         href, re.I):
                found.append(up.urljoin(url, href))

        seen, out = set(), []
        for f in found:
            f = f.replace("&amp;", "&")
            if f.startswith("//"):
                f = "https:" + f
            if f not in seen:
                seen.add(f)
                out.append({"url": f, "kind": "hls" if ".m3u8" in f.lower() else "file"})
        return {"ok": True, "count": len(out), "items": out[:200], "page": url}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
