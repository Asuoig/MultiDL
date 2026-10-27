# -*- coding: utf-8 -*-
"""浏览器指纹模式下载器（反爬绕过）。

当 curl_cffi 的 impersonate="chrome" 仍被识别为机器人时，可切换到此模式：
启动真实 Chrome 进程，先由浏览器完成「JS 挑战 / 跳转 / Cookie 颁发」，
再把直链 / Cookie / UA 交给下载引擎（正文仍走多线程分块）。

chromedriver 解析顺序（全部失败才报错）：
    1. 环境变量 MULTIDL_CHROMEDRIVER / CHROMEDRIVER
    2. 内置 drivers/chromedriver.exe（打包进 exe，免联网）
    3. Selenium Manager（selenium 自带，按已装 Chrome 版本自动匹配）
    4. webdriver_manager（最后兜底，需要联网）

可选依赖（未安装时本模块优雅降级并报原因，不会崩溃）：
    pip install selenium webdriver-manager
"""
import os
import sys
import random
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_SELENIUM = True
except Exception:                               # noqa: BLE001
    HAS_SELENIUM = False

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                             # noqa: BLE001
    HAS_PLAYWRIGHT = False


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]
_FILE_EXTS = (".exe", ".iso", ".zip", ".7z", ".tar.gz", ".tar", ".gz", ".dmg", ".deb", ".rpm")


def bundled_driver() -> Optional[str]:
    """找随程序分发的 chromedriver（打包后在 _MEIPASS/drivers，源码运行在 multidl/drivers）。"""
    for env in ("MULTIDL_CHROMEDRIVER", "CHROMEDRIVER"):
        p = os.environ.get(env)
        if p and os.path.exists(p):
            return p
    roots = []
    base = getattr(sys, "_MEIPASS", None)          # PyInstaller 解包目录
    if base:
        roots.append(base)
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for r in roots:
        p = os.path.join(r, "drivers", "chromedriver.exe")
        if os.path.exists(p):
            return p
    return None


def _bypass_proxy_for_localhost():
    """把 127.0.0.1 / localhost 加进 NO_PROXY。

    Selenium 的 HTTP 客户端走 urllib3，会读 HTTP_PROXY 环境变量；一旦系统里设了代理
    （Clash / 公司网关 / 沙箱注入），连本机 chromedriver 的请求也会被转发到代理，
    表现为所有命令都返回 `Message: unhandled request`。这是很隐蔽的坑。
    """
    hosts = ("127.0.0.1", "localhost", "::1")
    for key in ("NO_PROXY", "no_proxy"):
        parts = [p.strip() for p in os.environ.get(key, "").split(",") if p.strip()]
        for h in hosts:
            if h not in parts:
                parts.append(h)
        os.environ[key] = ",".join(parts)


def _user_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "MultiDL", "pw_profile")
    os.makedirs(d, exist_ok=True)
    return d


