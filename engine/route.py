"""自动选路：实测「直连」与「代理」两条路，选更快的那条。

为什么需要：很多海外站对国内直连限速（实测 lmstudio.ai 直连 0.28 MB/s、
走代理 1.70 MB/s，差 6 倍），但也有些站直连比代理更快（国内镜像走代理反而绕路）。
写死任何一条都不对，只能实测。

策略：
  - 直连不通          → 用代理
  - 代理不通          → 直连
  - 代理快 30% 以上   → 用代理
  - 其它情况          → 直连（省代理流量、少一跳）
结果按 host 缓存 10 分钟，同一站点不重复探测。
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse as up

CACHE_TTL = 600.0               # 同一 host 的选路结果缓存 10 分钟
SAMPLE_BYTES = 128 * 1024       # 单连接样本（小一点，慢站也能在超时内测出数）
SAMPLE_PER_CONN = 64 * 1024     # 多连接探测时每条连接读多少（小样本求快）
SAMPLE_CONNS = 8                # 探测并发数（与默认下载连接数一致）
SAMPLE_TIMEOUT = 1.5            # 单条路探测超时（只求比较快慢，不让用户干等）
PROXY_ADVANTAGE = 2.0           # 代理要比直连快 1 倍以上才切换（直连优先，省流量少一跳）
DIRECT_GOOD = 0.5               # 直连达到这个速度(MB/s)就不再测代理，直接用直连

_cache: dict[str, tuple] = {}   # host -> (use_proxy, ts, reason)
_lock = threading.Lock()
_host_locks: dict[str, threading.Lock] = {}
_host_locks_guard = threading.Lock()


def _host_lock(host):
    """同一 host 的探测串行化，避免多任务同时启动时重复测速。"""
    with _host_locks_guard:
        lk = _host_locks.get(host)
        if lk is None:
            lk = _host_locks[host] = threading.Lock()
        return lk


def _probe_size(url, proxies, timeout=6.0):
    """返回 (文件大小, 是否支持 Range)。"""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return 0, False
    r = None
    try:
        r = creq.get(url, impersonate="chrome", proxies=proxies, timeout=timeout,
                     allow_redirects=True, headers={"Range": "bytes=0-0"})
        cr = r.headers.get("Content-Range") or ""
        cl = (r.headers.get("Content-Length") or "").strip()
        m = re.search(r"/(\d+)$", cr)
        if m:
            return int(m.group(1)), True
        return (int(cl) if cl.isdigit() else 0), False
    except Exception:
        return 0, False
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def _sample(url, proxies, want=SAMPLE_BYTES, timeout=SAMPLE_TIMEOUT):
    """单连接下 want 字节，返回 (MB/s, size, ranged)；失败返回 (None, size, ranged)。

    顺便把 Content-Range 里的大小也带回来，省掉一次单独的大小探测请求。
    """
    size, ranged = 0, False
    try:
        from curl_cffi import requests as creq
    except Exception:
        return None
    t0 = time.monotonic()
    r = None
    try:
        r = creq.get(url, impersonate="chrome", proxies=proxies, stream=True,
                     timeout=timeout, allow_redirects=True,
                     headers={"Range": f"bytes=0-{want - 1}"})
        cr = r.headers.get("Content-Range") or ""
        mm = re.search(r"/(\d+)$", cr)
        if mm:
            size, ranged = int(mm.group(1)), True
        else:
            cl = (r.headers.get("Content-Length") or "").strip()
            size = int(cl) if cl.isdigit() else 0
        if r.status_code not in (200, 206):
            return None, size, ranged
        got = 0
        for chunk in r.iter_content(65536):
            got += len(chunk)
            if got >= want:
                break
        dt = max(time.monotonic() - t0, 0.05)
        if got < 64 * 1024:          # 几乎没数据，视为不可用
            return None, size, ranged
        return got / dt / 1048576.0, size, ranged
    except Exception:
        return None, size, ranged
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def _sample_parallel(url, proxies, size, conns=SAMPLE_CONNS,
                     per=SAMPLE_PER_CONN, timeout=SAMPLE_TIMEOUT):
    """多连接并行取样本，返回总吞吐 MB/s。

    必须多连接：很多站对单连接限速（实测 lmstudio.ai 直连单连接 0.28 MB/s，
    但 16 连接能到 5 MB/s；代理单连接 1.7 MB/s、8 连接却只有 2.5 MB/s）。
    用单连接样本比较，结论会和实际下载表现相反。
    """
    try:
        from curl_cffi import requests as creq
    except Exception:
        return None
    conns = max(1, min(int(conns), 16))
    counter = [0]
    lock = threading.Lock()
    t0 = time.monotonic()

    def worker(i):
        a = i * per
        b = min(a + per - 1, size - 1)
        if a >= size:
            return
        r = None
        try:
            r = creq.get(url, impersonate="chrome", proxies=proxies, stream=True,
                         timeout=timeout, allow_redirects=True,
                         headers={"Range": f"bytes={a}-{b}"})
            if r.status_code not in (200, 206):
                return
            got = 0
            for chunk in r.iter_content(65536):
                got += len(chunk)
                with lock:
                    counter[0] += len(chunk)
                if got >= per:
                    break
        except Exception:
            pass
        finally:
            try:
                if r is not None:
                    r.close()
            except Exception:
                pass

    ts = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(conns)]
    for t in ts:
        t.start()
    # 注意：必须用「总截止时间」，逐个 join(timeout) 会串行等待，8 个线程能等出 20 秒
    deadline = time.monotonic() + timeout
    for t in ts:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        t.join(timeout=remain)
    el = max(time.monotonic() - t0, 0.05)
    total = counter[0]
    if total < 32 * 1024:            # 样本本来就不大，有一点数据就能算速度
        return None
    return total / el / 1048576.0


def _measure(url, proxies):
    """测一条路的速度：优先多连接，拿不到大小就退回单连接。"""
    size, ranged = _probe_size(url, proxies)
    if ranged and size > SAMPLE_PER_CONN * 2:
        v = _sample_parallel(url, proxies, size)
        if v:
            return v
    return _sample(url, proxies)


def _probe_both(url, px):
    """测两条路。返回 (直连速度, 代理速度, 是否跳过了代理探测)。

    直连够快就直接返回，不再花时间测代理（国内站点大多如此，省 1-2 秒）。
    """
    p = f"http://{px[0]}:{px[1]}" if px else None
    pdict = {"http": p, "https": p} if p else None

    # 1) 单连接测直连，一次请求同时拿到速度 + 文件大小
    direct, size, ranged = _sample(url, None)
    if direct is not None and direct >= DIRECT_GOOD:
        return direct, None, True          # 直连够快，不必测代理

    # 2) 直连慢或不通，才测代理（有大小就走多连接，更接近真实下载）
    proxied = None
    if pdict:
        if ranged and size > SAMPLE_PER_CONN * 2:
            proxied = _sample_parallel(url, pdict, size)
        else:
            proxied, _, _ = _sample(url, pdict)
    return direct, proxied, False


def decide(url, force=False):
    """给这个 URL 选一条路。返回 (use_proxy, reason)。

    结果按 host 缓存；force=True 时强制重新探测。
    """
    host = (up.urlsplit(url).hostname or "").lower()
    if not host:
        return False, "无法解析主机名"

    if not force:
        with _lock:
            c = _cache.get(host)
        if c and time.time() - c[1] < CACHE_TTL:
            return c[0], c[2]

    # 同一 host 只探测一次：后来的任务等前面测完直接复用结果
    with _host_lock(host):
        if not force:
            with _lock:
                c = _cache.get(host)
            if c and time.time() - c[1] < CACHE_TTL:
                return c[0], c[2]

        try:
            from .proxy import system_proxy
            px = system_proxy()
        except Exception:
            px = None
        if not px:
            with _lock:
                _cache[host] = (False, time.time(), "未配置代理，直连")
            return False, "未配置代理，直连"

        direct, proxied, skipped = _probe_both(url, px)

        if direct is None and proxied is None:
            use, reason = False, "直连与代理都测不出速度，默认直连"
        elif direct is None:
            use, reason = True, f"直连不可用（超时/失败），改用代理（{proxied:.2f} MB/s）"
        elif skipped:
            use, reason = False, f"直连够快（{direct:.2f} MB/s），无需代理"
        elif proxied is None:
            use, reason = False, f"代理不可用（超时/失败），走直连（{direct:.2f} MB/s）"
        elif proxied > direct * PROXY_ADVANTAGE:
            use = True
            reason = f"代理更快（{proxied:.2f} vs 直连 {direct:.2f} MB/s）"
        else:
            use = False
            reason = f"直连够快（{direct:.2f} vs 代理 {proxied:.2f} MB/s）"

        with _lock:
            _cache[host] = (use, time.time(), reason)
        return use, reason


def cached(host):
    """读缓存（不触发探测），供界面显示。"""
    with _lock:
        c = _cache.get((host or "").lower())
    return (c[0], c[2]) if c else (None, "")


def clear():
    with _lock:
        _cache.clear()
