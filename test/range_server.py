"""测试用 HTTP 服务器：支持 Range、可关闭 Range、可限速。"""
import os
import re
import sys
import time
import hashlib
import argparse
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILES = {}
LOCK = threading.Lock()
NO_RANGE = False
SLOW = 0.0


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        name = self.path.split("?")[0].lstrip("/")
        if name == "no-range":
            name = list(FILES)[0]
            no_range = True
        else:
            no_range = NO_RANGE
        if name not in FILES:
            self.send_error(404)
            return
        path, size = FILES[name]
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and not no_range:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                else:
                    end = size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "none" if no_range else "bytes")
        self.send_header("ETag", '"test-etag-v1"')
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return

        with open(path, "rb") as f:
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
                    if SLOW:
                        time.sleep(SLOW)
            except (BrokenPipeError, ConnectionResetError):
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--file", action="append", default=[], help="name=path")
    ap.add_argument("--no-range", action="store_true")
    ap.add_argument("--slow", type=float, default=0.0)
    a = ap.parse_args()
    global NO_RANGE, SLOW
    NO_RANGE, SLOW = a.no_range, a.slow
    for spec in a.file:
        name, path = spec.split("=", 1)
        FILES[name] = (path, os.path.getsize(path))
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    srv.daemon_threads = True
    print("serving", list(FILES), "on", a.port, "no_range=", NO_RANGE)
    srv.serve_forever()


if __name__ == "__main__":
    main()
