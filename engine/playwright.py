# -*- coding: utf-8 -*-
"""Playwright 浏览器指纹下载器。用于绕过 JS 反爬、WAF 等检测。"""
import os
import sys
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright, BrowserType, BrowserContext, Page
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


class PlaywrightDownloader:
    """基于 Playwright 的真人浏览器下载器。"""

    def __init__(self):
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def start(self) -> bool:
        """启动浏览器上下文，返回是否成功。"""
        if not HAS_PLAYWRIGHT:
            logger.error("playwright 未安装，请执行：pip install playwright && playwright install chromium")
            return False

        try:
            self.context = None
            with sync_playwright() as p:
                self._context = p.chromium.launch_persistent_context(
                    user_data_dir=os.path.join(os.environ.get("TEMP", "C:\\Users\\Fang\\AppData\\Local"), "PlaywrightDownload"),
                    headless=False,  # 非无头模式，模拟真人
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                    ],
                )
            self.context = self._context
            # 获取默认页面作为当前操作对象
            if self.context.pages:
                self.page = self.context.pages[0]
            else:
                self.page = self.context.new_page()
            return True
        except Exception as e:
            logger.exception(f"启动 Playwright 浏览器失败：{e}")
            return False

    def download(self, url: str, save_path: Optional[str] = None) -> bool:
        """使用浏览器下载文件，支持大文件分块读取。"""
        if not self.context or not self.page:
            logger.error("浏览器上下文未启动")
            return False

        try:
            # 打开页面并等待加载
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 简单启发式：从 <a> 标签找带文件扩展名的链接
            links = self.page.query_selector_all("a")
            target_url: Optional[str] = None
            for a in links:
                href = a.get_attribute("href") or ""
                if any(ext in href.lower() for ext in [".exe", ".iso", ".zip", ".7z", ".tar.gz"]):
                    target_url = href
                    break

            # 兜底：直接用当前 URL
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

            logger.info(f"Playwright 下载完成：{save_path} ({os.path.getsize(save_path)} bytes)")
            return True
        except Exception as e:
            logger.exception(f"浏览器下载失败：{e}")
            return False

    def stop(self) -> None:
        """关闭浏览器上下文。"""
        if self.context is not None:
            try:
                self.context.close()
            finally:
                self.context = None