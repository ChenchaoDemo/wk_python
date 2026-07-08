"""学习通登录模块。"""

from __future__ import annotations

from typing import Iterable, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from config.config import LOGIN_URL, PASSWORD, SHORT_WAIT_TIME, USERNAME, WAIT_TIME
from utils.helper import exists_any_selector, save_screenshot, wait_page_ready, wait_visible_any_selector
from utils.logger import get_logger

logger = get_logger()


class LoginError(RuntimeError):
    """登录失败。"""


class CaptchaRequiredError(LoginError):
    """出现验证码或滑块，需要人工处理。"""


class LoginManager:
    """负责学习通账号密码登录。"""

    username_selectors: tuple[str, ...] = (
        "#phone",
        "input[name='phone']",
        "input[name='username']",
        "input[id*='user']",
        "input[placeholder*='手机号']",
        "input[placeholder*='账号']",
        "input[placeholder*='超星号']",
        "input[type='text']",
    )
    password_selectors: tuple[str, ...] = (
        "#pwd",
        "input[name='pwd']",
        "input[name='password']",
        "input[type='password']",
        "input[placeholder*='密码']",
    )
    login_button_selectors: tuple[str, ...] = (
        "#loginBtn",
        "button[type='submit']",
        "input[type='submit']",
        "a:has-text('登录')",
        "button:has-text('登录')",
        ".btn:has-text('登录')",
        "#login",
    )
    captcha_selectors: tuple[str, ...] = (
        "text=验证码",
        "text=拖动滑块",
        "text=安全验证",
        "input[placeholder*='验证码']",
        "#captcha",
        ".captcha",
        ".verify",
        ".nc_wrapper",
        "iframe[src*='captcha']",
    )
    logged_in_selectors: tuple[str, ...] = (
        "text=我的课程",
        "text=学习空间",
        "text=退出",
        "a[href*='logout']",
        ".userName",
        ".personalName",
        ".Header_user",
    )
    error_selectors: tuple[str, ...] = (
        "text=账号或密码错误",
        "text=密码错误",
        "text=用户名或密码",
        "text=登录失败",
        "text=账号不存在",
        ".error",
        ".err-txt",
        ".tips",
    )

    def __init__(self, username: str = USERNAME, password: str = PASSWORD, login_url: str = LOGIN_URL) -> None:
        self.username = username
        self.password = password
        self.login_url = login_url

    def login(self, page: Page) -> bool:
        """执行登录流程。

        Returns:
            bool: True 表示登录成功。
        """

        try:
            logger.info("打开登录页: %s", self.login_url)
            page.goto(self.login_url, wait_until="domcontentloaded", timeout=WAIT_TIME)
            wait_page_ready(page, WAIT_TIME)

            if self.is_logged_in(page):
                logger.info("检测到已有有效登录状态")
                return True

            if self._has_captcha(page):
                logger.error("登录页出现验证码，需要人工处理")
                raise CaptchaRequiredError("登录页出现验证码，需要人工处理")

            if not self.username or not self.password:
                raise LoginError("用户名或密码为空，请在 config/config.py 或环境变量中配置")

            username_input = wait_visible_any_selector(page, self.username_selectors, timeout=SHORT_WAIT_TIME)
            if username_input is None:
                raise LoginError("未找到用户名输入框")
            username_input.fill(self.username)
            logger.info("已输入用户名")

            password_input = wait_visible_any_selector(page, self.password_selectors, timeout=SHORT_WAIT_TIME)
            if password_input is None:
                raise LoginError("未找到密码输入框")
            password_input.fill(self.password)
            logger.info("已输入密码")

            if self._has_captcha(page):
                logger.error("提交前检测到验证码，需要人工处理")
                raise CaptchaRequiredError("提交前检测到验证码，需要人工处理")

            login_button = wait_visible_any_selector(page, self.login_button_selectors, timeout=SHORT_WAIT_TIME)
            if login_button is None:
                raise LoginError("未找到登录按钮")

            logger.info("点击登录按钮")
            login_button.click()

            return self._wait_login_result(page)
        except CaptchaRequiredError:
            save_screenshot(page, "error_captcha")
            raise
        except PlaywrightTimeoutError as exc:
            save_screenshot(page, "error_login_timeout")
            logger.exception("登录网络超时或页面加载失败: %s", exc)
            raise LoginError(f"登录超时: {exc}") from exc
        except PlaywrightError as exc:
            save_screenshot(page, "error_login_playwright")
            logger.exception("登录过程出现 Playwright 异常: %s", exc)
            raise LoginError(f"登录过程异常: {exc}") from exc
        except Exception as exc:
            save_screenshot(page, "error_login")
            logger.exception("登录失败: %s", exc)
            raise

    def is_logged_in(self, page: Page) -> bool:
        """判断当前页面是否已登录。"""

        current_url = page.url.lower()
        if "passport" in current_url and "login" in current_url:
            return exists_any_selector(page, self.logged_in_selectors, timeout=1000)

        # 不在登录页且页面具有用户相关元素时，认为登录状态有效。
        if exists_any_selector(page, self.logged_in_selectors, timeout=1000):
            return True

        # URL 跳出 passport 登录域通常也代表登录成功或被重定向到业务页。
        return "passport2.chaoxing.com/login" not in current_url and "passport2.chaoxing.com" not in current_url

    def _has_captcha(self, page: Page) -> bool:
        """检测验证码或滑块。"""

        return exists_any_selector(page, self.captcha_selectors, timeout=1500)

    def _wait_login_result(self, page: Page) -> bool:
        """等待登录结果，不使用固定 sleep。"""

        try:
            page.wait_for_load_state("domcontentloaded", timeout=WAIT_TIME)
        except PlaywrightTimeoutError:
            logger.warning("登录后等待 domcontentloaded 超时，继续检查页面状态")

        # 等待 URL/文本/错误提示/验证码任意结果出现。
        wait_script = """
        () => {
            const text = document.body ? document.body.innerText : '';
            const url = location.href.toLowerCase();
            return !url.includes('passport2.chaoxing.com/login')
                || text.includes('我的课程')
                || text.includes('退出')
                || text.includes('验证码')
                || text.includes('安全验证')
                || text.includes('账号或密码错误')
                || text.includes('密码错误')
                || text.includes('登录失败');
        }
        """
        try:
            page.wait_for_function(wait_script, timeout=WAIT_TIME)
        except PlaywrightTimeoutError:
            logger.warning("等待登录结果超时，开始兜底判断")

        if self._has_captcha(page):
            logger.error("登录后出现验证码，需要人工处理")
            raise CaptchaRequiredError("登录后出现验证码，需要人工处理")

        if exists_any_selector(page, self.error_selectors, timeout=1000):
            error_text = self._read_error_text(page, self.error_selectors)
            logger.error("登录失败: %s", error_text or "账号或密码错误")
            raise LoginError(error_text or "登录失败")

        if self.is_logged_in(page):
            wait_page_ready(page, WAIT_TIME)
            logger.info("登录成功")
            return True

        raise LoginError("无法确认登录成功，请检查页面或选择器")

    @staticmethod
    def _read_error_text(page: Page, selectors: Iterable[str]) -> Optional[str]:
        """读取登录错误提示文本。"""

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=1000):
                    return locator.inner_text(timeout=1000).strip()
            except Exception:
                continue
        return None
