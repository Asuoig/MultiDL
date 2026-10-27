"""网络瓶颈排查。

从三层取证，定位"慢在哪一段"：
  1. 本机：网卡链路速率（理论带宽上限）
  2. 国内直连：DNS 解析 / TCP 握手延迟 / 实际下载速度
  3. 代理链路：代理可用性 / 延迟 / 实际下载速度

结论按「链路速率 → 国内直连 → 代理」逐级对比给出，避免把"节点慢"误判成"宽带慢"。
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

# 国内直连测速源（按顺序尝试，取第一个成功的）
CN_SPEED_URLS = [
    ("清华 TUNA", "https://mirrors.tuna.tsinghua.edu.cn/anaconda/archive/"
                  "Anaconda3-2021.05-Windows-x86_64.exe"),
    ("阿里云", "https://mirrors.aliyun.com/ubuntu-releases/22.04/ubuntu-22.04.5-live-server-amd64.iso"),
    ("中科大", "https://mirrors.ustc.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-live-server-amd64.iso"),
]
# 国外测速源（经代理访问）。多线程测速需要大文件，优先 1GB 的 OVH
OUT_SPEED_URLS = [
    ("OVH", "https://proof.ovh.net/files/1Gb.dat"),
    ("OVH-100M", "https://proof.ovh.net/files/100Mb.dat"),
    ("CacheFly", "https://cachefly.cachefly.net/100mb.test"),
]
# 延迟探测目标
CN_HOSTS = ["mirrors.aliyun.com", "www.baidu.com", "mirrors.tuna.tsinghua.edu.cn"]
OUT_HOSTS = ["www.gstatic.com", "github.com", "www.cloudflare.com"]

TEST_BYTES = 4 * 1024 * 1024        # 快速模式：每个测速读 4MB
MT_DURATION = 3.0                   # 精确模式：测速时长（秒）
MT_WARMUP = 1.0                     # 丢弃前 1 秒（TCP 慢启动阶段，算进去会低估）
MT_MAX_TOTAL = 400 * 1024 * 1024    # 精确模式最多读 400MB，避免流量失控
MT_CONNS = 8                        # 默认并发连接数（与下载引擎默认一致）

# 上传测速端点（接受 POST 并丢弃数据）。走代理时该域名 SSL 常被节点阻断，所以只测直连
UPLOAD_URL = "https://speed.cloudflare.com/__up"
UP_DURATION = 3.0
UP_MAX_TOTAL = 200 * 1024 * 1024


def fmt_speed(mbps):
    """速度双单位：47.35 MB/s (379 Mbps)。"""
    try:
        v = float(mbps or 0)
    except (TypeError, ValueError):
        return "-"
    if v <= 0:
        return "-"
    return f"{v:.2f} MB/s ({v * 8:.0f} Mbps)"


def _log(progress, stage, text, level=None):
    """level: 'ok' | 'warn' | None，供界面着色。"""
    if progress:
        try:
            progress(stage, text, level)
        except TypeError:
            progress(stage, text)
        except Exception:
            pass


def _level_rtt(ms):
    if ms is None:
        return "warn"
    if ms < 50:
        return "ok"
    if ms < 300:
        return None
    return "warn"


def _level_speed(mbps):
    if not mbps:
        return "warn"
    if mbps >= 5:
        return "ok"
    if mbps >= 1:
        return None
    return "warn"


def _level_up(mbps):
    """上行带宽阈值比下行低（家宽上行普遍只有下行的 1/10 左右）。"""
    if not mbps:
        return "warn"
    if mbps >= 3:
        return "ok"
    if mbps >= 1:
        return None
    return "warn"


# ---------------- 1. 本机链路 ----------------
def link_info():
    """网卡名 / 链路速率 / 类型。拿不到返回 speed_mbps=0。"""
    out = {"name": "", "speed_mbps": 0, "desc": "", "type": ""}
    try:
        ps = ("Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
              "Select-Object Name,LinkSpeed,InterfaceDescription | ConvertTo-Json -Compress")
        # 中文 Windows 的 PowerShell 输出是 GBK，不能按 UTF-8 解
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=12,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = r.stdout or b""
        text = ""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        data = json.loads((text or "").strip() or "[]")
        if isinstance(data, dict):
            data = [data]
        best = None
        for d in data:
            sp = str(d.get("LinkSpeed") or "")
            mbps = 0.0
            try:
                head = sp.split()[0] if sp.split() else "0"
                num = float("".join(ch for ch in head if ch.isdigit() or ch == ".") or 0)
                unit = sp.lower()
                if "gbps" in unit:
                    mbps = num * 1000.0
                elif "kbps" in unit:
                    mbps = num / 1000.0
                elif "mbps" in unit:
                    mbps = num
                else:
                    mbps = 0.0
            except Exception:
                mbps = 0.0
            if best is None or mbps > best["speed_mbps"]:
                best = {"name": d.get("Name") or "", "speed_mbps": mbps,
                        "desc": d.get("InterfaceDescription") or "", "type": ""}
        if best:
            out = best
            d = (out["desc"] or "").lower()
            out["type"] = ("无线" if any(k in d for k in ("wi-fi", "wireless", "wlan", "802.11"))
                           else "以太网" if any(k in d for k in ("ethernet", "gbe", "realtek", "intel"))
                           else "")
    except Exception:
        pass
    return out


# ---------------- 2. 延迟 / DNS ----------------
def dns_time(host):
    t0 = time.monotonic()
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return (time.monotonic() - t0) * 1000.0
    except Exception:
        return None


def tcp_rtt(host, port=443, tries=3, timeout=3.0):
    """TCP 握手 RTT（取多次最小值，排除抖动）。返回 ms 或 None。"""
    best = None
    for _ in range(tries):
        t0 = time.monotonic()
        try:
            s = socket.create_connection((host, port), timeout)
            s.close()
            dt = (time.monotonic() - t0) * 1000.0
            best = dt if best is None else min(best, dt)
        except Exception:
            continue
    return best


# ---------------- 3. 下载测速 ----------------
def _download_speed(url, proxies=None, want=TEST_BYTES, timeout=15):
    """读固定字节算速度。优先 curl_cffi（能过反爬），没装则退回 http.client。"""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return _download_speed_plain(url, proxies, want, timeout)
    t0 = time.monotonic()
    try:
        r = creq.get(url, impersonate="chrome", proxies=proxies, stream=True, timeout=timeout,
                     headers={"Range": f"bytes=0-{want - 1}"})
        if r.status_code not in (200, 206):
            r.close()
            return None, r.status_code, 0.0
        got = 0
        for chunk in r.iter_content(262144):
            got += len(chunk)
            if got >= want:
                break
        dt = max(time.monotonic() - t0, 0.01)
        r.close()
        return got, r.status_code, got / dt
    except Exception:
        return None, 0, 0.0


def _download_speed_plain(url, proxies, want, timeout):
    import http.client
    import ssl
    u = urlsplit(url)
    try:
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    except Exception:
        return None, 0, 0.0
    t0 = time.monotonic()
    try:
        conn.request("GET", (u.path or "/") + (("?" + u.query) if u.query else ""),
                     headers={"User-Agent": "Mozilla/5.0",
                              "Range": f"bytes=0-{want - 1}"})
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            return None, resp.status, 0.0
        got = 0
        while got < want:
            b = resp.read(262144)
            if not b:
                break
            got += len(b)
        dt = max(time.monotonic() - t0, 0.01)
        return got, resp.status, got / dt
    except Exception:
        return None, 0, 0.0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _try_sources(sources, proxies, progress, stage):
    """依次尝试测速源，返回第一个速度达标的。"""
    for name, url in sources:
        _log(progress, stage, f"测速中：{name} …")
        got, code, bps = _download_speed(url, proxies)
        if got and got >= 512 * 1024:          # 至少拿到 512KB 才算有效
            return {"src": name, "url": url, "mbps": bps / 1048576.0,
                    "bytes": got, "code": code, "conns": 1, "precise": False}
        _log(progress, stage, f"  {name} 不可用（HTTP {code}，{got or 0} 字节），换下一个")
    return None


# ---------------- 多线程精确测速 ----------------
class _Counter:
    __slots__ = ("n", "_lock")

    def __init__(self):
        self.n = 0
        self._lock = threading.Lock()

    def add(self, k):
        with self._lock:
            self.n += k

    def get(self):
        with self._lock:
            return self.n


def _probe_range(url, proxies, timeout=8):
    """返回 (文件大小, 是否支持 Range)。"""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return 0, False
    try:
        r = creq.get(url, impersonate="chrome", proxies=proxies, timeout=timeout,
                     headers={"Range": "bytes=0-0"})
        cr = r.headers.get("Content-Range") or ""
        cl = (r.headers.get("Content-Length") or "").strip()
        r.close()
        m = re.search(r"/(\d+)$", cr)
        if m:
            return int(m.group(1)), True
        return (int(cl) if cl.isdigit() else 0), False
    except Exception:
        return 0, False


def _mt_worker(url, start, end, proxies, counter, stop, cap):
    """一个连接负责 [start, end] 区间，读到结束或被叫停。"""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return
    got = 0
    r = None
    try:
        r = creq.get(url, impersonate="chrome", proxies=proxies, stream=True, timeout=15,
                     headers={"Range": f"bytes={start}-{end}"})
        if r.status_code not in (200, 206):
            return
        for chunk in r.iter_content(131072):
            if stop.is_set():
                break
            counter.add(len(chunk))
            got += len(chunk)
            if cap and got >= cap:
                break
    except Exception:
        pass
    finally:
        try:
            if r is not None:
                r.close()
        except Exception:
            pass


def multithread_speed(url, proxies=None, conns=MT_CONNS, duration=MT_DURATION,
                      warmup=MT_WARMUP, max_total=MT_MAX_TOTAL, on_tick=None):
    """多连接并行测速，返回稳态速度（MB/s）。

    为什么比单线程准：
      1. 多连接绕开 TCP 拥塞窗口和服务器单连接限速
      2. 用固定时间窗口，而不是固定字节数（小文件测不出高带宽）
      3. 丢弃前 warmup 秒，避开 TCP 慢启动的低速阶段
      4. 只在稳态区间内做「字节增量 ÷ 时间」，不受启动瞬间影响
    """
    size, ranged = _probe_range(url, proxies)
    if not ranged or size <= 0:
        # 不支持分段 → 只能单线程
        got, code, bps = _download_speed(url, proxies, want=TEST_BYTES)
        if not got:
            return None
        return {"url": url, "mbps": bps / 1048576.0, "bytes": got, "code": code,
                "conns": 1, "precise": False, "size": size, "ranged": False}

    conns = max(1, min(int(conns), 16))
    per = max(1, size // conns)
    cap = max(1, max_total // conns)

    counter = _Counter()
    stop = threading.Event()
    threads = []
    for i in range(conns):
        a = i * per
        b = (size - 1) if i == conns - 1 else (a + per - 1)
        t = threading.Thread(target=_mt_worker,
                             args=(url, a, b, proxies, counter, stop, cap), daemon=True)
        t.start()
        threads.append(t)

    samples = []
    t0 = time.monotonic()
    while True:
        el = time.monotonic() - t0
        samples.append((el, counter.get()))
        if on_tick:
            try:
                on_tick(el, samples[-1][1])
            except Exception:
                pass
        if el >= duration:
            break
        time.sleep(0.1)

    stop.set()
    for t in threads:
        t.join(timeout=3)

    total = counter.get()
    if total < 256 * 1024:                 # 几乎没数据 → 视为失败
        return None

    # 稳态窗口：丢弃 warmup 之前的采样点
    pts = [(t, v) for t, v in samples if t >= warmup]
    if len(pts) >= 2 and (pts[-1][0] - pts[0][0]) > 0.2:
        dt = pts[-1][0] - pts[0][0]
        dv = pts[-1][1] - pts[0][1]
        steady = dv / dt if dt > 0 else total / max(duration, 0.01)
    else:
        steady = total / max(duration, 0.01)

    # 峰值：滑动 1 秒窗口内的最大吞吐。
    # 带宽上限要看峰值 —— 中途卡一下会把平均值拉低，但线路能力没变。
    peak = steady
    j = 0
    for i in range(len(samples)):
        while samples[i][0] - samples[j][0] > 1.0:
            j += 1
        span = samples[i][0] - samples[j][0]
        if span >= 0.9:
            peak = max(peak, (samples[i][1] - samples[j][1]) / span)

    return {"url": url, "mbps": steady / 1048576.0, "peak_mbps": peak / 1048576.0,
            "bytes": total,
            "avg_mbps": total / max(duration, 0.01) / 1048576.0,
            "conns": conns, "precise": True, "size": size, "ranged": True,
            "duration": duration}


def _try_sources_mt(sources, proxies, progress, stage, conns=MT_CONNS):
    """多线程模式：依次尝试测速源，实时报告速度。"""
    for name, url in sources:
        _log(progress, stage, f"测速中：{name}（{conns} 连接并行）…")
        state = {"last": -1.0}

        def tick(el, total, _name=name, _st=state):
            # 前 0.8 秒是 TCP 慢启动，报速度没意义；之后每 1.5 秒报一次
            if el < 0.8:
                return
            if el - _st["last"] >= 1.5:
                _st["last"] = el
                _log(progress, stage,
                     f"  {_name} 实时 {fmt_speed(total / 1048576 / max(el, 0.1))}")

        res = multithread_speed(url, proxies, conns=conns, on_tick=tick)
        if res:
            res["src"] = name
            return res
        _log(progress, stage, f"  {name} 不可用，换下一个")
    return None


# ---------------- 上传测速 ----------------
def _up_worker(host, port, path, counter, stop, cap):
    """chunked 上传，边发边计数。

    注意：发送速度会被 TCP 发送缓冲区"吸收"而虚高，
    所以调用方必须等本函数收到服务器响应后再算总时间。
    """
    import http.client
    import ssl
    conn = None
    sent = 0
    try:
        conn = http.client.HTTPSConnection(host, port, timeout=20,
                                           context=ssl.create_default_context())
        conn.putrequest("POST", path)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("User-Agent", "Mozilla/5.0")
        conn.endheaders()
        chunk = b"\0" * 65536
        while not stop.is_set():
            conn.send(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            counter.add(len(chunk))
            sent += len(chunk)
            if cap and sent >= cap:
                break
        conn.send(b"0\r\n\r\n")
        try:
            resp = conn.getresponse()
            resp.read()                    # 等服务器收完 → 数据才真正上线
        except Exception:
            pass
    except Exception:
        pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def upload_speed(url=UPLOAD_URL, conns=MT_CONNS, duration=UP_DURATION,
                 max_total=UP_MAX_TOTAL, on_tick=None):
    """多连接上传测速，返回真实上行吞吐（MB/s）。

    关键点：TCP 发送缓冲区会先吞下数据让 send() 立即返回，
    若只统计"发送阶段"会严重虚高（实测 8 连接 145 Mbps vs 真实 72 Mbps）。
    所以必须等所有连接收到服务器响应，用「总字节 ÷ 总耗时」算。
    """
    u = urlsplit(url)
    host = u.hostname
    port = u.port or 443
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    conns = max(1, min(int(conns), 16))
    cap = max(1, max_total // conns)

    counter = _Counter()
    stop = threading.Event()
    threads = []
    for _ in range(conns):
        t = threading.Thread(target=_up_worker,
                             args=(host, port, path, counter, stop, cap), daemon=True)
        t.start()
        threads.append(t)

    t0 = time.monotonic()
    while True:
        el = time.monotonic() - t0
        if on_tick:
            try:
                on_tick(el, counter.get())
            except Exception:
                pass
        if el >= duration:
            break
        time.sleep(0.1)

    stop.set()
    for t in threads:
        t.join(timeout=25)
    elapsed = max(time.monotonic() - t0, 0.01)
    total = counter.get()
    if total < 256 * 1024:
        return None
    return {"url": url, "mbps": total / elapsed / 1048576.0, "bytes": total,
            "seconds": elapsed, "conns": conns}


# ---------------- 4. 代理链路 ----------------
def proxy_target():
    """当前生效的代理 (host, port) 或 None。"""
    try:
        from .proxy import active_proxy
        return active_proxy()
    except Exception:
        return None


def proxy_reachable(px, timeout=2.0):
    try:
        s = socket.create_connection(px, timeout)
        s.close()
        return True
    except Exception:
        return False


def proxy_http_rtt(px, url="https://www.gstatic.com/generate_204", timeout=10):
    """经代理访问一个 204 端点，返回总耗时 ms（含代理转发 + 握手）。"""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return None
    p = f"http://{px[0]}:{px[1]}"
    t0 = time.monotonic()
    try:
        r = creq.get(url, impersonate="chrome", proxies={"http": p, "https": p}, timeout=timeout)
        dt = (time.monotonic() - t0) * 1000.0
        ok = r.status_code in (200, 204)
        r.close()
        return dt if ok else None
    except Exception:
        return None


# ---------------- 5. 结论 ----------------
def verdicts(res):
    """根据实测数据给出瓶颈判断。"""
    out = []
    link = res["link"]["speed_mbps"]
    cn = res.get("cn") or {}
    px = res.get("proxy_test") or {}
    cn_sp = cn.get("mbps") or 0
    px_sp = px.get("mbps") or 0
    cn_rtt = cn.get("rtt_ms")

    # 国内直连绝对水平（上限看峰值：中途卡顿不该被当成带宽不够）
    cn_conns = cn.get("conns") or 1
    cn_precise = cn.get("precise")
    cn_mode = f"{cn_conns} 连接并行" if cn_precise else "单线程"
    cn_peak = cn.get("peak_mbps") or cn_sp
    judge = max(cn_sp, cn_peak)
    peak_txt = f"，峰值 {fmt_speed(cn_peak)}" if cn_peak > cn_sp * 1.15 else ""
    if cn_sp:
        if judge < 3:
            out.append(f"国内直连只有 {fmt_speed(cn_sp)}（{cn_mode}），偏慢 —— "
                       "测速源限速、宽带或线路都可能有问题，可以换多个源再确认。")
        elif judge < 10:
            out.append(f"国内直连 {fmt_speed(cn_sp)}{peak_txt}（{cn_mode}），属于正常水平。")
        else:
            out.append(f"国内直连 {fmt_speed(cn_sp)}{peak_txt}（{cn_mode}），线路很好。")

    # 链路速率对比：多线程才可能跑满链路，单线程跑不满是正常的
    if link and cn_sp:
        link_mb = link / 8.0
        ratio = judge / link_mb if link_mb else 0
        if ratio >= 0.6:
            out.append(f"已达到网卡链路的 {ratio * 100:.0f}%（{link:.0f} Mbps / "
                       f"{link_mb:.1f} MB/s），带宽基本跑满。")
        elif cn_precise:
            out.append(f"网卡链路 {link:.0f} Mbps（{link_mb:.1f} MB/s），"
                       f"{cn_conns} 连接并行只跑到 {ratio * 100:.0f}% —— "
                       "瓶颈不在本机网卡，而在宽带套餐、测速源或线路。")
        else:
            out.append(f"网卡链路 {link:.0f} Mbps（{link_mb:.1f} MB/s），"
                       f"单线程跑到 {ratio * 100:.0f}% —— 单线程跑不满高带宽是正常的，"
                       "想看真实上限请用「精确模式」。")

    # 延迟判断
    if cn_rtt is not None:
        if cn_rtt < 30:
            out.append(f"国内延迟 {cn_rtt:.0f} ms，很好。")
        elif cn_rtt < 80:
            out.append(f"国内延迟 {cn_rtt:.0f} ms，正常。")
        else:
            out.append(f"国内延迟 {cn_rtt:.0f} ms 偏高，可能是跨网或线路绕行。")

    # 代理对比
    if px_sp and cn_sp:
        ratio = px_sp / cn_sp
        pct_txt = f"{ratio * 100:.1f}%" if ratio >= 0.01 else "不到 1%"
        if ratio < 0.2:
            out.append(f"代理速度只有国内直连的 {pct_txt}"
                       f"（{fmt_speed(px_sp)} vs {fmt_speed(cn_sp)}）—— 【瓶颈在代理节点】，"
                       "换节点或换机场比升级宽带更有用。")
        elif ratio < 0.5:
            out.append(f"代理速度是国内直连的 {pct_txt}"
                       f"（{fmt_speed(px_sp)} vs {fmt_speed(cn_sp)}）—— 节点带宽偏弱，"
                       "下载大文件建议换节点。")
        elif ratio < 0.8:
            out.append(f"代理速度是国内直连的 {pct_txt}，节点带宽一般。")
        else:
            out.append(f"代理速度达到国内直连的 {pct_txt}，节点质量不错。")
        if px_sp < 1:
            out.append(f"不过代理绝对速度只有 {fmt_speed(px_sp)}，即使比例不差，实际下载也会偏慢。")
    elif res.get("proxy", {}).get("enabled") and not px_sp:
        out.append("代理已配置但测速失败 —— 节点可能不可用或被限速，建议换节点再测。")
    elif not res.get("proxy", {}).get("enabled"):
        out.append("当前是直连模式（或未配置代理），无法评估代理链路。"
                   "想测代理请在底部把网络切到「代理」。")

    # 高延迟 + 低速组合
    px_rtt = px.get("rtt_ms")
    if px_rtt and px_rtt > 800:
        out.append(f"代理延迟 {px_rtt:.0f} ms 很高，页面/小文件会明显卡顿（大文件受影响小）。")

    # 上传带宽：上行不够/被占满时，下载的 ACK 回传会被拖慢
    up = res.get("upload") or {}
    up_sp = up.get("mbps") or 0
    if up_sp:
        if up_sp < 1:
            out.append(f"上传带宽只有 {fmt_speed(up_sp)}，偏低 —— 上传文件、做种、视频通话会卡；"
                       "如果下载慢的同时后台有程序在占上行（网盘同步、P2P 做种、云备份），"
                       "TCP 的 ACK 回传被拖慢，下载也会跟着变慢，先把上行占用清掉再测。")
        elif up_sp < 3:
            out.append(f"上传 {fmt_speed(up_sp)}，属于常见的家宽不对称上行，日常够用。")
        else:
            out.append(f"上传 {fmt_speed(up_sp)}，上行比较充裕。")

        if cn_sp and up_sp:
            ratio = up_sp / cn_sp
            if ratio < 0.05:
                out.append(f"上行只有下行的 {ratio * 100:.1f}%，典型不对称宽带，属正常现象。")
            elif ratio > 0.6:
                out.append(f"上行是下行的 {ratio * 100:.0f}%，接近对等带宽。")
    elif res.get("upload") == {}:
        pass                                    # 上传测速被跳过/失败，不输出结论

    return out


# ---------------- 主流程 ----------------
def run(progress=None, precise=True, conns=MT_CONNS):
    """执行全部检测。

    precise=True 走多连接精确测速（绕开单连接限速、丢弃慢启动、取稳态）；
    False 走单线程快速测速（流量小但受单连接限制）。
    progress(stage, text, level) 用于界面实时输出。
    """
    res = {"link": {}, "proxy": {}, "dns": {}, "cn": {}, "proxy_test": {},
           "upload": {}, "verdict": []}

    # 1) 本机链路
    _log(progress, "link", "读取网卡链路速率 …")
    res["link"] = link_info()
    li = res["link"]
    if li.get("speed_mbps"):
        _log(progress, "link", f"  {li['name']}（{li['type'] or '网卡'}）链路速率 "
                               f"{li['speed_mbps']:.0f} Mbps ({li['speed_mbps'] / 8:.1f} MB/s)", "ok")
    else:
        _log(progress, "link", "  未能读取链路速率（不影响其他检测）")

    # 2) 代理状态
    _log(progress, "proxy", "检查代理配置 …")
    px = proxy_target()
    try:
        from .proxy import current_mode, describe
        mode, desc = current_mode(), describe()
    except Exception:
        mode, desc = "auto", ""
    res["proxy"] = {"enabled": bool(px), "addr": f"{px[0]}:{px[1]}" if px else "",
                    "mode": mode, "desc": desc,
                    "port_ok": proxy_reachable(px) if px else False}
    _log(progress, "proxy", f"  模式={mode}  {desc}"
                            + ("" if not px else f"  端口{'可连接' if res['proxy']['port_ok'] else '不可连接'}"),
         "ok" if (not px or res["proxy"]["port_ok"]) else "warn")

    # 3) DNS
    _log(progress, "dns", "解析域名 …")
    res["dns"] = {"cn": dns_time("mirrors.aliyun.com"), "out": dns_time("www.gstatic.com")}
    if res["dns"]["cn"]:
        _log(progress, "dns", f"  国内域名解析 {res['dns']['cn']:.0f} ms",
         _level_rtt(res["dns"]["cn"]))
    if res["dns"]["out"]:
        _log(progress, "dns", f"  国外域名解析 {res['dns']['out']:.0f} ms",
         _level_rtt(res["dns"]["out"]))

    # 4) 国内直连
    _log(progress, "cn", "测试国内直连延迟 …")
    rtts = [(h, tcp_rtt(h)) for h in CN_HOSTS]
    ok_rtts = [(h, t) for h, t in rtts if t is not None]
    res["cn"]["rtt_ms"] = min(t for _, t in ok_rtts) if ok_rtts else None
    res["cn"]["rtt_host"] = min(ok_rtts, key=lambda x: x[1])[0] if ok_rtts else ""
    if res["cn"]["rtt_ms"]:
        _log(progress, "cn", f"  最低延迟 {res['cn']['rtt_ms']:.0f} ms（{res['cn']['rtt_host']}）",
         _level_rtt(res["cn"]["rtt_ms"]))
    else:
        _log(progress, "cn", "  国内延迟全部失败", "warn")

    mode_txt = f"{conns} 连接并行" if precise else "单线程"
    _log(progress, "cn", f"测试国内直连下载速度（{mode_txt}）…")
    cn = (_try_sources_mt(CN_SPEED_URLS, None, progress, "cn", conns) if precise
          else _try_sources(CN_SPEED_URLS, None, progress, "cn"))
    if cn:
        res["cn"].update(cn)
        _log(progress, "cn", f"  {cn['src']} 直连 {fmt_speed(cn['mbps'])}",
             _level_speed(cn["mbps"]))
    else:
        _log(progress, "cn", "  所有国内测速源均不可用", "warn")

    # 5) 代理链路
    if px:
        _log(progress, "px", "测试代理链路延迟 …")
        res["proxy_test"]["rtt_ms"] = proxy_http_rtt(px)
        if res["proxy_test"]["rtt_ms"]:
            _log(progress, "px", f"  代理访问国外 {res['proxy_test']['rtt_ms']:.0f} ms",
             _level_rtt(res["proxy_test"]["rtt_ms"]))
        else:
            _log(progress, "px", "  代理访问国外失败", "warn")

        _log(progress, "px", f"测试代理下载速度（{mode_txt}）…")
        p = f"http://{px[0]}:{px[1]}"
        out = (_try_sources_mt(OUT_SPEED_URLS, {"http": p, "https": p}, progress, "px", conns)
               if precise else
               _try_sources(OUT_SPEED_URLS, {"http": p, "https": p}, progress, "px"))
        if out:
            res["proxy_test"].update(out)
            _log(progress, "px", f"  {out['src']} 经代理 {fmt_speed(out['mbps'])}",
                 _level_speed(out["mbps"]))
        else:
            _log(progress, "px", "  所有国外测速源均不可用（节点可能被限速或不可用）", "warn")
    else:
        _log(progress, "px", "未配置代理，跳过代理链路测试", "warn")

    # 6) 上传带宽（直连）——上行不够时，下载的 ACK 回传会被拖慢
    up_conns = conns if precise else 4
    _log(progress, "up", f"测试上传带宽（直连，{up_conns} 连接）…")
    up_state = {"last": 0.0}

    def up_tick(el, total):
        if el < 0.8:
            return
        if el - up_state["last"] >= 1.5:
            up_state["last"] = el
            _log(progress, "up", f"  已发送 {total / 1048576:.1f} MB …")

    up = upload_speed(conns=up_conns, on_tick=up_tick)
    if up:
        res["upload"] = up
        _log(progress, "up", f"  上传 {fmt_speed(up['mbps'])}（{up['conns']} 连接，"
                             f"{up['bytes'] / 1048576:.1f} MB / {up['seconds']:.1f}s）",
             _level_up(up["mbps"]))
    else:
        _log(progress, "up", "  上传测速失败（端点不可达，不影响其他结论）", "warn")

    # 7) 结论
    _log(progress, "done", "")
    res["verdict"] = verdicts(res)
    return res
