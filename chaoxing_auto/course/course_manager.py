"""课程管理模块。"""

from __future__ import annotations

from typing import Dict, List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from config.config import COURSE_LIST_URL, WAIT_TIME
from utils.helper import deduplicate_items, save_screenshot, wait_page_ready
from utils.logger import get_logger

logger = get_logger()


class CourseError(RuntimeError):
    """课程管理异常。"""


class CourseManager:
    """获取课程列表，并根据课程名称进入课程。"""

    def __init__(self, page: Page, course_list_url: str = COURSE_LIST_URL) -> None:
        self.page = page
        self.course_list_url = course_list_url

    def get_courses(self) -> List[Dict[str, str]]:
        """获取我的课程列表。

        Returns:
            List[Dict[str, str]]: 形如 [{"name": "课程名", "url": "课程地址"}]
        """

        try:
            logger.info("打开课程列表页: %s", self.course_list_url)
            self.page.goto(self.course_list_url, wait_until="domcontentloaded", timeout=WAIT_TIME)
            wait_page_ready(self.page, WAIT_TIME)

            # 等待页面主体出现，避免 DOM 未加载完成时抓取为空。
            self.page.wait_for_selector("body", timeout=WAIT_TIME)

            courses = self.page.evaluate(
                """
                () => {
                    const result = [];
                    const pushCourse = (name, url) => {
                        name = (name || '').replace(/\\s+/g, ' ').trim();
                        url = (url || '').trim();
                        if (/^(javascript:|#)/i.test(url)) url = '';
                        if (!name || name.length < 2) return;
                        if (/登录|退出|首页|帮助|客服|通知|消息/.test(name)) return;
                        result.push({ name, url });
                    };

                    const selectors = [
                        'a[href*="course"]',
                        'a[href*="clazz"]',
                        'a[href*="mooc"]',
                        'a[href*="visit"]',
                        '.course a',
                        '.courseName a',
                        '.course-name a',
                        '.Mconright a',
                        '.course-list a',
                        '.item a'
                    ];

                    const nodes = new Set();
                    selectors.forEach(selector => {
                        document.querySelectorAll(selector).forEach(node => nodes.add(node));
                    });

                    nodes.forEach(node => {
                        const text = node.innerText || node.textContent || node.getAttribute('title') || '';
                        let href = node.href || node.getAttribute('data-url') || '';
                        try { href = href ? new URL(href, location.href).href : ''; } catch (e) {}
                        const looksLikeCourse = /course|clazz|mooc|visit|studentcourse|interaction/i.test(href)
                            || node.closest('.course,.courseName,.course-name,.course-list,.item');
                        if (looksLikeCourse) pushCourse(text, href);
                    });

                    // 某些页面课程名在非 a 标签上，尝试从常见课程卡片中寻找可点击链接。
                    document.querySelectorAll('.course,.course-item,.courseCard,.item,.Mconright').forEach(card => {
                        const text = card.innerText || card.textContent || '';
                        const link = card.querySelector('a[href]');
                        let href = link ? link.href : '';
                        try { href = href ? new URL(href, location.href).href : ''; } catch (e) {}
                        const firstLine = text.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
                        if (firstLine && href) pushCourse(firstLine, href);
                    });

                    return result;
                }
                """
            )
            cleaned = deduplicate_items(courses, key_fields=("name", "url"))
            logger.info("获取课程数量: %s", len(cleaned))
            for course in cleaned:
                logger.info("课程: %s -> %s", course.get("name"), course.get("url"))
            return cleaned
        except PlaywrightTimeoutError as exc:
            save_screenshot(self.page, "error_get_courses_timeout")
            logger.exception("获取课程列表超时: %s", exc)
            raise CourseError(f"获取课程列表超时: {exc}") from exc
        except PlaywrightError as exc:
            save_screenshot(self.page, "error_get_courses_playwright")
            logger.exception("获取课程列表失败: %s", exc)
            raise CourseError(f"获取课程列表失败: {exc}") from exc
        except Exception as exc:
            save_screenshot(self.page, "error_get_courses")
            logger.exception("获取课程列表异常: %s", exc)
            raise

    def open_course(self, course_name: str) -> Dict[str, str]:
        """根据课程名称进入课程。

        支持精确匹配和包含匹配。
        """

        if not course_name:
            raise CourseError("课程名称为空，请在 config/config.py 或命令行参数中指定课程名称")

        try:
            courses = self.get_courses()
            target = self._find_course(courses, course_name)
            if target is None:
                # 兜底：如果课程卡片是 JS 点击而没有 href，尝试按文本点击。
                logger.warning("课程列表中未找到 URL 课程，尝试按文本点击: %s", course_name)
                locator = self.page.get_by_text(course_name, exact=False).first
                locator.wait_for(state="visible", timeout=WAIT_TIME)
                locator.click()
                wait_page_ready(self.page, WAIT_TIME)
                logger.info("已通过文本点击进入课程: %s", course_name)
                return {"name": course_name, "url": self.page.url}

            logger.info("进入课程: %s", target["name"])
            url = target.get("url", "")
            if url:
                self.page.goto(url, wait_until="domcontentloaded", timeout=WAIT_TIME)
            else:
                self.page.get_by_text(target["name"], exact=False).first.click()
            wait_page_ready(self.page, WAIT_TIME)
            logger.info("课程页面加载完成: %s", self.page.url)
            return target
        except PlaywrightTimeoutError as exc:
            save_screenshot(self.page, "error_open_course_timeout")
            logger.exception("进入课程超时: %s", exc)
            raise CourseError(f"进入课程超时: {exc}") from exc
        except Exception as exc:
            save_screenshot(self.page, "error_open_course")
            logger.exception("进入课程失败: %s", exc)
            raise

    @staticmethod
    def _find_course(courses: List[Dict[str, str]], course_name: str) -> Optional[Dict[str, str]]:
        """查找目标课程，优先精确匹配。"""

        normalized = course_name.strip().lower()
        for course in courses:
            if course.get("name", "").strip().lower() == normalized:
                return course
        for course in courses:
            name = course.get("name", "").strip().lower()
            if normalized in name or name in normalized:
                return course
        return None
