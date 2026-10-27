# -*- coding: utf-8 -*-
"""Selenium 浏览器指纹下载器。用于绕过 JS 反爬、WAF 等检测。"""
import os
import sys
import time
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

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
    HAS_SELENIUM = False

try:
    from playwright.sync_api import sync_playwright, BrowserType, BrowserContext
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


class SeleniumDownloader:
    """基于 Selenium/Playwright 的真人浏览器下载器。"""

    def __init__(self):
        self.driver: Optional[webdriver.Chrome] = None
        self.waiter: Optional[WebDriverWait] = None
        self.context: Optional[BrowserContext] = None

    def start(self) -> bool:
        """启动浏览器驱动，返回是否成功。"""
        if not HAS_SELENIUM and not HAS_PLAYWRIGHT:
            logger.error("selenium 或 playwright 未安装，请执行：pip install selenium webdriver-manager playwright")
            return False

        try:
            if HAS_SELENIUM:
                self._start_selenium()
            elif HAS_PLAYWRIGHT:
                self._start_playwright()
            else:
                logger.error("selenium 和 playwright 都未安装，无法启动浏览器指纹模式")
                return False
            return True
        except Exception as e:
            logger.exception(f"启动浏览器失败：{e}")
            return False

    def _start_selenium(self) -> None:
        """Selenium 驱动启动逻辑（简化版，仅做真人模拟）"""
        options = ChromeOptions()
        # 关闭自动化标志，避免被反爬站点识别为机器人
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["ignore-gpu"])
        options.add_experimental_option("useAutomationControlled", "true")

        # 随机 User-Agent（模拟真人浏览器的 UA 池）
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        ]
        options.add_argument(f"user-agent={self._random_choice(user_agents)}")

        # 禁用不需要的扩展和硬件加速（减少指纹特征）
        options.add_experimental_option("excludeSwitches", ["ignore-gpu"])
        options.add_experimental_option("useAutomationControlled", "true")
        options.set_capability("goog:loggingPrefs", {"performance": "all"})

        # 禁用不必要的进程以降低资源占用
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["ignore-gpu"])

        service = ChromeService(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)

    def _start_playwright(self) -> None:
        """Playwright 浏览器上下文启动逻辑（简化版）"""
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=os.path.join(os.environ.get("TEMP", "C:\\Users\\Fang\\AppData\\Local"), "PlaywrightDownload"),
                headless=False,  # 非无头模式，模拟真人
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                ],
            )

    def _random_choice(self, items: list[str]) -> str:
        """从列表中随机选择一个元素。"""
        import random
        return random.choice(items) if items else ""

    def download(self, url: str, save_path: Optional[str] = None) -> bool:
        """使用浏览器下载文件，支持大文件分块读取。"""
        try:
            # 打开网页并等待内容加载（模拟真人浏览行为）
            self.driver.get(url)

            # 等待页面完成加载（部分站点需要 JS 渲染）
            WebDriverWait(self.driver, 10).until(
                ExpectedCondition.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # 简单策略：从网页的 <a> 标签中提取下载地址
            body = self.driver.find_element(By.TAG_NAME, "body")
            links = body.find_elements(By.TAG_NAME, "a")

            target_url = None
            for a in links:
                href = a.get_attribute("href") or ""
                # 简单启发式：找包含常见扩展名的链接（.exe, .iso, .zip, .7z 等）
                if any(ext in href.lower() for ext in [".exe", ".iso", ".zip", ".7z", ".tar.gz"]):
                    target_url = href
                    break

            # 如果没找到合适的链接，则尝试直接下载当前 URL（兜底）
            if not target_url:
                target_url = url

            # 使用 curl_cffi（已在 core.py 中定义）进行实际下载
            from curl_cffi import requests as creq
            response = creq.get(target_url, stream=True, timeout=60)
            total_size = int(response.headers.get("Content-Length", 0)) if "Content-Length" in response.headers else None

            # 分块写入文件，避免内存暴涨
            chunk_size = 1024 * 1024  # 1MB 一块
            start_pos = 0
            with open(save_path, "wb") as f:
                while True:
                    if total_size is not None and start_pos >= total_size:
                        break
                    chunk = response.stream(chunk_size)
                    if chunk:
                        f.write(chunk)
                    else:
                        break

            logger.info(f"Selenium 下载完成：{save_path} ({os.path.getsize(save_path)} bytes)")
            return True
        except Exception as e:
            logger.exception(f"浏览器下载失败：{e}")
            return False

    def stop(self) -> None:
        """关闭浏览器驱动。"""
        if self.driver is not None:
            try:
                self.driver.quit()
            finally:
                self.driver = None