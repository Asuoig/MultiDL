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

# chunked（无 Content-Length）响应下，低于此字节数视为"假响应"（服务器反爬的空/极小 body）
MIN_SANE_CHUNKED = 4096


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

    # 只透传防盗链需要的 Referer / Cookie；绝不动 User-Agent（会覆盖指纹伪装，导致 403）
    hdrs = {}
    if task.headers:
        for k in ("Referer", "Cookie"):
            v = task.headers.get(k)
            if v:
                hdrs[k] = v

    # 第一阶段：原始请求
    r = creq.get(url, impersonate="chrome", proxies=proxies, stream=True, timeout=30,
                 headers=hdrs or None)
    ct = (r.headers.get("Content-Type") or "")
    is_verify_page = (r.status_code == 200 and "text/html" in ct
                      and "Verifying" in r.text[:500])

    try:
        if is_verify_page:
            # USTC 等镜像站对大文件/特殊扩展名返回 JS 验证页，
            # JS 会设置 addr=<IP> cookie，重请求才放行。
            # curl_cffi 无法执行 JS，所以手动模拟：提取 IP 设置 cookie 后重请求。
            r.close()
            on_change(task)
            task.message = "浏览器指纹兜底：等待验证…（正在通过指纹绕过）"
            import re
            m = re.search(r"addr=([^;\"']*)", r.text)
            if m:
                verify_cookie = f"addr={m.group(1)}"
            else:
                # 从 Set-Cookie 或页面提取
                m2 = re.search(r"([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", r.text)
                verify_cookie = f"addr={m2.group(1)}" if m2 else ""

            if verify_cookie:
                task.message = "浏览器指纹兜底：验证通过，下载中…"
                r2 = creq.get(url, impersonate="chrome", proxies=proxies, stream=True,
                              timeout=300, headers={**hdrs, "Cookie": verify_cookie} if hdrs else {"Cookie": verify_cookie})
                ct2 = r2.headers.get("Content-Type") or ""
                if "text/html" not in ct2:
                    r = r2
                else:
                    # 仍然被拦
                    r2.close()
                    return False
            else:
                return False

        if r.status_code not in (200, 206):
            return False

        ok, got, total = _stream(r, task, part, on_change)
        if not ok:
            return False                                 # 用户取消

        # 可疑响应：服务器给了空 body，或 chunked（长度未知）却只吐出极少字节
        # （USTC 等镜像站会用这种"空/1 字节响应"做反爬，若不识别会生成 1B 假文件）
        if got == 0 or (total == 0 and got < MIN_SANE_CHUNKED):
            r.close()
            on_change(task)
            task.message = "浏览器指纹兜底：响应异常，重试中…"
            time.sleep(1.2)
            if task.stop.is_set():
                return False
            r2 = creq.get(url, impersonate="chrome", proxies=proxies, stream=True,
                          timeout=300, headers=hdrs or None)
            ct2 = (r2.headers.get("Content-Type") or "")
            if r2.status_code not in (200, 206) or "text/html" in ct2:
                r2.close()
                return False
            r = r2
            ok, got, total = _stream(r, task, part, on_change)
            if not ok:
                return False

        if got == 0 or (total == 0 and got < MIN_SANE_CHUNKED):
            task.message = "镜像站拒绝访问（返回空响应）"
            return False                                 # 仍被拦 / 假响应
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


def _stream(r, task, part, on_change):
    """把响应流写入 part 文件。返回 (是否正常结束, 已写字节数, 声明总长)。"""
    total = int(r.headers.get("Content-Length") or 0)
    got = 0
    with open(part, "wb") as f:
        for chunk in r.iter_content(65536):
            if task.stop.is_set():
                return False, got, total
            if not chunk:
                continue
            f.write(chunk)
            got += len(chunk)
            task.size = total or got
            task.downloaded = got
            task.speed = got / max(time.monotonic() - (task._t0 or time.monotonic()), 0.01)
            on_change(task)
    return True, got, total
