# -*- coding: utf-8 -*-
"""浏览器指纹模式下载器（反爬绕过）。

当 curl_cffi 的 impersonate="chrome" 仍被识别为机器人时，
切换到此模式：启动真实 Chrome/Chromium 进程模拟真人浏览行为。
依赖：selenium webdriver-manager playwright（均为可选依赖）
"""
import os
import sys
import time
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# --- Selenium + Playwright ---
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, ExpectedCondition
    from selenium.webdriver.support.expected_conditions import stale_element
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_SELENIUM = True
except ImportError:
    logger.warning("Selenium 驱动未安装，将降级使用 Playwright")
    HAS_SELENIUM = False

try:
    from playwright.sync_api import sync_playwright, BrowserType, BrowserContext
    HAS_PLAYWRIGHT = True
except ImportError:
    logger.warning("Playwright 未安装，将尝试使用 Selenium")
    HAS_PLAYWRIGHT = False


class BrowserDownloader:
    """浏览器指纹模式下载器。

    用于绕过基于指纹的反爬（JS 检测 webdriver、TLS JA3、WebGL 等）。
    底层仍用 curl_cffi 进行实际下载，避免浏览器内存暴涨。
    """

    def __init__(self):
        self.driver: Optional[webdriver.Chrome] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional["Page"] = None
        self._started: bool = False
        self._lock: object = object()

    @property
    def started(self) -> bool:
        return self._started and (self.driver is not None or self.context is not None)

    def start(self, timeout: int = 120) -> bool:
        """启动浏览器引擎。返回是否成功。"""
        if HAS_SELENIUM:
            return self._start_selenium(timeout)
        elif HAS_PLAYWRIGHT:
            return self._start_playwright(timeout)
        else:
            logger.critical("selenium 和 playwright 都未安装，无法启动浏览器指纹模式")
            return False

    def _start_selenium(self, timeout: int = 120) -> bool:
        """Selenium 驱动启动逻辑。"""
        try:
            options = ChromeOptions()
            # 真人模拟：关闭自动化标志、随机 UA
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["ignore-gpu"])
            options.add_experimental_option("useAutomationControlled", "true")

            user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            ]
            import random
            options.add_argument(f"user-agent={random.choice(user_agents)}")

            # 禁用不必要的扩展和硬件加速
            options.add_experimental_option("excludeSwitches", ["ignore-gpu"])
            options.set_capability("goog:loggingPrefs", {"performance": "all"})

            service = ChromeService(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
            logger.info("Selenium 驱动启动成功")
            return True
        except Exception as e:
            logger.exception(f"Selenium 驱动启动失败：{e}")
            return False

    def _start_playwright(self, timeout: int = 120) -> bool:
        """Playwright 浏览器上下文启动逻辑。"""
        try:
            with sync_playwright() as p:
                self.context = p.chromium.launch_persistent_context(
                    user_data_dir=os.path.join(os.environ.get("TEMP", "C:\\Users\\Fang\\AppData\\Local"), "PlaywrightDownload"),
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                    ],
                )
            self.page = self.context.pages[0] if self.context.pages else None
            logger.info("Playwright 浏览器上下文启动成功")
            return True
        except Exception as e:
            logger.exception(f"Playwright 浏览器上下文启动失败：{e}")
            return False

    def _random_choice(self, items: list[str]) -> str:
        import random
        return random.choice(items) if items else ""

    def download(self, url: str, save_path: Optional[str] = None) -> bool:
        """使用浏览器下载文件（分块写入，避免内存暴涨）。"""
        try:
            from curl_cffi import requests as creq  # type: ignore[import-not-found]

            # Selenium: 打开页面并等待渲染
            if self.driver is not None and HAS_SELENIUM:
                self.driver.get(url)
                WebDriverWait(self.driver, 10).until(
                    ExpectedCondition.presence_of_element_located((By.TAG_NAME, "body"))
                )
                body = self.driver.find_element(By.TAG_NAME, "body")
                links = body.find_elements(By.TAG_NAME, "a")
                for a in links:
                    href = a.get_attribute("href") or ""
                    if any(ext in href.lower() for ext in [".exe", ".iso", ".zip", ".7z", ".tar.gz"]):
                        url = href
                        break

            # Playwright: 从 <a> 标签找下载链接
            elif self.page is not None and HAS_PLAYWRIGHT:
                links = self.page.query_selector_all("a")
                for a in links:
                    href = a.get_attribute("href") or ""
                    if any(ext in href.lower() for ext in [".exe", ".iso", ".zip", ".7z", ".tar.gz"]):
                        url = href
                        break

            # 兜底：直接用原始 URL
            if not url:
                url = save_path.split("://")[-1] if save_path else "https://" + os.path.basename(save_path)

            logger.info(f"开始下载：{url[:60]}...")

            response = creq.get(url, stream=True, timeout=60)
            total_size = int(response.headers.get("Content-Length", 0)) if "Content-Length" in response.headers else None
            chunk_size = 1024 * 1024  # 1MB 一块，避免内存暴涨

            with open(save_path, "wb") as f:
                downloaded = 0
                while True:
                    if total_size is not None and downloaded >= total_size:
                        break
                    chunk = response.stream(chunk_size)
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                    else:
                        break

            logger.info(f"下载完成：{save_path} ({os.path.getsize(save_path)} bytes)")
            return True
        except Exception as e:
            logger.exception(f"浏览器下载失败：{e}")
            return False

    def stop(self) -> None:
        """关闭浏览器驱动。"""
        if self.driver is not None and HAS_SELENIUM:
            try:
                self.driver.quit()
            finally:
                self.driver = None
        if self.context is not None and HAS_PLAYWRIGHT:
            try:
                self.context.close()
            finally:
                self.context = None


# --- 统一接口：兼容 MultiDL 引擎的调用风格 ---

from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class EngineDownloader:
    """统一的浏览器指纹模式下载器。支持切换 Selenium / Playwright。"""

    def __init__(self):
        self._selenium = SeleniumDownloader() if HAS_SELENIUM else None
        self._playwright = PlaywrightDownloader() if HAS_PLAYWRIGHT else None
        self._browser: Optional[BrowserDownloader] = None  # 内部统一用 BrowserDownloader

    def start(self) -> bool:
        return self._browser.start() if self._browser is not None else False

    def stop(self) -> None:
        pass

    def download(self, url: str, save_path: Optional[str] = None) -> Tuple[bool, str]:
        """下载文件。返回 (是否成功，原因)。"""
        from curl_cffi import requests as creq  # type: ignore[import-not-found]
        try:
            response = creq.get(url, stream=True, timeout=60)
            total_size = int(response.headers.get("Content-Length", 0)) if "Content-Length" in response.headers else None
            chunk_size = 1024 * 1024
            with open(save_path, "wb") as f:
                downloaded = 0
                while True:
                    if total_size is not None and downloaded >= total_size:
                        break
                    chunk = response.stream(chunk_size)
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                    else:
                        break
            return True, "浏览器指纹模式下载成功"
        except Exception as e:
            logger.exception(f"下载失败：{e}")
            return False, str(e)


# 默认导入
Downloader = EngineDownloader
