"""网络瓶颈排查。

从三层取证，定位"慢在哪一段"：
  1. 本机：网卡链路速率（理论带宽上限）
  2. 国内直连：DNS 解析 / TCP 握手延迟 / 实际下载速度
  3. 代理链路：代理可用性 / 延迟 / 实际下载速度

结论按「链路速率 → 国内直连 → 代理」逐级对比给出，避免把"节点慢"误判成"宽带慢"。
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
from urllib.parse import urlsplit

# 国内直连测速源（按顺序尝试，取第一个成功的）
CN_SPEED_URLS = [
    ("清华 TUNA", "https://mirrors.tuna.tsinghua.edu.cn/anaconda/archive/"
                  "Anaconda3-2021.05-Windows-x86_64.exe"),
    ("阿里云", "https://mirrors.aliyun.com/ubuntu-releases/22.04/ubuntu-22.04.5-live-server-amd64.iso"),
    ("中科大", "https://mirrors.ustc.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-live-server-amd64.iso"),
]
# 国外测速源（经代理访问）
OUT_SPEED_URLS = [
    ("OVH", "https://proof.ovh.net/files/10Mb.dat"),
    ("CacheFly", "https://cachefly.cachefly.net/10mb.test"),
    ("Hetzner", "https://speed.hetzner.de/10MB.bin"),
]
# 延迟探测目标
CN_HOSTS = ["mirrors.aliyun.com", "www.baidu.com", "mirrors.tuna.tsinghua.edu.cn"]
OUT_HOSTS = ["www.gstatic.com", "github.com", "www.cloudflare.com"]

TEST_BYTES = 4 * 1024 * 1024        # 每个测速读 4MB，够算稳定速度又不费流量


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
                    "bytes": got, "code": code}
        _log(progress, stage, f"  {name} 不可用（HTTP {code}，{got or 0} 字节），换下一个")
    return None


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

    # 国内直连绝对水平（单线程测速本来就跑不满千兆/2.5G，所以按绝对值判断更准）
    if cn_sp:
        if cn_sp < 3:
            out.append(f"国内直连只有 {cn_sp:.1f} MB/s，偏慢 —— 可能测速源单线程限速，"
                       "也可能宽带/线路有问题，可以换多个源再确认。")
        elif cn_sp < 10:
            out.append(f"国内直连 {cn_sp:.1f} MB/s，属于单线程正常水平。")
        else:
            out.append(f"国内直连 {cn_sp:.1f} MB/s，线路很好。")

    # 链路速率只作参考：单线程测速受服务器限速影响，不能直接当宽带上限
    if link and cn_sp:
        link_mb = link / 8.0
        if cn_sp >= link_mb * 0.6:
            out.append(f"已接近网卡链路上限（{link:.0f} Mbps ≈ {link_mb:.0f} MB/s）。")
        else:
            out.append(f"网卡链路 {link:.0f} Mbps（≈{link_mb:.0f} MB/s），"
                       f"单线程只跑到 {cn_sp / link_mb * 100:.0f}% —— 单线程跑不满高带宽是正常的，"
                       "多线程下载会明显更高。")

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
        if ratio < 0.2:
            out.append(f"代理速度只有国内直连的 {ratio * 100:.0f}%"
                       f"（{px_sp:.1f} vs {cn_sp:.1f} MB/s）—— **瓶颈在代理节点**，"
                       "换节点或换机场比升级宽带更有用。")
        elif ratio < 0.5:
            out.append(f"代理速度是国内直连的 {ratio * 100:.0f}%"
                       f"（{px_sp:.1f} vs {cn_sp:.1f} MB/s）—— 节点带宽偏弱，"
                       "下载大文件建议换节点。")
        elif ratio < 0.8:
            out.append(f"代理速度是国内直连的 {ratio * 100:.0f}%，节点带宽一般。")
        else:
            out.append(f"代理速度达到国内直连的 {ratio * 100:.0f}%，节点质量不错。")
        if px_sp < 1:
            out.append(f"不过代理绝对速度只有 {px_sp:.1f} MB/s，即使比例不差，实际下载也会偏慢。")
    elif res.get("proxy", {}).get("enabled") and not px_sp:
        out.append("代理已配置但测速失败 —— 节点可能不可用或被限速，建议换节点再测。")
    elif not res.get("proxy", {}).get("enabled"):
        out.append("当前是直连模式（或未配置代理），无法评估代理链路。"
                   "想测代理请在底部把网络切到「代理」。")

    # 高延迟 + 低速组合
    px_rtt = px.get("rtt_ms")
    if px_rtt and px_rtt > 800:
        out.append(f"代理延迟 {px_rtt:.0f} ms 很高，页面/小文件会明显卡顿（大文件受影响小）。")

    return out


# ---------------- 主流程 ----------------
def run(progress=None):
    """执行全部检测。progress(stage, text) 用于界面实时输出。"""
    res = {"link": {}, "proxy": {}, "dns": {}, "cn": {}, "proxy_test": {}, "verdict": []}

    # 1) 本机链路
    _log(progress, "link", "读取网卡链路速率 …")
    res["link"] = link_info()
    li = res["link"]
    if li.get("speed_mbps"):
        _log(progress, "link", f"  {li['name']}（{li['type'] or '网卡'}）链路速率 "
                               f"{li['speed_mbps']:.0f} Mbps ≈ {li['speed_mbps'] / 8:.0f} MB/s", "ok")
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

    _log(progress, "cn", "测试国内直连下载速度 …")
    cn = _try_sources(CN_SPEED_URLS, None, progress, "cn")
    if cn:
        res["cn"].update(cn)
        _log(progress, "cn", f"  {cn['src']} 直连 {cn['mbps']:.2f} MB/s",
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

        _log(progress, "px", "测试代理下载速度 …")
        p = f"http://{px[0]}:{px[1]}"
        out = _try_sources(OUT_SPEED_URLS, {"http": p, "https": p}, progress, "px")
        if out:
            res["proxy_test"].update(out)
            _log(progress, "px", f"  {out['src']} 经代理 {out['mbps']:.2f} MB/s",
                 _level_speed(out["mbps"]))
        else:
            _log(progress, "px", "  所有国外测速源均不可用（节点可能被限速或不可用）", "warn")
    else:
        _log(progress, "px", "未配置代理，跳过代理链路测试", "warn")

    # 6) 结论
    _log(progress, "done", "")
    res["verdict"] = verdicts(res)
    return res
