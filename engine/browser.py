# -*- coding: utf-8 -*-
"""浏览器指纹模式下载器（反爬绕过）。

当 curl_cffi 的 impersonate="chrome" 仍被识别为机器人时，可切换到此模式：
启动真实 Chrome / Chromium 进程，先由浏览器完成「JS 挑战 / 跳转 / Cookie 颁发」，
再用 curl_cffi（带浏览器会话 Cookie + 指纹）拉取正文，避免浏览器内存暴涨。

依赖（均为可选，未安装时本模块会优雅降级并报原因，不会崩溃）：
    pip install selenium webdriver-manager playwright
    playwright install chromium
"""
import os
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
        return ("未安装浏览器指纹依赖。请先执行：\n"
                "  pip install selenium webdriver-manager playwright\n"
                "  playwright install chromium\n"
                "安装后即可在「选项 → 网络模式」启用「浏览器指纹（反爬）」。")

    # ---- 生命周期 ----
    def start(self) -> Tuple[bool, str]:
        """启动浏览器后端。返回 (是否成功, 原因/后端名)。"""
        if not self.available():
            return False, self.missing_reason()
        if self.backend is not None:
            return True, self.backend
        # 优先 Selenium（headless=False 模拟真人），失败回退 Playwright
        if HAS_SELENIUM:
            try:
                self._start_selenium()
                self.backend = "selenium"
                logger.info("浏览器指纹模式：Selenium 后端已启动")
                return True, "selenium"
            except Exception as e:                       # noqa: BLE001
                logger.warning("Selenium 启动失败，回退 Playwright：%s", e)
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
        return False, "无可用浏览器后端"

    def _start_selenium(self):
        options = ChromeOptions()
        # 关闭自动化特征：这是反爬识别 webdriver 的关键开关
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument(f"user-agent={random.choice(_USER_AGENTS)}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        service = ChromeService(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)

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