class BrowserDownloader:
    """浏览器指纹模式下载器（单例式，按需启动）。"""

    def __init__(self):
        self.driver = None          # selenium webdriver
        self._pw = None             # playwright driver（手动生命周期）
        self.context = None         # playwright context
        self.page = None            # playwright page
        self.backend: Optional[str] = None   # "selenium" | "playwright" | None

    # ---- 状态查询 ----
    def available(self) -> bool:
        return HAS_SELENIUM or HAS_PLAYWRIGHT

    def missing_reason(self) -> str:
        return ("缺少浏览器指纹依赖（selenium）。官方 exe 已内置，源码运行请执行：\n"
                "  pip install selenium webdriver-manager\n"
                "并确认本机已安装 Chrome / Edge。")

    # ---- 生命周期 ----
    def start(self) -> Tuple[bool, str]:
        """启动浏览器后端。返回 (是否成功, 原因/后端名)。"""
        if not self.available():
            return False, self.missing_reason()
        if self.backend is not None:
            return True, self.backend
        _bypass_proxy_for_localhost()
        # 优先 Selenium（非 headless，用本机真实 Chrome），失败回退 Playwright
        err = None
        if HAS_SELENIUM:
            try:
                self._start_selenium()
                self._stealth()
                self.backend = "selenium"
                logger.info("浏览器指纹模式：Selenium 后端已启动")
                return True, "selenium"
            except Exception as e:                       # noqa: BLE001
                err = e
                logger.warning("Selenium 启动失败，尝试 Playwright：%s", e)
                self._close()
        if HAS_PLAYWRIGHT:
            try:
                self._start_playwright()
                self.backend = "playwright"
                logger.info("浏览器指纹模式：Playwright 后端已启动")
                return True, "playwright"
            except Exception as e:                       # noqa: BLE001
                logger.exception("Playwright 启动失败：%s", e)
                self._close()
                return False, f"Playwright 启动失败：{e}"
        if err is not None:
            return False, f"Chrome 启动失败：{err}"
        return False, "无可用浏览器后端"

    def _start_selenium(self):
        options = ChromeOptions()
        # 关闭自动化特征：这是反爬识别 webdriver 的关键开关
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        # 注意：不要设 --user-agent。真实 Chrome 的 UA 才是最可信的指纹，
        # 硬编码一个旧版本 UA 反而制造「UA 与 Client Hints / TLS 不一致」的破绽。
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        last = None
        # 1) 内置 chromedriver（免联网，最快）
        path = bundled_driver()
        if path:
            try:
                self.driver = webdriver.Chrome(
                    service=ChromeService(executable_path=path), options=options)
                logger.info("使用内置 chromedriver：%s", path)
                return
            except Exception as e:                   # noqa: BLE001
                last = e
                logger.warning("内置 chromedriver 启动失败（可能与本机 Chrome 版本不匹配）：%s", e)
        # 2) Selenium Manager：按已装 Chrome 自动匹配驱动
        try:
            self.driver = webdriver.Chrome(options=options)
            logger.info("使用 Selenium Manager 自动匹配的 chromedriver")
            return
        except Exception as e:                       # noqa: BLE001
            last = e
            logger.warning("Selenium Manager 启动失败：%s", e)
        # 3) webdriver_manager 兜底（需联网）
        try:
            service = ChromeService(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
            logger.info("使用 webdriver_manager 下载的 chromedriver")
            return
        except Exception as e:                       # noqa: BLE001
            last = e
            logger.warning("webdriver_manager 启动失败：%s", e)
        raise RuntimeError(f"无法启动 Chrome：{last}")

    def _stealth(self):
        """抹掉最容易被检测的 navigator.webdriver（CDP 注入，对所有页面生效）。"""
        try:
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator,'webdriver',"
                          "{get:()=>undefined});"
            })
        except Exception as e:                       # noqa: BLE001
            logger.debug("stealth 注入失败（不影响使用）：%s", e)

    def _start_playwright(self):
        # 手动管理生命周期（不能用 with 块，否则退出即关闭）
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=_user_data_dir(),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self.page = self.context.new_page() if not self.context.pages else self.context.pages[0]

    def _close(self):
        try:
            if self.driver is not None:
                self.driver.quit()
        except Exception:
            pass
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self.driver = self.context = self.page = self._pw = None
        self.backend = None

    def stop(self):
        self._close()

    # ---- 核心：先用浏览器过反爬，拿到真实直链/Cookie，再 curl_cffi 拉正文 ----
    def _resolve(self, url: str) -> str:
        """用浏览器打开页面，尝试提取真实文件直链。失败返回原 url。"""
        try:
            if self.backend == "selenium" and self.driver is not None:
                self.driver.get(url)
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body")))
                for a in self.driver.find_elements(By.TAG_NAME, "a"):
                    href = a.get_attribute("href") or ""
                    if href.lower().endswith(_FILE_EXTS):
                        return href
            elif self.backend == "playwright" and self.page is not None:
                self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                for a in self.page.query_selector_all("a"):
                    href = a.get_attribute("href") or ""
                    if href.lower().endswith(_FILE_EXTS):
                        return href
        except Exception as e:                           # noqa: BLE001
            logger.warning("浏览器解析直链失败，退回原 url：%s", e)
        return url

    def _cookies(self) -> list:
        """把浏览器会话里的 Cookie 导出给 curl_cffi 用（name=value 列表）。"""
        try:
            if self.backend == "selenium" and self.driver is not None:
                return [f"{c['name']}={c['value']}" for c in self.driver.get_cookies()]
            if self.backend == "playwright" and self.context is not None:
                out = []
                for c in self.context.cookies():
                    out.append(f"{c['name']}={c['value']}")
                return out
        except Exception:                               # noqa: BLE001
            pass
        return []

    # ---- 供下载引擎（engine/core.py 的 Task）调用 ----
    def resolve_url(self, url: str) -> str:
        """打开页面并尝试解析真实文件直链；解析不到返回原 url。"""
        return self._resolve(url)

    def cookies_header(self) -> str:
        """浏览器会话 Cookie 拼成请求头值；无 Cookie 返回空串。"""
        return "; ".join(self._cookies())

    def user_agent(self) -> str:
        """浏览器实际使用的 UA（让后续 HTTP 请求与 Cookie 归属保持一致）。"""
        try:
            if self.backend == "selenium" and self.driver is not None:
                return self.driver.execute_script("return navigator.userAgent") or ""
            if self.backend == "playwright" and self.page is not None:
                return self.page.evaluate("navigator.userAgent") or ""
        except Exception:                               # noqa: BLE001
            pass
        return ""

    def download(self, url: str, save_path: str) -> Tuple[bool, str]:
        """下载文件。返回 (是否成功, 原因)。"""
        if not self.available():
            return False, self.missing_reason()
        ok, why = self.start()
        if not ok:
            return False, why
        try:
            from curl_cffi import requests as creq      # type: ignore
            real_url = self._resolve(url)
            cookies = "; ".join(self._cookies())
            headers = {"User-Agent": random.choice(_USER_AGENTS)}
            if cookies:
                headers["Cookie"] = cookies
            logger.info("浏览器指纹模式下载：%s", real_url[:80])
            with creq.get(real_url, headers=headers, stream=True,
                          impersonate="chrome", timeout=60) as r:
                total = int(r.headers.get("Content-Length", 0) or 0)
                chunk = 1024 * 1024
                written = 0
                with open(save_path, "wb") as f:
                    for data in r.iter_content(chunk):
                        if not data:
                            break
                        f.write(data)
                        written += len(data)
                        if total and written >= total:
                            break
            logger.info("下载完成：%s（%d 字节）", save_path, written)
            return True, "ok"
        except Exception as e:                          # noqa: BLE001
            logger.exception("浏览器指纹模式下载失败：%s", e)
            return False, f"下载失败：{e}"


