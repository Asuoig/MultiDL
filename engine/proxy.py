"""代理支持（纯标准库，零第三方依赖）。

优先级：`--proxy` 显式指定 > Windows 系统代理 > 直连。

- 自动识别 HTTP/HTTPS 系统代理（Clash / V2RayN / Shadowsocks / Netch / sing-box 等开启「系统代理」都会写注册表）
- 尊重系统代理的「例外 / 绕过列表」ProxyOverride（内网地址不会误入代理）
- 只支持 HTTP 代理（CONNECT 隧道）。SOCKS-only 的系统代理会**跳过并直连**（TUN 模式下直连也能通）
- 连接失败时由 core.py 自动退回直连，代理配错也**不会让下载失败**
"""
from __future__ import annotations

import sys
import time
import fnmatch

_override = None                                  # (host, port) | "none" | "force" | None
_cache = {"at": 0.0, "proxy": None, "bypass": ""}
_CACHE_TTL = 5.0


def _parse(s):
    s = (s or "").strip()
    if not s:
        return None
    for pre in ("http://", "https://", "socks5h://", "socks5://", "socks4://", "socks://"):
        if s.lower().startswith(pre):
            s = s[len(pre):]
            break
    s = s.rstrip("/")
    if "@" in s:                                  # user:pass@host:port → 丢弃认证部分
        s = s.rsplit("@", 1)[1]
    if s.count(":") == 1:
        h, p = s.split(":")
        try:
            return (h.strip(), int(p))
        except ValueError:
            return None
    return (s, 80) if s else None


def set_override(value):
    """main.py 解析 --proxy 后调用；也用于界面切换网络模式。

    取值：None / "auto"=自动读系统代理；"none"/"direct"=强制直连；
          "force"/"proxy"=强制走系统代理（忽略绕过列表）；host:port=指定代理。
    """
    global _override
    if value is None:
        _override = None
        return
    v = str(value).strip()
    low = v.lower()
    if low in ("", "auto", "system"):
        _override = None
    elif low in ("none", "off", "0", "direct", "no"):
        _override = "none"
    elif low in ("force", "proxy", "on", "always"):
        _override = "force"
    else:
        _override = _parse(v)


def current_mode():
    """当前模式：'direct' | 'proxy' | 'auto'。"""
    if _override == "none":
        return "direct"
    if _override == "force" or _override:
        return "proxy"
    return "auto"


def active_proxy():
    """对一般外网地址实际会使用的代理 (host, port)；None 表示直连。"""
    if _override == "none":
        return None
    if _override == "force":
        px, _ = _load_system()
        return px or _load_server_any()
    if _override:
        return _override
    px, _ = _load_system()
    return px


def describe():
    """给界面显示的一句话说明。"""
    mode = current_mode()
    if mode == "direct":
        return "直连（不走代理）"
    px = active_proxy()
    if px:
        tag = "（强制）" if (mode == "proxy" and _override == "force") else ""
        return f"代理 {px[0]}:{px[1]}{tag}"
    if mode == "proxy":
        return "代理（未找到代理地址，将直连）"
    return "自动（系统未启用代理，将直连）"


def _load_system():
    """返回 (proxy(h,p) 或 None, ProxyOverride 字符串)。带 5 秒缓存。"""
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL:
        return _cache["proxy"], _cache["bypass"]
    proxy, bypass = None, ""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enable = winreg.QueryValueEx(k, "ProxyEnable")[0]
        except OSError:
            enable = 0
        try:
            server = winreg.QueryValueEx(k, "ProxyServer")[0]
        except OSError:
            server = ""
        try:
            bypass = winreg.QueryValueEx(k, "ProxyOverride")[0] or ""
        except OSError:
            bypass = ""
        winreg.CloseKey(k)
        if enable and server:
            if "=" in server:                     # http=...;https=...;socks=... 形式
                d = {}
                for part in server.split(";"):
                    if "=" in part:
                        kk, vv = part.split("=", 1)
                        d[kk.strip().lower()] = vv.strip()
                server = d.get("https") or d.get("http") or ""   # 只有 socks= 时为空 → 直连
            proxy = _parse(server)
    except Exception:
        proxy = None
    _cache["at"], _cache["proxy"], _cache["bypass"] = now, proxy, bypass
    return proxy, bypass


def _load_server_any():
    """不管 ProxyEnable 开关，只要注册表里存过代理地址就取出来。

    用户手动选「代理」时应该尊重这个地址 —— 很多人关掉系统代理开关后，
    Clash 的 127.0.0.1:7890 仍留在注册表里。
    """
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            server = winreg.QueryValueEx(k, "ProxyServer")[0] or ""
        except OSError:
            server = ""
        winreg.CloseKey(k)
        if "=" in server:
            d = {}
            for part in server.split(";"):
                if "=" in part:
                    kk, vv = part.split("=", 1)
                    d[kk.strip().lower()] = vv.strip()
            server = d.get("https") or d.get("http") or ""
        return _parse(server)
    except Exception:
        return None


def _match_bypass(host, ov):
    if not ov:
        return False
    h = (host or "").lower()
    for p in ov.split(";"):
        p = p.strip().lower()
        if not p:
            continue
        if p == "<local>":
            if "." not in h:
                return True
            continue
        if fnmatch.fnmatchcase(h, p) or h == p:
            return True
    return False


def _is_local(host):
    h = (host or "").lower()
    return h in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or h.endswith(".local")


def proxy_for(host):
    """该目标主机应使用的 (host, port)；None 表示直连。"""
    if _override == "none":
        return None
    if _override == "force":                      # 强制代理：忽略绕过列表，但本地地址仍直连
        if _is_local(host):
            return None
        px, _ = _load_system()
        return px or _load_server_any()
    if _override:
        return _override
    if _is_local(host):
        return None
    px, bypass = _load_system()
    if not px:
        return None
    if _match_bypass(host, bypass):
        return None
    return px
