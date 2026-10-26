"""无界面引擎测试：直接驱动 Manager，验证分段/续传/降级/限速/性能。"""
import os
import sys
import time
import hashlib
import shutil
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.manager import Manager

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "big.bin")
DL = os.path.join(HERE, "_dl")
DATA = os.path.join(HERE, "_data")


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def wait(mgr, tid, timeout=180, quiet=True):
    t0 = time.time()
    peak = 0
    while time.time() - t0 < timeout:
        t = mgr.get(tid)
        if not t:
            return None, 0
        peak = max(peak, len([c for c in t.chunks if not c.done]))
        if t.state in ("done", "error", "canceled"):
            return t, peak
        if not quiet:
            print("   ", t.state, round(t.snapshot()["percent"] or 0, 1), "%",
                  round(t.speed / 1048576, 2), "MB/s")
        time.sleep(0.2)
    return mgr.get(tid), peak


def rpt(name, ok, extra=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return ok


def main():
    shutil.rmtree(DL, ignore_errors=True)
    shutil.rmtree(DATA, ignore_errors=True)
    mgr = Manager(DATA, DL, max_parallel=3, default_conn=8)
    results = []

    # 1) 多线程 + 内容一致
    print("== 1. 多线程分段（Range 服务器 :8899） ==")
    t0 = time.time()
    s = mgr.add("http://127.0.0.1:8899/big.bin", max_conn=8)
    t, peak = wait(mgr, s["id"])
    el = time.time() - t0
    ok = t and t.state == "done" and sha(t.target) == sha(SRC)
    results.append(rpt("内容一致", ok,
                       f"({el:.2f}s, 峰值连接 {peak}, 平均 {os.path.getsize(SRC)/el/1048576:.1f} MB/s)"))

    # 2) 无 Range 降级
    print("== 2. 无 Range 降级（:8897） ==")
    s = mgr.add("http://127.0.0.1:8897/big.bin", max_conn=8)
    t, peak = wait(mgr, s["id"])
    ok = t and t.state == "done" and sha(t.target) == sha(SRC)
    results.append(rpt("降级单连接且内容一致", ok, f"(range={t.supports_range}, 连接 {peak})"))

    # 3) 暂停续传
    print("== 3. 暂停 / 续传（慢速 :8896） ==")
    s = mgr.add("http://127.0.0.1:8896/big.bin", max_conn=6)
    tid = s["id"]
    t0 = time.time()
    while time.time() - t0 < 40:
        t = mgr.get(tid)
        if (t.snapshot()["percent"] or 0) > 10:
            break
        time.sleep(0.2)
    mgr.pause(tid)
    time.sleep(1.0)
    a = mgr.get(tid)
    pct_a = a.snapshot()["percent"]
    state_at_pause = a.state          # 立刻取快照值：a 是活引用，之后会变
    d1 = a.downloaded
    time.sleep(1.8)
    d2 = mgr.get(tid).downloaded
    frozen = abs(d2 - d1) < 65536
    mgr.resume(tid)
    t, _ = wait(mgr, tid, timeout=150)
    same = t.state == "done" and sha(t.target) == sha(SRC)
    ok = state_at_pause == "paused" and frozen and same
    results.append(rpt("暂停冻结 + 续传完整", ok,
                       f"(暂停于 {pct_a}%, state={state_at_pause}, 冻结={frozen}, "
                       f"最终={t.state}, 内容一致={same})"))

    # 4) 限速
    print("== 4. 全局限速 1024 KB/s ==")
    mgr.set_limits(speed_limit=1024 * 1024)
    t0 = time.time()
    s = mgr.add("http://127.0.0.1:8899/big.bin", max_conn=4)
    t, _ = wait(mgr, s["id"], timeout=180)
    el = time.time() - t0
    rate = os.path.getsize(SRC) / el / 1024
    ok = t.state == "done" and rate < 1600
    results.append(rpt("限速生效", ok, f"({rate:.0f} KB/s, {el:.1f}s)"))
    mgr.set_limits(speed_limit=0)

    # 5) 错误处理
    print("== 5. 404 错误态 ==")
    s = mgr.add("http://127.0.0.1:8899/nope.bin")
    t, _ = wait(mgr, s["id"], timeout=30)
    results.append(rpt("进入错误态", t.state == "error", f"({t.message[:40]})"))

    print()
    print(f"总计 {sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
