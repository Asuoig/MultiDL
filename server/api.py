"""本地 HTTP API + 内置 Web UI。只监听 127.0.0.1。"""
from __future__ import annotations

import os
import re
import json
import time
import ssl
import threading
import http.client
import urllib.parse as up
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine.core import DEFAULT_UA
from engine.manager import Manager




def _json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _cors(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _cors(handler):
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def make_server(mgr: Manager, port: int = 8765):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MultiDL/1.0"

        def log_message(self, fmt, *a):
            pass

        # ---------- 工具 ----------
        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def _static(self, path):
            # 桌面版不再提供网页界面；保留一个说明页，方便排查端口是否活着
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
            self.end_headers()
            self.wfile.write(html)

        # ---------- 路由 ----------
        def do_OPTIONS(self):
            self.send_response(204)
            _cors(self)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            u = up.urlsplit(self.path)
            p = u.path
            q = up.parse_qs(u.query)

            if p == "/api/tasks":
                return _json(self, {"tasks": mgr.snapshots(), "settings": mgr.settings()})
            if p == "/api/settings":
                return _json(self, mgr.settings())
            if p == "/api/open":
                target = (q.get("path") or [""])[0]
                return self._open(target)
            return self._static(p)

        def do_POST(self):
            u = up.urlsplit(self.path)
            p = u.path
            d = self._body()

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
                s = mgr.set_limits(
                    max_parallel=d.get("max_parallel"),
                    speed_limit=d.get("speed_limit"),
                    default_conn=d.get("default_conn"),
                )
                return _json(self, {"ok": True, "settings": s})

            if p == "/api/sniff":
                return _json(self, sniff(d.get("url", ""), d.get("referer") or None))

            return _json(self, {"ok": False, "error": "not found"}, 404)

        # ---------- 打开所在文件夹 ----------
        def _open(self, target):
            root = os.path.abspath(mgr.download_dir)
            ap = os.path.abspath(target or root)
            if not (ap == root or ap.startswith(root + os.sep) or ap.startswith(mgr.data_dir)):
                return _json(self, {"ok": False, "error": "路径不在下载目录内"}, 403)
            folder = ap if os.path.isdir(ap) else os.path.dirname(ap)
            try:
                if os.path.isdir(folder):
                    os.startfile(folder)          # noqa: 仅 Windows
                    return _json(self, {"ok": True})
            except Exception as e:
                return _json(self, {"ok": False, "error": str(e)}, 500)
            return _json(self, {"ok": False, "error": "目录不存在"}, 404)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


MEDIA_RE = re.compile(
    r"""https?://[^\s"'<>\\)]+?\.(?:mp4|m4v|webm|mkv|mov|flv|ts|mp3|m4a|aac|ogg|wav|m3u8|zip|rar|7z|iso|exe|msi|pdf|apk)(?:\?[^\s"'<>\\)]*)? """,
    re.I | re.X,
)
SRC_RE = re.compile(r"""<(?:video|source|audio)[^>]+src=["']([^"']+)["']""", re.I)
A_RE = re.compile(r"""<a[^>]+href=["']([^"']+)["']""", re.I)


def sniff(url: str, referer: str | None = None):
    """抓取页面里的媒体/文件直链。"""
    if not url:
        return {"ok": False, "error": "url 为空"}
    try:
        u = up.urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        if u.scheme == "https":
            c = http.client.HTTPSConnection(host, port, timeout=20,
                                            context=ssl.create_default_context())
        else:
            c = http.client.HTTPConnection(host, port, timeout=20)
        h = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
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
