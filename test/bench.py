"""性能对比：单连接基线 vs MultiDL 多连接，各跑 3 轮取中位数。

服务器受控速率（每 64KB 延迟 50ms → 单连接约 1.24 MB/s），
连接数是唯一变量，因此能公平体现多线程收益。
"""
import os
import sys
import time
import shutil
import hashlib
import statistics
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.manager import Manager

URL = "http://127.0.0.1:8898/big.bin"
SRC = os.path.join(ROOT, "test", "big.bin")
SIZE = os.path.getsize(SRC)
BASE = os.path.join(ROOT, "test", "_bench")
ROUNDS = 3


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


SRC_SHA = sha(SRC)


def fresh(tag):
    d = os.path.join(BASE, tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def run_single(i):
    d = fresh(f"single{i}")
    dst = os.path.join(d, "f.bin")
    t0 = time.time()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(URL, headers={"User-Agent": "bench"})
    with op.open(req, timeout=30) as r, open(dst, "wb") as f:
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
    el = time.time() - t0
    return el, os.path.getsize(dst) == SIZE


def run_multidl(i, conns=8):
    d = fresh(f"multidl{i}")
    mgr = Manager(os.path.join(d, "data"), os.path.join(d, "dl"),
                  max_parallel=1, default_conn=conns)
    t0 = time.time()
    s = mgr.add(URL, max_conn=conns)
    tid = s["id"]
    peak = 0
    while time.time() - t0 < 180:
        t = mgr.get(tid)
        peak = max(peak, len([c for c in t.chunks if not c.done]))
        if t.state in ("done", "error"):
            break
        time.sleep(0.05)
    el = time.time() - t0
    ok = t.state == "done" and sha(t.target) == SRC_SHA
    return el, ok, peak


def med(xs):
    return statistics.median(xs) if xs else float("nan")


def main():
    print(f"目标 {URL}  {SIZE / 1048576:.0f} MB  |  服务器每 64KB 延迟 50ms")
    print(f"每项 {ROUNDS} 轮取中位数\n")

    single, multi = [], []
    for i in range(ROUNDS):
        el, ok = run_single(i)
        single.append(el)
        print(f"  单连接  #{i+1}: {el:6.2f}s  {SIZE/el/1048576:5.2f} MB/s  {'OK' if ok else 'BAD'}")

    for i in range(ROUNDS):
        el, ok, peak = run_multidl(i)
        if ok:
            multi.append(el)
        print(f"  MultiDL #{i+1}: {el:6.2f}s  {SIZE/el/1048576:5.2f} MB/s  连接{peak}  "
              f"{'OK' if ok else 'BAD'}")

    print()
    print(f"单连接中位数  : {med(single):6.2f}s   {SIZE/med(single)/1048576:5.2f} MB/s")
    if multi:
        print(f"MultiDL 中位数: {med(multi):6.2f}s   {SIZE/med(multi)/1048576:5.2f} MB/s"
              f"   加速 {med(single)/med(multi):.1f}x")


if __name__ == "__main__":
    main()
