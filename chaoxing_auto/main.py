"""程序入口：学习通自动化执行引擎。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# 确保既支持 `python main.py`，也支持 `python -m chaoxing_auto.main`。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from browser.browser_manager import BrowserManager
from config.config import COURSE_NAME, DEBUG_MODE, HEADLESS, PAUSE_ON_ERROR
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
    ) -> None:
        self.course_name = course_name
        self.headless = headless
        self.debug_mode = debug_mode
        self.status = TaskStatus(course_name=course_name, status="idle")
        self.browser_manager: Optional[BrowserManager] = None
        self._stop_requested = False

    def start_task(self, course_name: Optional[str] = None) -> Dict[str, Any]:
        """启动自动学习任务。

        当前实现为同步执行；后续接入后台服务时，可在外部线程或任务队列中调用。
        """

        if course_name:
            self.course_name = course_name
            self.status.course_name = course_name

        self._stop_requested = False
        self.status.status = "running"
        self.status.progress = 0.0
        self.status.message = "任务启动"
        logger.info("自动学习任务启动，课程名称: %s", self.course_name)

        try:
            self.run()
            if self._stop_requested:
                self.status.status = "stopped"
                self.status.message = "任务已停止"
            else:
                self.status.status = "success"
                self.status.progress = 100.0
                self.status.message = "任务完成"
            return self.get_status()
        except Exception as exc:
            self.status.status = "failed"
            self.status.message = str(exc)
            logger.exception("任务执行失败: %s", exc)
            return self.get_status()

    def stop_task(self) -> Dict[str, Any]:
        """请求停止任务。"""

        self._stop_requested = True
        self.status.status = "stopped"
        self.status.message = "已请求停止任务"
        logger.info("收到停止任务请求")
        return self.get_status()

    def get_status(self) -> Dict[str, Any]:
        """获取当前任务状态。"""

        return self.status.to_dict()

    def run(self) -> None:
        """完整自动化流程。

        启动浏览器 -> 登录学习通 -> 进入指定课程 -> 获取章节 -> 循环播放章节视频。
        """

        if not self.course_name:
            raise ValueError("课程名称为空，请在 config/config.py 设置 COURSE_NAME，或运行时传入 --course")

        page = None
        self.browser_manager = BrowserManager(headless=self.headless)

        try:
            self.browser_manager.start_browser()
            self.browser_manager.create_context()
            page = self.browser_manager.new_page()

            debug_pause(page, self.debug_mode, "浏览器已启动")

            # 登录学习通，登录成功后保存 storage_state，后续运行自动复用。
            login_manager = LoginManager()
            if not login_manager.login(page):
                raise RuntimeError("登录失败")
            self.browser_manager.save_storage_state()
            debug_pause(page, self.debug_mode, "登录完成")

            # 进入课程。
            course_manager = CourseManager(page)
            course = course_manager.open_course(self.course_name)
            self.status.course_name = course.get("name", self.course_name)
            self.status.message = f"已进入课程: {self.status.course_name}"
            logger.info("当前课程: %s", self.status.course_name)
            debug_pause(page, self.debug_mode, "进入课程完成")

            # 获取章节。
            chapter_manager = ChapterManager(page)
            chapters = chapter_manager.get_chapters()
            if not chapters:
                raise RuntimeError("未获取到章节列表，请检查课程页面结构或选择器")

            total = len(chapters)
            logger.info("开始遍历章节，总数: %s", total)

            for index, chapter in enumerate(chapters, start=1):
                if self._stop_requested:
                    logger.info("任务已停止，退出章节循环")
                    break

                chapter_title = chapter.get("title", f"第 {index} 个章节")
                self.status.chapter_name = chapter_title
                self.status.status = "running"
                self.status.message = f"正在学习章节: {chapter_title}"
                self.status.progress = round((index - 1) / total * 100, 2)
                logger.info("开始学习章节 [%s/%s]: %s", index, total, chapter_title)

                try:
                    chapter_manager.open_chapter(chapter)
                    debug_pause(page, self.debug_mode, f"章节已打开: {chapter_title}")

                    video_player = VideoPlayer(page)
                    if not video_player.has_video():
                        logger.warning("章节未检测到视频，跳过: %s", chapter_title)
                        self.status.message = f"章节无视频，已跳过: {chapter_title}"
                        continue

                    def on_progress(video_progress: float, _: Dict[str, float]) -> None:
                        # 总任务进度 = 已完成章节 + 当前视频章节进度。
                        overall = ((index - 1) + video_progress / 100) / total * 100
                        self.status.progress = round(overall, 2)
                        self.status.message = f"章节视频播放中: {chapter_title}，视频进度 {video_progress:.2f}%"

                    played = video_player.play(on_progress=on_progress, should_stop=lambda: self._stop_requested)
                    if self._stop_requested or not played:
                        self.status.message = f"任务停止于章节: {chapter_title}"
                        break
                    self.status.progress = round(index / total * 100, 2)
                    self.status.message = f"章节完成: {chapter_title}"
                    logger.info("章节完成 [%s/%s]: %s", index, total, chapter_title)
                except VideoNotFoundError:
                    logger.warning("章节视频不存在，继续下一章: %s", chapter_title)
                    self.status.message = f"章节无视频，已跳过: {chapter_title}"
                except Exception as exc:
                    save_screenshot(page, f"error_chapter_{index}")
                    logger.exception("章节处理失败，继续下一章节: %s，错误: %s", chapter_title, exc)
                    self.status.message = f"章节处理失败，已跳过: {chapter_title}"
                    if self.debug_mode and PAUSE_ON_ERROR:
                        debug_pause(page, True, f"章节异常: {chapter_title}")
                    continue

            logger.info("自动学习流程结束")
        except Exception:
            if page is not None:
                save_screenshot(page, "error_task")
                if self.debug_mode and PAUSE_ON_ERROR:
                    debug_pause(page, True, "任务异常")
            raise
        finally:
            if self.browser_manager is not None:
                self.browser_manager.close()


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
