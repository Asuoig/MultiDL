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

_override = None                                  # (host, port) | "none" | None
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
    """main.py 解析 --proxy 后调用。"""
    global _override
    if value is None:
        _override = None
        return
    v = str(value).strip()
    if v.lower() in ("", "none", "off", "0", "direct", "no"):
        _override = "none"
    else:
        _override = _parse(v)


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
