"""浏览器生命周期管理模块。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.sync_api import Page, Playwright, sync_playwright

from config.config import (
    AUTH_FILE,
    BROWSER_CHANNEL,
    BROWSER_USER_AGENT,
    HEADLESS,
    SLOW_MO,
    WAIT_TIME,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
)
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
        self._cdp_sessions: list[Any] = []
        self._debugger_disabled_page_ids: set[int] = set()

    def start_browser(self) -> Browser:
        """启动浏览器。

        默认优先使用本机 Chrome，而不是 Playwright 自带 Chromium。
        这样学习通返回的页面通常会和用户手动打开网页版时更一致。
        """

        try:
            logger.info("启动浏览器，headless=%s channel=%s", self.headless, BROWSER_CHANNEL or "playwright-chromium")
            self.playwright = sync_playwright().start()
            launch_kwargs = {
                "headless": self.headless,
                "slow_mo": self.slow_mo,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                    "--disable-infobars",
                    "--lang=zh-CN",
                ],
            }
            if BROWSER_CHANNEL:
                launch_kwargs["channel"] = BROWSER_CHANNEL

            try:
                self.browser = self.playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:
                if not BROWSER_CHANNEL:
                    raise
                logger.warning("使用本机 Chrome 启动失败，回退到 Playwright Chromium: %s", exc)
                launch_kwargs.pop("channel", None)
                self.browser = self.playwright.chromium.launch(**launch_kwargs)
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
            # 有界面模式下不要固定成移动/小窗口视口，让页面更接近用户手动打开 Chrome 的效果。
            "viewport": None if not self.headless else {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "accept_downloads": True,
            "ignore_https_errors": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "extra_http_headers": {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }
        if BROWSER_USER_AGENT:
            context_kwargs["user_agent"] = BROWSER_USER_AGENT

        if self.storage_state_path.exists():
            logger.info("加载登录状态: %s", self.storage_state_path)
            context_kwargs["storage_state"] = str(self.storage_state_path)
        else:
            logger.info("未发现登录状态文件，首次运行将在登录后保存 auth.json")

        try:
            self.context = self.browser.new_context(**context_kwargs)
            self.context.set_default_timeout(WAIT_TIME)
            self.context.set_default_navigation_timeout(WAIT_TIME)
            self.context.on("page", self._disable_debugger_pause)
            self.context.add_init_script(
                """
                (() => {
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined,
                    });

                    // 学习通部分测验页会通过 Function/eval/setInterval 注入 `debugger`，
                    // 打开开发者工具时会反复暂停。这里尽量把动态字符串里的 debugger 去掉，
                    // Playwright 自身读取 DOM/监听接口不再依赖手动 F12。
                    const stripDebugger = (value) => {
                        if (typeof value !== 'string') return value;
                        return value.replace(/\\bdebugger\\s*;?/g, '');
                    };

                    try {
                        const NativeFunction = window.Function;
                        window.Function = new Proxy(NativeFunction, {
                            apply(target, thisArg, args) {
                                return Reflect.apply(target, thisArg, Array.from(args, stripDebugger));
                            },
                            construct(target, args) {
                                return Reflect.construct(target, Array.from(args, stripDebugger), target);
                            },
                        });
                    } catch (e) {}

                    try {
                        const nativeEval = window.eval;
                        window.eval = new Proxy(nativeEval, {
                            apply(target, thisArg, args) {
                                return Reflect.apply(target, thisArg, Array.from(args, stripDebugger));
                            },
                        });
                    } catch (e) {}

                    ['setTimeout', 'setInterval'].forEach((name) => {
                        try {
                            const nativeTimer = window[name];
                            window[name] = new Proxy(nativeTimer, {
                                apply(target, thisArg, args) {
                                    if (typeof args[0] === 'string') {
                                        args = [stripDebugger(args[0]), ...Array.from(args).slice(1)];
                                    }
                                    return Reflect.apply(target, thisArg, args);
                                },
                            });
                        } catch (e) {}
                    });
                })();
                """
            )
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
            self._disable_debugger_pause(self.page)
            logger.info("已创建新页面")
            return self.page
        except Exception as exc:
            logger.exception("创建页面失败: %s", exc)
            raise

    def _disable_debugger_pause(self, page: Page) -> None:
        """通过 CDP 跳过 debugger 暂停，便于调试测验页结构。"""

        if self.context is None:
            return
        page_id = id(page)
        if page_id in self._debugger_disabled_page_ids:
            return

        try:
            session = self.context.new_cdp_session(page)
            session.send("Debugger.enable")
            session.send("Debugger.setSkipAllPauses", {"skip": True})
            self._cdp_sessions.append(session)
            self._debugger_disabled_page_ids.add(page_id)
            logger.info("已启用 debugger 跳过暂停")
        except Exception as exc:  # noqa: BLE001 - 非 Chromium 或 CDP 不可用时不影响主流程
            logger.debug("启用 debugger 跳过暂停失败，继续运行: %s", exc)

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
