# -*- coding: utf-8 -*-
"""发布前门禁：本地 API 安全断言 + 下载主链路回归。

用法（项目根目录）：
    .venv/Scripts/python.exe test/t_release.py

全绿才允许发版；任何一项 FAIL 会以退出码 1 结束。
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import socket
import hashlib
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.manager import Manager          # noqa: E402
from server.api import make_server          # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(name, cond, detail=""):
    global PASSES
    if cond:
        PASSES += 1
        print(f"  [PASS] {name}")
    else:
        FAILS.append(name)
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 本地 API
work = tempfile.mkdtemp(prefix="multidl_rel_")
dl = os.path.join(work, "dl")
os.makedirs(dl, exist_ok=True)
mgr = Manager(os.path.join(work, "data"), dl, max_parallel=1)
srv = make_server(mgr, 0)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
EXT = "chrome-extension://abcdefghijklmnop"


def req(path, method="GET", body=None, headers=None, ctype="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None and ctype:
        r.add_header("Content-Type", ctype)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=6) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 413/415 时服务端会主动断开（不读 body），读响应体可能抛 ConnectionReset
        try:
            payload = e.read().decode("utf-8", "replace")
        except Exception:
            payload = ""
        return e.code, dict(e.headers), payload
    except Exception as e:
        return None, {}, f"{type(e).__name__}: {e}"


print("\n=== 1. 本地 API 准入 ===")
c, h, _ = req("/api/tasks", headers={"Origin": "https://evil.example"})
check("跨站读取被拒 (403)", c == 403, f"HTTP {c}")
c, h, _ = req("/api/tasks", method="OPTIONS",
              headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
check("预检不下发 ACAO", h.get("Access-Control-Allow-Origin") is None, f"ACAO={h.get('Access-Control-Allow-Origin')!r}")
c, _, _ = req("/api/tasks", headers={"Host": "evil.example:8765"})
check("伪造 Host 被拒 (403)", c == 403, f"HTTP {c}")
c, h, _ = req("/api/tasks", headers={"Origin": EXT})
check("扩展来源可读 (200)", c == 200, f"HTTP {c}")

# 扩展标记头通道：兜底「某天 Chrome 把扩展 Origin 改成 null」
c, _, _ = req("/api/tasks", "POST",
              {"url": "http://127.0.0.1:1/probe.bin", "autostart": False},
              {"Origin": "null", "X-MultiDL-Ext": "1"})
check("Origin:null + 扩展标记头 → 放行（兼容通道）", c == 200, f"HTTP {c}")
c, _, _ = req("/api/tasks", method="OPTIONS",
              headers={"Origin": "null", "X-MultiDL-Ext": "1",
                       "Access-Control-Request-Method": "POST"})
check("预检不认扩展标记头（网页无法借预检过关）", c == 403, f"HTTP {c}")

print("\n=== 2. 请求体约束 ===")
c, _, b = req("/api/tasks", "POST", {"url": "http://127.0.0.1:1/a.bin"}, ctype="text/plain")
check("text/plain 被拒 (415)", c == 415, f"HTTP {c} {b[:60]}")
c, _, b = req("/api/tasks", "POST", {"url": "x" * 400000})
check("超大 body 被拒 (413)", c == 413, f"HTTP {c} {b[:60]}")

print("\n=== 3. 落盘路径与协议 ===")
outside = os.path.join(os.environ["USERPROFILE"], "multidl_rel_outside.bin")
c, _, b = req("/api/tasks", "POST", {"url": "http://127.0.0.1:1/a.bin", "target": outside})
check("目录外 target 被拒 (400)", c == 400 and not os.path.exists(outside), f"HTTP {c} {b[:70]}")
for u in ("file:///C:/Windows/win.ini", "gopher://127.0.0.1:6379/_x", "data:text/plain,hi"):
    c, _, b = req("/api/tasks", "POST", {"url": u})
    check(f"协议 {u.split(':')[0]}:// 被拒 (400)", c == 400, f"HTTP {c} {b[:60]}")

print("\n=== 4. SSRF 防护 ===")
c, _, b = req("/api/sniff", "POST", {"url": f"{BASE}/api/settings"})
ok = (json.loads(b).get("ok") is False) if c == 200 else False
check("sniff 拒绝本机/内网", ok, f"HTTP {c} {b[:80]}")

print("\n=== 5. 任务 ID 唯一性 ===")
ids = [mgr.add("http://127.0.0.1:1/same.bin", autostart=False)["id"] for _ in range(8)]
check("同 URL 连续 8 次 → 8 个独立 ID", len(set(ids)) == 8, f"{len(set(ids))} 个")

# ---------------------------------------------------------------- 下载链路
DATA = os.urandom(8 * 1024 * 1024)
SHA = hashlib.sha256(DATA).hexdigest()


class RangeSrv(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        if not m:
            self.send_response(200)
            self.send_header("Content-Length", str(len(DATA)))
            self.end_headers()
            self.wfile.write(DATA)
            return
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else len(DATA) - 1
        b = min(b, len(DATA) - 1)
        body = DATA[a:b + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {a}-{b}/{len(DATA)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


rs = ThreadingHTTPServer(("127.0.0.1", 0), RangeSrv)
rs.daemon_threads = True
threading.Thread(target=rs.serve_forever, daemon=True).start()


def wait(tid, secs=40):
    t0 = time.time()
    while time.time() - t0 < secs:
        t = mgr.get(tid)
        if t.state in ("done", "error", "canceled"):
            return t
        time.sleep(0.2)
    return mgr.get(tid)


print("\n=== 6. 下载主链路 ===")
s = mgr.add(f"http://127.0.0.1:{rs.server_address[1]}/big.bin", max_conn=8)
t = wait(s["id"])
same = t.state == "done" and hashlib.sha256(open(t.target, "rb").read()).hexdigest() == SHA
check("分段下载 + SHA256 一致", same, f"state={t.state} msg={t.message[:40]}")

# 重复 start 不应起第二套线程
check("start() 幂等（已完成任务再 start 返回 False）", mgr.start(s["id"]) is False)

# 持续 5xx 必须有重试上限
CNT = {"n": 0}


class BadSrv(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        CNT["n"] += 1
        if (self.headers.get("Range") or "") == "bytes=0-0":
            self.send_response(206)
            self.send_header("Content-Range", "bytes 0-0/8000000")
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"A")
            return
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()


bs = ThreadingHTTPServer(("127.0.0.1", 0), BadSrv)
bs.daemon_threads = True
threading.Thread(target=bs.serve_forever, daemon=True).start()
s2 = mgr.add(f"http://127.0.0.1:{bs.server_address[1]}/f.bin", max_conn=4)
t2 = wait(s2["id"], 25)
check("持续 5xx 有重试上限（<200 请求且以 error 收尾）",
      t2.state == "error" and CNT["n"] < 200, f"state={t2.state} 请求数={CNT['n']}")

print("\n=== 7. 浏览器指纹模式 ===")
import subprocess                                          # noqa: E402
import engine.browser as browsemod                        # noqa: E402
from engine.core import Task as _Task                      # noqa: E402

# 7.0 打包必备件：内置 chromedriver 必须存在且能跑（否则 exe 里的浏览器模式会退化成下载驱动）
_drv = browsemod.bundled_driver()
_drv_ver = ""
if _drv:
    try:
        _drv_ver = subprocess.run([_drv, "--version"], capture_output=True,
                                  text=True, timeout=20).stdout.strip()
    except Exception as e:                                 # noqa: BLE001
        _drv_ver = f"err:{e}"
check("内置 chromedriver 可用", bool(_drv_ver.startswith("ChromeDriver")),
      f"{_drv} → {_drv_ver[:40]}")

# 7.0b 本机代理环境变量不能把「连本机 chromedriver」的请求也代理掉（否则全部命令 unhandled request）
browsemod._bypass_proxy_for_localhost()
_np = (os.environ.get("NO_PROXY", "") + "," + os.environ.get("no_proxy", "")).lower()
check("NO_PROXY 已放行本机回环", "127.0.0.1" in _np and "localhost" in _np, _np[:60])


class _NoDepsDL:
    """模拟「依赖未安装」的后端：必须优雅降级，不能中断下载。"""
    def available(self):
        return False

    def missing_reason(self):
        return "stub：依赖未安装"


class _FakeDL:
    """模拟已就绪的后端：验证直链 / Cookie / UA 是否写回任务。"""
    def available(self):
        return True

    def start(self):
        return True, "stub"

    def resolve_url(self, url):
        return url.replace("/page", "/real.bin")

    def cookies_header(self):
        return "sid=abc; tok=def"

    def user_agent(self):
        return "UA-FROM-BROWSER"


_orig_dl = browsemod._downloader
try:
    browsemod._downloader = _NoDepsDL()
    mgr.set_proxy_mode("browser")
    check("browser 模式已通知引擎", browsemod.is_enabled())
    s3 = mgr.add(f"http://127.0.0.1:{rs.server_address[1]}/big.bin", max_conn=8)
    t3 = wait(s3["id"])
    ok3 = (t3.state == "done"
           and hashlib.sha256(open(t3.target, "rb").read()).hexdigest() == SHA)
    check("缺依赖时降级为常规下载（不失败）", ok3, f"state={t3.state} msg={t3.message[:40]}")

    browsemod._downloader = _FakeDL()
    tt = _Task("http://example.com/page", "x.bin")
    p_ok, p_why = browsemod.prepare_task(tt)
    check("prepare_task 写回直链 / Cookie / UA",
          p_ok and tt.url.endswith("/real.bin")
          and tt.headers.get("Cookie") == "sid=abc; tok=def"
          and tt.ua == "UA-FROM-BROWSER",
          f"ok={p_ok} url={tt.url} why={p_why[:40]}")
finally:
    browsemod._downloader = _orig_dl
    mgr.set_proxy_mode("auto")
    check("切回自动模式后引擎不再走浏览器", not browsemod.is_enabled())

srv.shutdown()
rs.shutdown()
bs.shutdown()

print("\n" + "=" * 56)
print(f"通过 {PASSES} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print("  ✗", f)
    print("结论：不可发布")
    sys.exit(1)
print("结论：可以发布")
