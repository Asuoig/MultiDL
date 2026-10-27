"""HLS (m3u8) 下载：拉取媒体播放列表，逐段下载后拼接。"""
from __future__ import annotations

import os
import re
import ssl
import time
import shutil
import threading
import http.client
import urllib.parse as up

from .core import DEFAULT_UA, _make_conn


def is_hls(url: str) -> bool:
    p = up.urlsplit(url).path.lower()
    return p.endswith(".m3u8") or "m3u8" in p


def _fetch(url, headers, timeout=25, limit=1 << 24):
    u = up.urlsplit(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    # 走统一的连接工厂：这样 HLS 也会遵守代理配置与自动选路
    c = _make_conn(u.scheme, host, port, timeout)
    h = dict(headers)
    h.setdefault("User-Agent", DEFAULT_UA)
    c.request("GET", path, headers=h)
    r = c.getresponse()
    if r.status >= 400:
        raise IOError(f"HTTP {r.status} for {url}")
    data = r.read(limit)
    c.close()
    return data


def parse_playlist(text: str, base: str):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    segs, key, cur_key = [], None, None
    variant = None
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF"):
            for j in range(i + 1, len(lines)):
                if not lines[j].startswith("#"):
                    variant = up.urljoin(base, lines[j])
                    break
            if variant:
                return {"variant": variant, "segments": [], "key": None}
        elif l.startswith("#EXT-X-KEY"):
            m = re.search(r'URI="([^"]+)"', l)
            if m:
                cur_key = up.urljoin(base, m.group(1))
        elif not l.startswith("#"):
            segs.append({"url": up.urljoin(base, l), "key": cur_key})
    return {"variant": None, "segments": segs, "key": key}


def hls_download(sess, limiter, on_change):
    headers = dict(sess.headers)
    headers.setdefault("User-Agent", sess.ua or DEFAULT_UA)

    text = _fetch(sess.url, headers).decode("utf-8", "replace")
    pl = parse_playlist(text, sess.url)
    depth = 0
    while pl["variant"] and depth < 3:
        text = _fetch(pl["variant"], headers).decode("utf-8", "replace")
        pl = parse_playlist(text, pl["variant"])
        depth += 1
    segs = pl["segments"]
    if not segs:
        raise IOError("播放列表中没有可下载的分段")

    for s in segs:
        if s["key"]:
            raise IOError("该 HLS 流使用加密分段（EXT-X-KEY），本工具不处理")

    os.makedirs(sess.tmpdir, exist_ok=True)
    sess.size = len(segs)
    sess.state = "running"
    sess.message = f"HLS 分段下载中 (0/{len(segs)})"
    sess.supports_range = False
    on_change(sess)

    parts = []
    t0 = time.monotonic()
    for i, s in enumerate(segs):
        # 暂停要能真正停住：旧写法内层 break 只跳出 while，外层接着下分段
        while sess.pause.is_set() and not sess.stop.is_set():
            time.sleep(0.2)
        if sess.stop.is_set():
            break
        p = os.path.join(sess.tmpdir, f"seg_{i:05d}.ts")
        parts.append(p)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            sess.downloaded = i + 1
            continue
        data = _fetch(s["url"], headers, timeout=40)
        limiter.consume(len(data), stop=sess.stop.is_set)
        with open(p, "wb") as f:
            f.write(data)
        sess.downloaded = i + 1
        el = max(0.001, time.monotonic() - t0)
        sess.speed = sum(os.path.getsize(x) for x in parts if os.path.exists(x)) / el
        sess.message = f"HLS 分段下载中 ({i + 1}/{len(segs)})"
        on_change(sess)

    if sess.stop.is_set():
        sess.state = "canceled"
        sess.message = "已取消"
        on_change(sess)
        return

    sess.message = "正在拼接分段..."
    on_change(sess)
    os.makedirs(os.path.dirname(os.path.abspath(sess.target)) or ".", exist_ok=True)
    with open(sess.target, "wb") as out:
        for p in parts:
            if os.path.exists(p):
                with open(p, "rb") as f:
                    while True:
                        b = f.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
    sess.size = os.path.getsize(sess.target)
    sess.downloaded = sess.size
    sess.state = "done"
    sess.message = "已完成"
    sess.finished = time.time()
    sess.speed = 0.0
    on_change(sess)
    shutil.rmtree(sess.tmpdir, ignore_errors=True)     # 分段已拼完，清掉临时目录
