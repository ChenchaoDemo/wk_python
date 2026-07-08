"""程序入口：学习通自动化执行引擎。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# 确保既支持 `python main.py`，也支持 `python -m chaoxing_auto.main`。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import Page

from browser.browser_manager import BrowserManager
from config.config import COURSE_NAME, DEBUG_MODE, HEADLESS, PASSWORD, PAUSE_ON_ERROR, USERNAME
from course.chapter import ChapterManager
from course.course_manager import CourseManager
from login.login import LoginManager
from utils.helper import TaskStatus, debug_pause, save_screenshot
from utils.logger import get_logger
from video.video_player import VideoNotFoundError, VideoPlayer

logger = get_logger()


class ChaoxingAutomationEngine:
    """学习通自动化执行引擎。

    该类预留 start_task / stop_task / get_status 接口，后续可以直接被
    Flask、FastAPI、SpringBoot 网关或任务调度器调用。
    """

    def __init__(
        self,
        course_name: str = COURSE_NAME,
        headless: bool = HEADLESS,
        debug_mode: bool = DEBUG_MODE,
        username: str = USERNAME,
        password: str = PASSWORD,
        allow_manual_verify: bool = False,
        status_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.course_name = course_name
        self.headless = headless
        self.debug_mode = debug_mode
        self.username = username
        self.password = password
        self.allow_manual_verify = allow_manual_verify
        self.status_callback = status_callback
        self.status = TaskStatus(course_name=course_name, status="idle")
        self.browser_manager: Optional[BrowserManager] = None
        self._stop_requested = False
        self.courses: List[Dict[str, str]] = []

    def set_status_callback(self, callback: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """设置状态回调，GUI 可通过该回调实时刷新界面。"""

        self.status_callback = callback

    def _notify_status(
        self,
        *,
        status: Optional[str] = None,
        message: Optional[str] = None,
        progress: Optional[float] = None,
        course_name: Optional[str] = None,
        chapter_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """更新并广播当前任务状态。"""

        if status is not None:
            self.status.status = status
        if message is not None:
            self.status.message = message
        if progress is not None:
            self.status.progress = round(float(progress), 2)
        if course_name is not None:
            self.course_name = course_name
            self.status.course_name = course_name
        if chapter_name is not None:
            self.status.chapter_name = chapter_name

        data = self.get_status()
        if self.status_callback is not None:
            try:
                self.status_callback(data)
            except Exception:
                logger.exception("任务状态回调执行失败")
        return data

    def _login_status_message(self, message: str) -> None:
        """将登录模块的文本状态转成任务状态。"""

        self._notify_status(status="running", message=message)

    def _ensure_page(self) -> Page:
        """确保浏览器、上下文和页面已经创建。"""

        if self.browser_manager is None:
            self.browser_manager = BrowserManager(headless=self.headless)

        if self.browser_manager.browser is None or not self.browser_manager.browser.is_connected():
            self._notify_status(status="running", message="正在启动浏览器...")
            self.browser_manager.context = None
            self.browser_manager.page = None
            self.browser_manager.start_browser()

        if self.browser_manager.context is None:
            self.browser_manager.create_context()

        if self.browser_manager.page is None or self.browser_manager.page.is_closed():
            return self.browser_manager.new_page()

        return self.browser_manager.page

    def close(self) -> None:
        """关闭浏览器资源。"""

        if self.browser_manager is not None:
            self.browser_manager.close()
            self.browser_manager = None

    def start_task(self, course_name: Optional[str] = None) -> Dict[str, Any]:
        """启动自动学习任务。

        当前实现为同步执行；后续接入后台服务时，可在外部线程或任务队列中调用。
        """

        if course_name:
            self.course_name = course_name
            self.status.course_name = course_name

        self._stop_requested = False
        self._notify_status(status="running", progress=0.0, message="任务启动")
        logger.info("自动学习任务启动，课程名称: %s", self.course_name)

        try:
            self.run()
            if self._stop_requested:
                self._notify_status(status="stopped", message="任务已停止")
            else:
                self._notify_status(status="success", progress=100.0, message="任务完成")
            return self.get_status()
        except Exception as exc:
            self._notify_status(status="failed", message=str(exc))
            logger.exception("任务执行失败: %s", exc)
            return self.get_status()

    def stop_task(self) -> Dict[str, Any]:
        """请求暂停任务。

        暂停只停止自动化循环，不主动关闭浏览器窗口，便于在当前页面继续观察或手动处理。
        """

        self._stop_requested = True
        self._notify_status(status="stopped", message="已请求暂停任务，浏览器窗口会保留")
        logger.info("收到暂停任务请求")
        return self.get_status()

    def get_status(self) -> Dict[str, Any]:
        """获取当前任务状态。"""

        return self.status.to_dict()

    def login_and_get_courses(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """登录学习通并获取课程列表。

        该方法供 GUI 使用：用户先输入账号密码，点击“登录并获取课程”，成功后
        返回课程列表，浏览器保持打开，等待用户选择课程。
        """

        if username is not None:
            self.username = username.strip()
        if password is not None:
            self.password = password

        self._stop_requested = False
        page: Optional[Page] = None
        self._notify_status(status="running", progress=0.0, chapter_name="", message="正在登录并获取课程列表...")

        try:
            page = self._ensure_page()
            debug_pause(page, self.debug_mode, "浏览器已启动")

            login_manager = LoginManager(
                username=self.username,
                password=self.password,
                allow_manual_verify=self.allow_manual_verify,
                status_callback=self._login_status_message,
            )
            if not login_manager.login(page):
                raise RuntimeError("登录失败")

            if self.browser_manager is not None:
                self.browser_manager.save_storage_state()
            debug_pause(page, self.debug_mode, "登录完成")

            self._notify_status(status="running", message="登录成功，正在读取课程列表...")
            course_manager = CourseManager(page)
            self.courses = course_manager.get_courses()

            message = (
                f"已获取到 {len(self.courses)} 门课程，请选择课程后点击“开始学习”"
                if self.courses
                else "登录成功，但未获取到课程，请检查课程入口或页面结构"
            )
            self._notify_status(status="courses_loaded", progress=0.0, message=message)
            return self.courses
        except Exception as exc:
            if page is not None:
                save_screenshot(page, "error_login_courses")
            self._notify_status(status="failed", message=str(exc))
            logger.exception("登录并获取课程失败: %s", exc)
            raise

    def start_selected_course(self, course: Dict[str, str]) -> Dict[str, Any]:
        """从 GUI 中用户选择的课程开始学习。"""

        self._stop_requested = False
        page: Optional[Page] = None
        course_name = (course.get("name") or "").strip()
        self._notify_status(
            status="running",
            progress=0.0,
            course_name=course_name,
            chapter_name="",
            message=f"开始学习课程: {course_name or '选中课程'}",
        )

        try:
            page = self._ensure_page()
            course_manager = CourseManager(page)
            opened_course = course_manager.open_course_item(course)
            opened_name = opened_course.get("name", course_name)
            self._notify_status(
                status="running",
                course_name=opened_name,
                message=f"已进入课程: {opened_name}",
            )
            logger.info("当前课程: %s", opened_name)
            debug_pause(page, self.debug_mode, "进入课程完成")

            self._learn_open_course(page)
            if self._stop_requested:
                self._notify_status(status="stopped", message="任务已暂停，浏览器窗口已保留")
            else:
                self._notify_status(status="success", progress=100.0, message="任务完成")
            return self.get_status()
        except Exception as exc:
            if page is not None:
                save_screenshot(page, "error_selected_course_task")
            self._notify_status(status="failed", message=str(exc))
            logger.exception("选中课程学习失败: %s", exc)
            return self.get_status()
        finally:
            if self._stop_requested:
                logger.info("任务已暂停，保留浏览器窗口")
            else:
                self.close()

    def run(self) -> None:
        """完整自动化流程。

        启动浏览器 -> 登录学习通 -> 进入指定课程 -> 获取章节 -> 循环播放章节视频。
        """

        if not self.course_name:
            raise ValueError("课程名称为空，请在 config/config.py 设置 COURSE_NAME，或运行时传入 --course")

        page: Optional[Page] = None

        try:
            self.login_and_get_courses(username=self.username, password=self.password)
            page = self._ensure_page()

            # 进入课程。
            course_manager = CourseManager(page)
            selected_course = CourseManager._find_course(self.courses, self.course_name)
            if selected_course is not None:
                course = course_manager.open_course_item(selected_course)
            else:
                course = course_manager.open_course(self.course_name)
            opened_course_name = course.get("name", self.course_name)
            self._notify_status(course_name=opened_course_name, message=f"已进入课程: {opened_course_name}")
            logger.info("当前课程: %s", opened_course_name)
            debug_pause(page, self.debug_mode, "进入课程完成")

            self._learn_open_course(page)
        except Exception:
            if page is not None:
                save_screenshot(page, "error_task")
                if self.debug_mode and PAUSE_ON_ERROR:
                    debug_pause(page, True, "任务异常")
            raise
        finally:
            self.close()

    def _learn_open_course(self, page: Page) -> None:
        """学习当前已经打开的课程页面。"""

        chapter_manager = ChapterManager(page)
        chapters = chapter_manager.get_chapters()
        if not chapters:
            raise RuntimeError("未获取到章节列表，请检查课程页面结构或选择器")

        total = len(chapters)
        logger.info("开始遍历章节，总数: %s", total)
        self._notify_status(status="running", message=f"获取到 {total} 个章节，开始学习")

        for index, chapter in enumerate(chapters, start=1):
            if self._stop_requested:
                logger.info("任务已暂停，退出章节循环并保留浏览器")
                break

            chapter_title = chapter.get("title", f"第 {index} 个章节")
            self._notify_status(
                status="running",
                chapter_name=chapter_title,
                progress=round((index - 1) / total * 100, 2),
                message=f"正在学习章节: {chapter_title}",
            )
            logger.info("开始学习章节 [%s/%s]: %s", index, total, chapter_title)

            try:
                chapter_manager.open_chapter(chapter)
                debug_pause(page, self.debug_mode, f"章节已打开: {chapter_title}")

                video_player = VideoPlayer(page)
                if not video_player.has_video():
                    logger.warning("章节未检测到视频，跳过: %s", chapter_title)
                    self._notify_status(message=f"章节无视频，已跳过: {chapter_title}")
                    continue

                def on_progress(video_progress: float, _: Dict[str, float]) -> None:
                    # 总任务进度 = 已完成章节 + 当前视频章节进度。
                    overall = ((index - 1) + video_progress / 100) / total * 100
                    self._notify_status(
                        progress=overall,
                        message=f"章节视频播放中: {chapter_title}，视频进度 {video_progress:.2f}%",
                    )

                played = video_player.play(on_progress=on_progress, should_stop=lambda: self._stop_requested)
                if self._stop_requested or not played:
                    self._notify_status(message=f"任务暂停于章节: {chapter_title}")
                    break
                self._notify_status(progress=round(index / total * 100, 2), message=f"章节完成: {chapter_title}")
                logger.info("章节完成 [%s/%s]: %s", index, total, chapter_title)
            except VideoNotFoundError:
                logger.warning("章节视频不存在，继续下一章: %s", chapter_title)
                self._notify_status(message=f"章节无视频，已跳过: {chapter_title}")
            except Exception as exc:
                save_screenshot(page, f"error_chapter_{index}")
                logger.exception("章节处理失败，继续下一章节: %s，错误: %s", chapter_title, exc)
                self._notify_status(message=f"章节处理失败，已跳过: {chapter_title}")
                if self.debug_mode and PAUSE_ON_ERROR:
                    debug_pause(page, True, f"章节异常: {chapter_title}")
                continue

        logger.info("自动学习流程结束")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="学习通 Python + Playwright 自动化执行引擎")
    parser.add_argument("--course", type=str, default=COURSE_NAME, help="课程名称，例如：人工智能导论")
    parser.add_argument("--headless", action="store_true", help="使用无界面模式运行")
    parser.add_argument("--headed", action="store_true", help="强制使用有界面模式运行")
    parser.add_argument("--debug", action="store_true", help="启用 Playwright 页面调试暂停模式")
    return parser.parse_args()


def run() -> Dict[str, Any]:
    """函数式入口，便于外部模块调用。"""

    args = parse_args()
    headless = HEADLESS
    if args.headless:
        headless = True
    if args.headed:
        headless = False

    engine = ChaoxingAutomationEngine(
        course_name=args.course,
        headless=headless,
        debug_mode=args.debug or DEBUG_MODE,
    )
    return engine.start_task()


if __name__ == "__main__":
    final_status = run()
    logger.info("最终任务状态: %s", final_status)
