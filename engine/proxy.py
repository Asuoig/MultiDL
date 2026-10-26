"""代理支持（纯标准库）。

优先级：
1. `--proxy host:port` 显式指定（`none`/`off` 表示强制直连）
2. Windows 系统代理（HKCU Internet Settings，Clash 等会写这里）
3. 都没有 → 直连

本地地址（localhost / 127.0.0.1 / ::1）永远直连，避免把本地 API/测试服务器也代理了。
"""
from __future__ import annotations

import sys
import time

_override = None            # (host, port) | "none" | None
_cache = {"at": 0.0, "val": None}
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
    if "@" in s:
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


def system_proxy():
    if sys.platform != "win32":
        return None
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL:
        return _cache["val"]
    val = None
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
        winreg.CloseKey(k)
        if enable and server:
            if "=" in server:                       # http=...;https=... 形式
                d = {}
                for part in server.split(";"):
                    if "=" in part:
                        kk, vv = part.split("=", 1)
                        d[kk.strip().lower()] = vv.strip()
                server = d.get("https") or d.get("http") or ""
            val = _parse(server)
    except Exception:
        val = None
    _cache["at"], _cache["val"] = now, val
    return val


def _is_local(host):
    h = (host or "").lower()
    return h in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or h.endswith(".local")


def proxy_for(host):
    """返回该目标主机应使用的 (host, port)，或 None 表示直连。"""
    if _override == "none":
        return None
    if _override:
        return _override
    if _is_local(host):
        return None
    return system_proxy()
