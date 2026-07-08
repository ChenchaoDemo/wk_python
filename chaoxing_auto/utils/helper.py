"""通用辅助函数与任务状态对象。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeoutError

from config.config import SCREENSHOT_DIR, SHORT_WAIT_TIME
from utils.logger import get_logger

logger = get_logger()


@dataclass
class TaskStatus:
    """自动学习任务状态对象，便于未来被后端接口读取。"""

    course_name: str = ""
    chapter_name: str = ""
    progress: float = 0.0
    status: str = "idle"  # idle/running/courses_loaded/success/failed/stopped/skipped
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转换为接口友好的 dict。"""

        data = asdict(self)
        # 兼容用户要求的字段示例：course/chapter
        data["course"] = self.course_name
        data["chapter"] = self.chapter_name
        data["progress"] = round(float(self.progress), 2)
        return data


def now_text() -> str:
    """返回适合作为文件名的一段时间字符串。"""

    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符。"""

    invalid_chars = '<>:"/\\|?*\n\r\t'
    result = "".join("_" if char in invalid_chars else char for char in name)
    return result.strip(" ._") or "untitled"


def save_screenshot(page: Optional[Page], prefix: str = "error") -> Optional[Path]:
    """保存当前页面截图，异常场景自动调用。"""

    if page is None:
        return None

    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{sanitize_filename(prefix)}_{now_text()}.png"
        page.screenshot(path=str(path), full_page=True)
        logger.info("已保存异常截图: %s", path)
        return path
    except Exception as exc:  # noqa: BLE001 - 截图失败不应覆盖原始异常
        logger.exception("保存截图失败: %s", exc)
        return None


def wait_visible_any_selector(
    page_or_frame: Page | Frame,
    selectors: Iterable[str],
    timeout: int = SHORT_WAIT_TIME,
) -> Optional[Locator]:
    """等待多个 selector 中任意一个可见，返回第一个命中的 Locator。"""

    for selector in selectors:
        try:
            locator = page_or_frame.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            return locator
        except PlaywrightTimeoutError:
            continue
        except PlaywrightError:
            continue
    return None


def exists_any_selector(
    page_or_frame: Page | Frame,
    selectors: Iterable[str],
    timeout: int = 1000,
) -> bool:
    """判断页面上是否存在任意一个 selector。"""

    return wait_visible_any_selector(page_or_frame, selectors, timeout=timeout) is not None


def safe_text(locator: Locator) -> str:
    """安全读取元素文本。"""

    try:
        return locator.inner_text(timeout=SHORT_WAIT_TIME).strip()
    except Exception:
        return ""


def wait_page_ready(page: Page, timeout: int) -> None:
    """等待页面进入可交互状态。"""

    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError:
        logger.warning("等待 domcontentloaded 超时，继续执行")

    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        # 学习通页面可能有长连接或埋点请求，networkidle 超时不一定代表不可用。
        logger.warning("等待 networkidle 超时，继续执行")


def debug_pause(page: Optional[Page], enabled: bool, reason: str = "") -> None:
    """页面调试暂停：打开 Playwright Inspector，便于观察当前页面。"""

    if not enabled or page is None:
        return
    logger.info("进入页面调试暂停模式: %s", reason or "manual")
    page.pause()


def run_with_screenshot(
    page: Optional[Page],
    action: Callable[[], Any],
    screenshot_prefix: str,
) -> Any:
    """执行动作，异常时保存截图并继续抛出异常。"""

    try:
        return action()
    except Exception:
        save_screenshot(page, screenshot_prefix)
        raise


def normalize_url(url: str) -> str:
    """清理 URL 文本。"""

    return (url or "").strip()


def deduplicate_items(items: List[Dict[str, str]], key_fields: tuple[str, ...] = ("name", "url")) -> List[Dict[str, str]]:
    """按照指定字段去重，保持原始顺序。"""

    seen: set[tuple[str, ...]] = set()
    result: List[Dict[str, str]] = []
    for item in items:
        key = tuple((item.get(field) or "").strip() for field in key_fields)
        if not any(key) or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
