"""curl_cffi 兜底下载（可选依赖）。

原生引擎失败（403 / 空响应 / 被墙）时，用 curl_cffi 的 Chrome TLS 指纹再试：
- 先走代理（如果配了），再走直连；
- 成功则写文件并把任务标记为完成；
- curl_cffi 未安装则直接返回 False（不影响主程序）。

单连接顺序下载（不分段），只作为"能下就行"的兜底。
"""
from __future__ import annotations

import os
import time
import urllib.parse as up


def curl_cffi_download(task, on_change) -> bool:
    try:
        from curl_cffi import requests as creq          # noqa: F401
    except Exception:
        return False                                     # 没装 → 跳过兜底

    from .proxy import proxy_for

    host = up.urlsplit(task.final_url or task.url).hostname
    px = proxy_for(host)
    attempts = []
    if px:
        p = f"http://{px[0]}:{px[1]}"
        attempts.append({"http": p, "https": p})
    attempts.append(None)                                # 直连

    for proxies in attempts:
        if task.stop.is_set():
            return False
        try:
            if _one(task, creq, proxies, on_change):
                return True
        except Exception:
            continue
    return False


def _one(task, creq, proxies, on_change) -> bool:
    url = task.final_url or task.url
    part = task.part_path
    os.makedirs(os.path.dirname(os.path.abspath(task.target)) or ".", exist_ok=True)

    task.state = "running"
    task.message = "浏览器指纹兜底下载中…"
    task._t0 = time.monotonic()
    task._bytes = 0
    task.speed = 0.0
    on_change(task)

    r = creq.get(url, impersonate="chrome", proxies=proxies, stream=True, timeout=30,
                 headers={"User-Agent": task.ua or "Mozilla/5.0"})
    try:
        if r.status_code not in (200, 206):
            return False
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(65536):
                if task.stop.is_set():
                    return False
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                task.size = total or got
                task.downloaded = got
                task.speed = got / max(time.monotonic() - task._t0, 0.01)
                on_change(task)
        if got == 0:
            return False                                 # 空响应 → 仍被拦
        if total and got < total:
            return False
        os.replace(part, task.target)
        task.chunks = []                                 # 兜底是整文件，清掉分块信息
        task.state = "done"
        task.message = "已完成（浏览器指纹兜底）"
        task.speed = 0.0
        on_change(task)
        return True
    finally:
        try:
            r.close()
        except Exception:
            pass