# 进程内共享一个实例（浏览器只启动一次）
_downloader: Optional[BrowserDownloader] = None


def get_downloader() -> BrowserDownloader:
    global _downloader
    if _downloader is None:
        _downloader = BrowserDownloader()
    return _downloader


# ---- 网络模式开关：由 Manager 在切换「浏览器指纹（反爬）」时置位 ----
# 用独立标志而不是读 proxy 模块，是因为 browser 模式在代理层等价于「直连」，
# 两者从 proxy.current_mode() 上无法区分。
_enabled = False


def set_enabled(on: bool):
    global _enabled
    _enabled = bool(on)


def is_enabled() -> bool:
    return _enabled


def prepare_task(task) -> Tuple[bool, str]:
    """最小接入：用真实浏览器过反爬，把直链 / Cookie / UA 写回 Task。

    只做「准备工作」，正文仍由 Task 的多线程分块流程下载。
    返回 (是否成功, 说明)；失败时调用方继续走常规 HTTP，不中断下载。
    """
    dl = get_downloader()
    if not dl.available():
        return False, dl.missing_reason()
    ok, why = dl.start()
    if not ok:
        return False, why
    url = task.url
    real = dl.resolve_url(url)
    if real and real != url:
        task.url = real
    cookie = dl.cookies_header()
    if cookie:
        task.headers["Cookie"] = cookie
    ua = dl.user_agent()
    if ua:
        task.ua = ua
        task.headers["User-Agent"] = ua
    detail = "直链已替换" if real != url else "直链未变（按原地址下载）"
    return True, f"{why} 后端已就绪，{detail}，Cookie {len(cookie)} 字符"
