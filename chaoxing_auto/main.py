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
from course.question import QuestionManager
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
        self.current_course: Optional[Dict[str, str]] = None
        self._resume_chapter_index = 0
        self._resume_card_index = 0

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

    def _return_to_course_list_after_task(self) -> None:
        """任务结束后回到课程列表页，并刷新内存中的课程列表。

        GUI 模式下完成一门课后不应该关闭浏览器，而是回到课程列表，方便用户继续
        选择其它课程学习。
        """

        try:
            page = self._ensure_page()
            self._notify_status(status="running", message="当前课程任务完成，正在返回课程列表...")
            course_manager = CourseManager(page)
            self.courses = course_manager.get_courses()
            self._notify_status(
                status="success",
                progress=100.0,
                chapter_name="",
                message=f"任务完成，已返回课程列表，可继续选择其他课程（共 {len(self.courses)} 门）",
            )
        except Exception as exc:  # noqa: BLE001 - 返回列表失败不应该把已完成任务判失败
            logger.warning("任务完成后返回课程列表失败，浏览器仍然保留: %s", exc)
            self._notify_status(
                status="success",
                progress=100.0,
                chapter_name="",
                message="任务完成，但返回课程列表失败，浏览器窗口已保留",
            )

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
        self.current_course = dict(course)
        self._resume_chapter_index = 0
        self._resume_card_index = 0
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

            self._learn_open_course(page, start_index=0)
            if self._stop_requested:
                self._notify_status(status="stopped", message="任务已暂停，浏览器窗口已保留")
            else:
                self._return_to_course_list_after_task()
            return self.get_status()
        except Exception as exc:
            if page is not None:
                save_screenshot(page, "error_selected_course_task")
            self._notify_status(status="failed", message=str(exc))
            logger.exception("选中课程学习失败: %s", exc)
            return self.get_status()
        finally:
            logger.info("选中课程任务结束，保留浏览器窗口")

    def resume_task(self) -> Dict[str, Any]:
        """继续上一次被暂停的课程任务。

        继续时复用已经保留的浏览器窗口，并从暂停时所在章节重新开始。
        """

        if self.current_course is None:
            self._notify_status(status="failed", message="没有可继续的课程任务，请先选择课程开始学习")
            return self.get_status()

        self._stop_requested = False
        page: Optional[Page] = None
        course_name = (self.current_course.get("name") or self.course_name or "").strip()
        start_index = max(0, int(self._resume_chapter_index or 0))
        card_start_index = max(0, int(self._resume_card_index or 0))
        self._notify_status(
            status="running",
            course_name=course_name,
            message=(
                f"继续任务：{course_name or '当前课程'}，"
                f"从第 {start_index + 1} 个章节、第 {card_start_index + 1} 个卡片开始"
            ),
        )

        try:
            page = self._ensure_page()
            course_manager = CourseManager(page)
            opened_course = course_manager.open_course_item(self.current_course)
            opened_name = opened_course.get("name", course_name)
            self._notify_status(
                status="running",
                course_name=opened_name,
                message=f"已重新进入课程，继续学习: {opened_name}",
            )
            logger.info("继续课程: %s，从章节索引 %s、卡片索引 %s 开始", opened_name, start_index, card_start_index)

            self._learn_open_course(page, start_index=start_index)
            if self._stop_requested:
                self._notify_status(status="stopped", message="任务已再次暂停，浏览器窗口已保留")
            else:
                self._return_to_course_list_after_task()
            return self.get_status()
        except Exception as exc:
            if page is not None:
                save_screenshot(page, "error_resume_task")
            self._notify_status(status="failed", message=str(exc))
            logger.exception("继续任务失败: %s", exc)
            return self.get_status()
        finally:
            logger.info("继续任务结束，保留浏览器窗口")

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

    def _learn_open_course(self, page: Page, start_index: int = 0) -> None:
        """学习当前已经打开的课程页面。"""

        chapter_manager = ChapterManager(page)
        question_manager = QuestionManager(page)
        question_manager.enable_network_capture(
            label=(self.current_course or {}).get("name") or self.course_name or "当前课程"
        )
        all_chapters = chapter_manager.get_chapters()
        if not all_chapters:
            raise RuntimeError("未获取到章节列表，请检查课程页面结构或选择器")

        original_total = len(all_chapters)
        completed_chapters: List[Dict[str, str]] = []
        pending_chapters: List[Dict[str, str]] = []
        for original_index, chapter in enumerate(all_chapters):
            chapter["_source_index"] = str(original_index)
            if ChapterManager.is_completed_item(chapter):
                completed_chapters.append(chapter)
            else:
                pending_chapters.append(chapter)

        start_index = max(0, min(start_index, original_total))
        pending_chapters = [
            chapter
            for chapter in pending_chapters
            if int(chapter.get("_source_index") or 0) >= start_index
        ]

        logger.info(
            "章节登记完成: 总章节=%s 已完成=%s 待刷=%s 本次从原始序号=%s 开始",
            original_total,
            len(completed_chapters),
            len(pending_chapters),
            start_index + 1 if original_total else 0,
        )
        self._notify_status(
            status="running",
            progress=0.0,
            message=(
                f"章节登记完成：共 {original_total} 节，已完成 {len(completed_chapters)} 节，"
                f"本次待刷 {len(pending_chapters)} 节"
            ),
        )
        for pending_index, chapter in enumerate(pending_chapters, start=1):
            logger.info(
                "待刷章节登记 [%s/%s]: 原始序号=%s title=%s chapterId=%s source=%s class=%s decision=%s",
                pending_index,
                len(pending_chapters),
                int(chapter.get("_source_index") or 0) + 1,
                chapter.get("title"),
                chapter.get("chapter_id") or "未显示",
                chapter.get("status_source") or "未显示",
                chapter.get("status_class") or "未显示",
                chapter.get("status_decision") or "未找到明确状态，按未完成处理",
            )

        if not pending_chapters:
            self._resume_chapter_index = original_total
            self._resume_card_index = 0
            self._notify_status(status="success", progress=100.0, message="课程章节已全部完成，无需学习")
            return

        chapters = pending_chapters
        total = len(chapters)

        logger.info("开始遍历待刷章节，总数: %s（原始章节 %s，已完成 %s）", total, original_total, len(completed_chapters))
        if start_index:
            self._notify_status(status="running", message=f"登记到 {total} 个待刷章节，从原始第 {start_index + 1} 个章节继续")
        else:
            self._notify_status(status="running", message=f"登记到 {total} 个待刷章节，开始学习")

        for index, chapter in enumerate(chapters, start=1):
            if self._stop_requested:
                logger.info("任务已暂停，退出章节循环并保留浏览器")
                break

            chapter_title = chapter.get("title", f"第 {index} 个章节")
            source_index = int(chapter.get("_source_index") or 0)
            self._resume_chapter_index = source_index
            if source_index != start_index:
                self._resume_card_index = 0

            self._notify_status(
                status="running",
                chapter_name=chapter_title,
                progress=round((index - 1) / total * 100, 2),
                message=f"正在学习待刷章节 [{index}/{total}]: {chapter_title}",
            )
            logger.info("开始学习待刷章节 [%s/%s]: %s", index, total, chapter_title)

            try:
                chapter_manager.open_chapter(chapter)
                debug_pause(page, self.debug_mode, f"章节已打开: {chapter_title}")

                if QuestionManager.is_question_like_title(chapter_title) and self._handle_question_page(
                    question_manager,
                    chapter_title,
                ):
                    self._resume_chapter_index = source_index + 1
                    self._resume_card_index = 0
                    self._notify_status(progress=round(index / total * 100, 2), message=f"题目/测验页已记录: {chapter_title}")
                    logger.info("题目/测验页已记录，跳过视频处理 [%s/%s]: %s", index, total, chapter_title)
                    continue

                cards = chapter_manager.get_cards()
                if cards:
                    card_start = self._resume_card_index if source_index == start_index else 0
                    paused = self._learn_chapter_cards(
                        page=page,
                        chapter_manager=chapter_manager,
                        chapter_title=chapter_title,
                        chapter_index=index,
                        chapter_total=total,
                        cards=cards,
                        start_card_index=card_start,
                        resume_chapter_index=source_index,
                        question_manager=question_manager,
                    )
                    if paused:
                        self._notify_status(message=f"任务暂停于章节: {chapter_title}")
                        break
                    self._resume_chapter_index = source_index + 1
                    self._resume_card_index = 0
                    self._notify_status(progress=round(index / total * 100, 2), message=f"章节完成: {chapter_title}")
                    logger.info("章节卡片全部处理完成 [%s/%s]: %s", index, total, chapter_title)
                    continue

                played = self._play_current_video_unit(
                    page=page,
                    unit_title=chapter_title,
                    chapter_index=index,
                    chapter_total=total,
                    unit_index=1,
                    unit_total=1,
                )
                if self._stop_requested or not played:
                    self._notify_status(message=f"任务暂停于章节: {chapter_title}")
                    break
                self._resume_chapter_index = source_index + 1
                self._resume_card_index = 0
                self._notify_status(progress=round(index / total * 100, 2), message=f"章节完成: {chapter_title}")
                logger.info("章节完成 [%s/%s]: %s", index, total, chapter_title)
            except VideoNotFoundError:
                if self._handle_question_page(question_manager, chapter_title):
                    self._resume_chapter_index = source_index + 1
                    self._resume_card_index = 0
                    continue
                logger.warning("未完成章节未检测到视频或卡片视频，继续下一章: %s", chapter_title)
                self._notify_status(message=f"未完成章节未检测到视频，已跳过: {chapter_title}")
                self._resume_chapter_index = source_index + 1
                self._resume_card_index = 0
            except Exception as exc:
                save_screenshot(page, f"error_chapter_{index}")
                logger.exception("章节处理失败，继续下一章节: %s，错误: %s", chapter_title, exc)
                self._notify_status(message=f"章节处理失败，已跳过: {chapter_title}")
                if self.debug_mode and PAUSE_ON_ERROR:
                    debug_pause(page, True, f"章节异常: {chapter_title}")
                self._resume_chapter_index = source_index + 1
                self._resume_card_index = 0
                continue

        logger.info("自动学习流程结束")

    def _learn_chapter_cards(
        self,
        *,
        page: Page,
        chapter_manager: ChapterManager,
        chapter_title: str,
        chapter_index: int,
        chapter_total: int,
        cards: List[Dict[str, str]],
        start_card_index: int = 0,
        resume_chapter_index: Optional[int] = None,
        question_manager: Optional[QuestionManager] = None,
    ) -> bool:
        """逐个点击并处理当前章节内的卡片。

        Returns:
            bool: True 表示任务被暂停；False 表示本章节卡片已处理完。
        """

        card_total = len(cards)
        start_card_index = max(0, min(start_card_index, card_total))
        resume_index = chapter_index - 1 if resume_chapter_index is None else resume_chapter_index
        logger.info("章节 [%s] 检测到 %s 个卡片，从第 %s 个开始", chapter_title, card_total, start_card_index + 1)

        for card_index, card in enumerate(cards[start_card_index:], start=start_card_index + 1):
            if self._stop_requested:
                self._resume_card_index = card_index - 1
                return True

            card_title = card.get("title") or f"卡片 {card_index}"
            self._resume_chapter_index = resume_index
            self._resume_card_index = card_index - 1

            if ChapterManager.is_completed_item(card):
                self._resume_card_index = card_index
                self._notify_status(
                    chapter_name=f"{chapter_title} / {card_title}",
                    message=f"章节卡片已完成，跳过 [{card_index}/{card_total}]: {card_title}",
                )
                logger.info("章节卡片已完成，跳过 [%s/%s]: %s", card_index, card_total, card_title)
                continue

            self._notify_status(
                status="running",
                chapter_name=f"{chapter_title} / {card_title}",
                message=f"正在处理章节卡片 [{card_index}/{card_total}]: {card_title}",
            )

            try:
                chapter_manager.open_card(card)
                debug_pause(page, self.debug_mode, f"章节卡片已打开: {card_title}")

                try:
                    played = self._play_current_video_unit(
                        page=page,
                        unit_title=f"{chapter_title} / {card_title}",
                        chapter_index=chapter_index,
                        chapter_total=chapter_total,
                        unit_index=card_index,
                        unit_total=card_total,
                    )
                except VideoNotFoundError:
                    if question_manager is not None and self._handle_question_page(
                        question_manager,
                        f"{chapter_title} / {card_title}",
                    ):
                        self._resume_card_index = card_index
                        continue
                    logger.info("章节卡片未检测到视频，跳过: %s / %s", chapter_title, card_title)
                    self._notify_status(message=f"章节卡片无视频，已跳过: {card_title}")
                    self._resume_card_index = card_index
                    continue

                if self._stop_requested or not played:
                    self._resume_card_index = card_index - 1
                    return True

                self._resume_card_index = card_index
                logger.info("章节卡片完成 [%s/%s]: %s", card_index, card_total, card_title)
            except Exception as exc:
                save_screenshot(page, f"error_card_{chapter_index}_{card_index}")
                logger.exception("章节卡片处理失败，继续下一卡片: %s，错误: %s", card_title, exc)
                self._notify_status(message=f"章节卡片处理失败，已跳过: {card_title}")
                self._resume_card_index = card_index
                if self.debug_mode and PAUSE_ON_ERROR:
                    debug_pause(page, True, f"章节卡片异常: {card_title}")
                continue

        return False

    def _handle_question_page(self, question_manager: QuestionManager, label: str) -> bool:
        """当前页面不是视频时，尝试识别并导出题目/测验页结构。"""

        try:
            recent_snapshot = question_manager.get_recent_question_snapshot()
            if recent_snapshot and (
                QuestionManager.is_question_like_title(label)
                or "doHomeWork" in str(recent_snapshot.get("url") or "")
            ):
                questions = recent_snapshot.get("questions") or []
                answer_count = len(recent_snapshot.get("page_answer_candidates") or [])
                dump_path = recent_snapshot.get("dump_path") or ""
                raw_path = recent_snapshot.get("raw_path") or ""
                message = (
                    f"检测到题目/测验接口，已导出结构: {label}，"
                    f"题目 {len(questions)} 个，候选答案字段 {answer_count} 个"
                )
                if dump_path:
                    message += f"，结构文件: {dump_path}"
                if raw_path:
                    message += f"，原始响应: {raw_path}"
                self._notify_status(message=message)
                logger.info(message)
                return True

            info = question_manager.inspect_current_page(label=label)
            if not info.get("is_question_page"):
                return False

            questions = info.get("questions") or []
            question_answer_count = sum(len(question.get("answer_candidates") or []) for question in questions)
            page_answer_count = len(info.get("page_answer_candidates") or [])
            answer_count = question_answer_count + page_answer_count
            has_choice_or_answer = answer_count > 0 or any(
                question.get("type") in {"single", "multiple", "judge", "fill"}
                and (question.get("options") or question.get("controls_count"))
                for question in questions
            )
            if not QuestionManager.is_question_like_title(label) and not has_choice_or_answer:
                return False

            dump_path = info.get("dump_path") or ""
            message = (
                f"检测到题目/测验页，已导出结构: {label}，"
                f"题目 {len(questions)} 个，候选答案字段 {answer_count} 个"
            )
            if dump_path:
                message += f"，文件: {dump_path}"
            self._notify_status(message=message)
            logger.info(message)
            return True
        except Exception as exc:  # noqa: BLE001 - 题目分析失败不影响主学习流程
            logger.warning("题目/测验页分析失败，按普通无视频内容跳过: %s", exc)
            return False

    def _play_current_video_unit(
        self,
        *,
        page: Page,
        unit_title: str,
        chapter_index: int,
        chapter_total: int,
        unit_index: int,
        unit_total: int,
    ) -> bool:
        """播放当前页面/卡片中的视频，并按章节和卡片数量折算总进度。"""

        video_player = VideoPlayer(page)
        if not video_player.has_video(timeout=8000):
            raise VideoNotFoundError("当前页面或卡片未检测到视频")

        task_status = video_player.get_video_task_status(timeout=8000)
        task_point_status = str(task_status.get("status") or "unknown")
        if task_point_status != "unfinished":
            if task_point_status == "completed":
                message = f"视频任务点已完成，跳过播放: {unit_title}"
            else:
                message = f"未检测到明确“任务点未完成”状态，跳过播放: {unit_title}"
            self._notify_status(
                message=message,
            )
            logger.info(message)
            return True

        unit_total = max(1, unit_total)
        unit_index = max(1, unit_index)

        def on_progress(video_progress: float, _: Dict[str, float]) -> None:
            # 总进度 = 已完成章节 + 当前章节内已完成卡片 + 当前视频进度。
            current_chapter_fraction = ((unit_index - 1) + video_progress / 100) / unit_total
            overall = ((chapter_index - 1) + current_chapter_fraction) / chapter_total * 100
            self._notify_status(
                progress=overall,
                message=f"视频播放中: {unit_title}，视频进度 {video_progress:.2f}%",
            )

        return video_player.play(on_progress=on_progress, should_stop=lambda: self._stop_requested)


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
