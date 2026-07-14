"""????????????"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.sync_api import Page, Playwright, sync_playwright

from config.config import (
    AUTH_FILE,
    BROWSER_CHANNEL,
    BROWSER_USER_AGENT,
    HEADLESS,
    QR_STATUS_FIRST_BODY,
    QR_STATUS_INTERCEPT_ENABLED,
    QR_STATUS_SUCCESS_BODY,
    SLOW_MO,
    WAIT_TIME,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
)
from utils.logger import get_logger

logger = get_logger()


class BrowserManager:
    """??????????????????????

    ??????? browser + storage_state???? user_data_dir ????
    Chromium persistent context?????????? Chrome Profile?
    """

    def __init__(
        self,
        headless: bool = HEADLESS,
        storage_state_path: Path = AUTH_FILE,
        slow_mo: int = SLOW_MO,
        user_data_dir: Optional[Path] = None,
    ) -> None:
        self.headless = headless
        self.storage_state_path = Path(storage_state_path)
        self.slow_mo = slow_mo
        self.user_data_dir = Path(user_data_dir) if user_data_dir is not None else None
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._cdp_sessions: list[Any] = []
        self._debugger_disabled_page_ids: set[int] = set()
        self._qr_status_request_counts: dict[str, int] = {}

    def _launch_kwargs(self) -> dict[str, Any]:
        launch_kwargs: dict[str, Any] = {
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
        return launch_kwargs

    def _context_kwargs(self, *, load_storage_state: bool) -> dict[str, Any]:
        context_kwargs: dict[str, Any] = {
            # ????????????????????????? Chrome ????
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

        if load_storage_state:
            if self.storage_state_path.exists():
                logger.info("??????: %s", self.storage_state_path)
                context_kwargs["storage_state"] = str(self.storage_state_path)
            else:
                logger.info("????????????????????? auth.json")
        return context_kwargs

    def start_browser(self) -> Optional[Browser]:
        """??????

        ?? user_data_dir ????? persistent context????????? browser?
        ??? create_context() ????????
        """

        try:
            logger.info(
                "??????headless=%s channel=%s profile=%s",
                self.headless,
                BROWSER_CHANNEL or "playwright-chromium",
                self.user_data_dir or "storage_state",
            )
            self.playwright = sync_playwright().start()

            if self.user_data_dir is not None:
                self.user_data_dir.mkdir(parents=True, exist_ok=True)
                persistent_kwargs = self._launch_kwargs()
                persistent_kwargs.update(self._context_kwargs(load_storage_state=False))
                try:
                    self.context = self.playwright.chromium.launch_persistent_context(
                        str(self.user_data_dir),
                        **persistent_kwargs,
                    )
                except Exception as exc:
                    if not BROWSER_CHANNEL:
                        raise
                    logger.warning("???? Chrome ??? Profile ???????? Playwright Chromium: %s", exc)
                    persistent_kwargs.pop("channel", None)
                    self.context = self.playwright.chromium.launch_persistent_context(
                        str(self.user_data_dir),
                        **persistent_kwargs,
                    )
                self.browser = self.context.browser
                self._configure_context(self.context)
                return self.browser

            launch_kwargs = self._launch_kwargs()
            try:
                self.browser = self.playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:
                if not BROWSER_CHANNEL:
                    raise
                logger.warning("???? Chrome ???????? Playwright Chromium: %s", exc)
                launch_kwargs.pop("channel", None)
                self.browser = self.playwright.chromium.launch(**launch_kwargs)
            return self.browser
        except Exception as exc:
            logger.exception("???????: %s", exc)
            self.close()
            raise

    def create_context(self) -> BrowserContext:
        """?????????????? auth.json ?????"""

        if self.context is not None:
            return self.context

        if self.browser is None:
            self.start_browser()

        if self.context is not None:
            return self.context

        assert self.browser is not None

        try:
            self.context = self.browser.new_context(**self._context_kwargs(load_storage_state=True))
            self._configure_context(self.context)
            return self.context
        except Exception as exc:
            logger.exception("??????????: %s", exc)
            raise

    def _configure_context(self, context: BrowserContext) -> None:
        """????????????????? debugger ???"""

        context.set_default_timeout(WAIT_TIME)
        context.set_default_navigation_timeout(WAIT_TIME)
        if QR_STATUS_INTERCEPT_ENABLED:
            context.route("**/mooc-ans/qr/getqrstatus**", self._handle_qr_status_route)
            logger.info("已启用二维码状态接口拦截: /mooc-ans/qr/getqrstatus")
        context.on("page", self._disable_debugger_pause)
        context.add_init_script(
            """
            (() => {
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });

                // ??????????? Function/eval/setInterval ?? `debugger`?
                // ?????????????????????????? debugger ???
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
        for page in context.pages:
            self._disable_debugger_pause(page)

    def _handle_qr_status_route(self, route: Any) -> None:
        """拦截二维码状态轮询接口，直接返回成功响应。"""

        request = route.request
        url = request.url or ""
        try:
            parsed = urlsplit(url)
            if "/mooc-ans/qr/getqrstatus" not in parsed.path:
                route.continue_()
                return

            query = parse_qs(parsed.query)
            qr_key = query.get("uuid", [url])[0] or url
            request_count = self._qr_status_request_counts.get(qr_key, 0) + 1
            self._qr_status_request_counts[qr_key] = request_count
            body = QR_STATUS_FIRST_BODY if request_count == 1 else QR_STATUS_SUCCESS_BODY

            origin = request.headers.get("origin")
            headers = {
                "content-type": "text/html;charset=UTF-8",
                "cache-control": "no-store, no-cache, must-revalidate",
                "pragma": "no-cache",
            }
            if origin:
                headers["access-control-allow-origin"] = origin
                headers["access-control-allow-credentials"] = "true"

            route.fulfill(status=200, headers=headers, body=body)
            logger.info("已拦截二维码状态接口并返回第 %s 次响应: %s body=%s", request_count, url, body)
        except Exception as exc:  # noqa: BLE001 - 拦截失败时放行请求，避免影响其它流程
            logger.warning("二维码状态接口拦截失败，改为放行: %s", exc)
            try:
                route.continue_()
            except Exception as continue_exc:  # noqa: BLE001
                logger.debug("二维码状态接口放行失败: %s", continue_exc)

    def new_page(self) -> Page:
        """??????"""

        if self.context is None:
            self.create_context()
        assert self.context is not None

        try:
            existing_pages = [page for page in self.context.pages if not page.is_closed()]
            if existing_pages:
                self.page = existing_pages[0]
            else:
                self.page = self.context.new_page()
            self.page.set_default_timeout(WAIT_TIME)
            self.page.set_default_navigation_timeout(WAIT_TIME)
            self._disable_debugger_pause(self.page)
            logger.info("???/????")
            return self.page
        except Exception as exc:
            logger.exception("??????: %s", exc)
            raise

    def _disable_debugger_pause(self, page: Page) -> None:
        """?? CDP ?? debugger ?????????????"""

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
            logger.info("??? debugger ????")
        except Exception as exc:  # noqa: BLE001 - ? Chromium ? CDP ??????????
            logger.debug("?? debugger ???????????: %s", exc)

    def save_storage_state(self) -> None:
        """????????? auth.json?persistent profile ????????????"""

        if self.context is None:
            logger.warning("??????????????????")
            return

        try:
            self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.context.storage_state(path=str(self.storage_state_path))
            logger.info("???????: %s", self.storage_state_path)
        except PlaywrightError as exc:
            logger.exception("????????: %s", exc)

    def close(self) -> None:
        """????????????? Playwright?"""

        logger.info("???????")
        for resource_name in ("page", "context", "browser"):
            resource = getattr(self, resource_name, None)
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:  # noqa: BLE001 - ???????????????
                logger.warning("?? %s ??: %s", resource_name, exc)
            finally:
                setattr(self, resource_name, None)

        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("?? Playwright ??: %s", exc)
            finally:
                self.playwright = None

    def __enter__(self) -> "BrowserManager":
        self.start_browser()
        self.create_context()
        self.new_page()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        self.close()
