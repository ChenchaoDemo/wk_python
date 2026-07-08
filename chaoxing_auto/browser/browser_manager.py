"""浏览器生命周期管理模块。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.sync_api import Page, Playwright, sync_playwright

from config.config import AUTH_FILE, HEADLESS, SLOW_MO, WAIT_TIME, VIEWPORT_HEIGHT, VIEWPORT_WIDTH
from utils.logger import get_logger

logger = get_logger()


class BrowserManager:
    """负责启动、创建上下文、创建页面和关闭浏览器。"""

    def __init__(
        self,
        headless: bool = HEADLESS,
        storage_state_path: Path = AUTH_FILE,
        slow_mo: int = SLOW_MO,
    ) -> None:
        self.headless = headless
        self.storage_state_path = Path(storage_state_path)
        self.slow_mo = slow_mo
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def start_browser(self) -> Browser:
        """启动 Chromium 浏览器。"""

        try:
            logger.info("启动浏览器，headless=%s", self.headless)
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
            return self.browser
        except Exception as exc:
            logger.exception("浏览器启动失败: %s", exc)
            self.close()
            raise

    def create_context(self) -> BrowserContext:
        """创建浏览器上下文，并自动加载 auth.json 登录状态。"""

        if self.browser is None:
            self.start_browser()

        assert self.browser is not None

        context_kwargs = {
            "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "accept_downloads": True,
            "ignore_https_errors": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

        if self.storage_state_path.exists():
            logger.info("加载登录状态: %s", self.storage_state_path)
            context_kwargs["storage_state"] = str(self.storage_state_path)
        else:
            logger.info("未发现登录状态文件，首次运行将在登录后保存 auth.json")

        try:
            self.context = self.browser.new_context(**context_kwargs)
            self.context.set_default_timeout(WAIT_TIME)
            self.context.set_default_navigation_timeout(WAIT_TIME)
            return self.context
        except Exception as exc:
            logger.exception("创建浏览器上下文失败: %s", exc)
            raise

    def new_page(self) -> Page:
        """创建新页面。"""

        if self.context is None:
            self.create_context()
        assert self.context is not None

        try:
            self.page = self.context.new_page()
            self.page.set_default_timeout(WAIT_TIME)
            self.page.set_default_navigation_timeout(WAIT_TIME)
            logger.info("已创建新页面")
            return self.page
        except Exception as exc:
            logger.exception("创建页面失败: %s", exc)
            raise

    def save_storage_state(self) -> None:
        """保存当前登录状态到 auth.json。"""

        if self.context is None:
            logger.warning("浏览器上下文不存在，无法保存登录状态")
            return

        try:
            self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.context.storage_state(path=str(self.storage_state_path))
            logger.info("登录状态已保存: %s", self.storage_state_path)
        except PlaywrightError as exc:
            logger.exception("保存登录状态失败: %s", exc)

    def close(self) -> None:
        """关闭页面、上下文、浏览器和 Playwright。"""

        logger.info("关闭浏览器资源")
        for resource_name in ("page", "context", "browser"):
            resource = getattr(self, resource_name, None)
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:  # noqa: BLE001 - 关闭流程需要尽最大努力释放资源
                logger.warning("关闭 %s 失败: %s", resource_name, exc)
            finally:
                setattr(self, resource_name, None)

        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止 Playwright 失败: %s", exc)
            finally:
                self.playwright = None

    def __enter__(self) -> "BrowserManager":
        self.start_browser()
        self.create_context()
        self.new_page()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        self.close()
