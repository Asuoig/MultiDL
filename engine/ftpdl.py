"""FTP 下载（单流 + REST 断点续传）。"""
from __future__ import annotations

import os
import time
import shutil
import ftplib
import urllib.parse as up


def ftp_download(sess, limiter, on_change):
    u = up.urlsplit(sess.url)
    host = u.hostname
    port = u.port or 21
    path = up.unquote(u.path or "/")
    user = up.unquote(u.username) if u.username else "anonymous"
    pwd = up.unquote(u.password) if u.password else "anonymous@"

    os.makedirs(sess.tmpdir, exist_ok=True)
    part = os.path.join(sess.tmpdir, "0000.part")
    offset = os.path.getsize(part) if os.path.exists(part) else 0

    sess.state = "running"
    sess.message = "FTP 下载中"
    on_change(sess)

    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=25)
    ftp.login(user, pwd)
    try:
        ftp.voidcmd("TYPE I")
        try:
            total = ftp.size(path) or 0
        except Exception:
            total = 0
        sess.size = total
        sess.supports_range = True
        f = open(part, "ab")
        t0 = time.monotonic()
        got = offset

        def cb(block):
            nonlocal got
            if sess.stop.is_set():
                raise RuntimeError("__abort__")
            while sess.pause.is_set():
                if sess.stop.is_set():
                    raise RuntimeError("__abort__")
                time.sleep(0.2)
            limiter.consume(len(block), stop=sess.stop.is_set)
            f.write(block)
            got += len(block)
            sess.downloaded = got
            el = max(0.001, time.monotonic() - t0)
            sess.speed = (got - offset) / el
            on_change(sess)

        try:
            if offset:
                ftp.retrbinary(f"RETR {path}", cb, blocksize=1 << 16, rest=offset)
            else:
                ftp.retrbinary(f"RETR {path}", cb, blocksize=1 << 16)
        finally:
            f.close()
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    if sess.stop.is_set():
        sess.state = "canceled"
        sess.message = "已取消"
        on_change(sess)
        return

    os.makedirs(os.path.dirname(os.path.abspath(sess.target)) or ".", exist_ok=True)
    if os.path.exists(sess.target):
        os.remove(sess.target)
    os.replace(part, sess.target)
    sess.state = "done"
    sess.message = "已完成"
    sess.downloaded = sess.size or got
    sess.finished = time.time()
    sess.speed = 0.0
    on_change(sess)
    shutil.rmtree(sess.tmpdir, ignore_errors=True)      # 临时 part 已改名，清掉空目录
