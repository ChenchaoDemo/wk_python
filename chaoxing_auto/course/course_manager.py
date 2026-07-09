"""课程管理模块。"""

from __future__ import annotations

from typing import Dict, List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from config.config import COURSE_LIST_URL, WAIT_TIME
from utils.helper import save_screenshot, wait_page_ready
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
            List[Dict[str, str]]: 形如 [{"name": "课程名", "url": "课程地址", ...更多课程信息}]
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

                    const normalizeText = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const normalizeLines = (value) => (value || '')
                        .split('\\n')
                        .map(line => normalizeText(line))
                        .filter(Boolean);

                    const datePattern = '(\\\\d{4}[./-]\\\\d{1,2}[./-]\\\\d{1,2}(?:\\\\s+\\\\d{1,2}:\\\\d{2}(?::\\\\d{2})?)?|\\\\d{4}年\\\\d{1,2}月\\\\d{1,2}日(?:\\\\s*\\\\d{1,2}:\\\\d{2}(?::\\\\d{2})?)?)';

                    const findByLabels = (text, labels) => {
                        for (const label of labels) {
                            const reg = new RegExp(`${label}\\\\s*[:：]?\\\\s*(${datePattern})`, 'i');
                            const match = text.match(reg);
                            if (match) return normalizeText(match[1]);
                        }
                        return '';
                    };

                    const findDateRange = (text) => {
                        const rangeReg = new RegExp(`(?:开课时间|课程时间|学习时间|有效期|起止时间|开放时间)\\\\s*[:：]?\\\\s*(${datePattern})\\\\s*(?:至|到|[-~—–])\\\\s*(${datePattern})`, 'i');
                        const match = text.match(rangeReg);
                        if (!match) return { start: '', end: '' };
                        return { start: normalizeText(match[1]), end: normalizeText(match[2]) };
                    };

                    const isDirtyCourseName = (name) => {
                        const text = normalizeText(name);
                        if (!text) return true;
                        if (/^(移动到|添加课程|退课|已退课课程)$/.test(text)) return true;
                        if (/结束/.test(text)) return true;
                        if (/登录|退出|首页|帮助|客服|通知|消息/.test(text)) return true;
                        return false;
                    };

                    const extractCourseId = (url, extraText = '') => {
                        const candidates = [];
                        const addCandidate = (value) => {
                            value = normalizeText(value);
                            if (value) candidates.push(value);
                        };

                        addCandidate(url);
                        try {
                            const decoded = decodeURIComponent(url || '');
                            addCandidate(decoded);
                        } catch (e) {}
                        addCandidate(extraText);

                        try {
                            const parsed = new URL(url, location.href);
                            for (const [key, value] of parsed.searchParams.entries()) {
                                if (/^courseid$/i.test(key) && value) return value;
                            }
                            for (const [key, value] of parsed.searchParams.entries()) {
                                if (/course.*id/i.test(key) && value) return value;
                            }
                        } catch (e) {}

                        for (const candidate of candidates) {
                            const match = candidate.match(/(?:^|[?&#/;\\s_-])courseId\\s*[=:]\\s*([0-9A-Za-z_-]+)/i)
                                || candidate.match(/(?:^|[?&#/;\\s_-])courseid\\s*[=:]\\s*([0-9A-Za-z_-]+)/i)
                                || candidate.match(/courseId%3D([0-9A-Za-z_-]+)/i)
                                || candidate.match(/courseid%3D([0-9A-Za-z_-]+)/i);
                            if (match) return match[1];
                        }
                        return '';
                    };

                    const extractTaskInfo = (text) => {
                        let taskDone = '';
                        let taskTotal = '';

                        const fractionPatterns = [
                            /(?:任务点|任务|进度|完成情况)\\s*[:：]?\\s*(\\d+)\\s*\\/\\s*(\\d+)/i,
                            /已完成\\s*(\\d+)\\s*(?:个|项)?\\s*(?:\\/|，|,)?\\s*(?:共|总计|总)?\\s*(\\d+)\\s*(?:个|项)?\\s*(?:任务点|任务)?/i,
                            /(\\d+)\\s*\\/\\s*(\\d+)\\s*(?:个|项)?\\s*(?:任务点|任务)/i
                        ];
                        for (const pattern of fractionPatterns) {
                            const match = text.match(pattern);
                            if (match) {
                                taskDone = match[1];
                                taskTotal = match[2];
                                break;
                            }
                        }

                        if (!taskDone) {
                            const doneMatch = text.match(/已完成\\s*[:：]?\\s*(\\d+)\\s*(?:个|项)?\\s*(?:任务点|任务)?/i);
                            if (doneMatch) taskDone = doneMatch[1];
                        }
                        if (!taskTotal) {
                            const totalMatch = text.match(/(?:课程任务数|总任务数|任务总数|任务点总数|任务数|任务点|共)\\s*[:：]?\\s*(\\d+)\\s*(?:个|项)?\\s*(?:任务点|任务)?/i);
                            if (totalMatch) taskTotal = totalMatch[1];
                        }

                        const taskProgress = taskDone && taskTotal
                            ? `${taskDone}/${taskTotal}`
                            : (taskDone ? `已完成 ${taskDone}` : (taskTotal ? `共 ${taskTotal}` : ''));

                        return {
                            task_done: taskDone,
                            task_total: taskTotal,
                            task_progress: taskProgress,
                        };
                    };

                    const extractExamInfo = (text) => {
                        const explicitNoExam = /暂无考试|无考试|没有考试|未安排考试/.test(text);
                        const hasExamText = /考试|测验|期末|开考|待考/.test(text);
                        let examStatus = '';

                        if (explicitNoExam) {
                            examStatus = '无考试';
                        } else if (hasExamText) {
                            if (/进行中|正在考试/.test(text)) examStatus = '考试进行中';
                            else if (/待考试|未开始|未开考/.test(text)) examStatus = '有考试-未开始';
                            else if (/已完成|已考试|已交卷|已提交/.test(text)) examStatus = '考试已完成';
                            else if (/已截止|已结束/.test(text)) examStatus = '考试已结束';
                            else examStatus = '有考试';
                        }

                        return {
                            has_exam: explicitNoExam ? '否' : (hasExamText ? '是' : ''),
                            exam_status: examStatus,
                            exam_start_time: findByLabels(text, ['考试开始时间', '考试时间', '开考时间', '开始考试时间']),
                            exam_end_time: findByLabels(text, ['考试截止时间', '考试结束时间', '考试截止', '交卷截止时间']),
                        };
                    };

                    const extractName = (rawName, fallbackText) => {
                        const lines = normalizeLines(rawName);
                        const fallbackLines = normalizeLines(fallbackText);
                        const allLines = [...lines, ...fallbackLines];
                        for (const line of allLines) {
                            if (isDirtyCourseName(line)) continue;
                            if (/开课|开始时间|结束|截止|有效期|考试|测验|任务|进度|教师|老师|班级|学分|进入|继续学习/.test(line)) continue;
                            if (line.length >= 2) return line;
                        }
                        return normalizeText(rawName || fallbackText);
                    };

                    const findCourseCard = (node) => {
                        return node.closest('.course,.course-item,.courseCard,.course-card,.courseName,.course-name,.course-list,.item,.Mconright,li,div') || node;
                    };

                    const pushCourse = (name, url, detailsText = '') => {
                        const detailRaw = detailsText || name || '';
                        const text = normalizeText(detailRaw);
                        name = extractName(name, detailRaw);
                        url = (url || '').trim();
                        if (/^(javascript:|#)/i.test(url)) url = '';
                        if (!name || name.length < 2) return;
                        if (isDirtyCourseName(name)) return;

                        const range = findDateRange(text);
                        const startTime = findByLabels(text, ['课程开始时间', '开课时间', '开始时间', '学习开始时间', '起始时间']) || range.start;
                        const endTime = findByLabels(text, ['课程截止时间', '截止时间', '结束时间', '结课时间', '学习截止时间', '有效期至']) || range.end;
                        const taskInfo = extractTaskInfo(text);
                        const examInfo = extractExamInfo(text);
                        const courseId = extractCourseId(url, text);

                        result.push({
                            name,
                            course_id: courseId,
                            url,
                            start_time: startTime,
                            end_time: endTime,
                            ...examInfo,
                            ...taskInfo,
                            raw_text: text,
                        });
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
                        const card = findCourseCard(node);
                        const text = node.getAttribute('title') || node.innerText || node.textContent || '';
                        const cardText = card ? (card.innerText || card.textContent || '') : text;
                        const attrText = [
                            node.getAttribute('data-courseid'),
                            node.getAttribute('data-courseId'),
                            node.getAttribute('courseid'),
                            node.getAttribute('courseId'),
                            node.getAttribute('data-id'),
                            card ? card.getAttribute('data-courseid') : '',
                            card ? card.getAttribute('data-courseId') : '',
                            card ? card.getAttribute('courseid') : '',
                            card ? card.getAttribute('courseId') : '',
                        ].filter(Boolean).join(' ');
                        let href = node.href || node.getAttribute('data-url') || '';
                        try { href = href ? new URL(href, location.href).href : ''; } catch (e) {}
                        const looksLikeCourse = /course|clazz|mooc|visit|studentcourse|interaction/i.test(href)
                            || node.closest('.course,.courseName,.course-name,.course-list,.item');
                        if (looksLikeCourse) pushCourse(text, href, `${cardText} ${attrText}`);
                    });

                    // 某些页面课程名在非 a 标签上，尝试从常见课程卡片中寻找可点击链接。
                    document.querySelectorAll('.course,.course-item,.courseCard,.course-card,.item,.Mconright').forEach(card => {
                        const text = card.innerText || card.textContent || '';
                        const link = card.querySelector('a[href]');
                        let href = link ? link.href : '';
                        const attrText = [
                            card.getAttribute('data-courseid'),
                            card.getAttribute('data-courseId'),
                            card.getAttribute('courseid'),
                            card.getAttribute('courseId'),
                            card.getAttribute('data-id'),
                        ].filter(Boolean).join(' ');
                        try { href = href ? new URL(href, location.href).href : ''; } catch (e) {}
                        const firstLine = text.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
                        if (firstLine && href) pushCourse(firstLine, href, `${text} ${attrText}`);
                    });

                    return result;
                }
                """
            )
            cleaned = self._merge_course_items(courses)
            logger.info("获取课程数量: %s", len(cleaned))
            for course in cleaned:
                logger.info(
                    "课程: %s | courseId=%s 开始=%s 截止=%s 考试=%s 考试开始=%s 任务=%s -> %s",
                    course.get("name"),
                    course.get("course_id") or "未显示",
                    course.get("start_time") or "未显示",
                    course.get("end_time") or "未显示",
                    course.get("exam_status") or course.get("has_exam") or "未显示",
                    course.get("exam_start_time") or "未显示",
                    course.get("task_progress") or "未显示",
                    course.get("url"),
                )
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

    def open_course_item(self, course: Dict[str, str]) -> Dict[str, str]:
        """进入已经从课程列表中选中的课程。

        GUI 流程会先获取课程列表，用户选择课程后再开始学习；此方法避免再次
        重新抓取课程列表，直接使用用户选中的课程名称和链接进入课程。
        """

        name = (course.get("name") or "").strip()
        url = (course.get("url") or "").strip()
        if not name and not url:
            raise CourseError("选中的课程缺少名称和链接")

        try:
            logger.info("进入选中课程: %s", name or url)
            if url:
                self.page.goto(url, wait_until="domcontentloaded", timeout=WAIT_TIME)
            else:
                self.page.get_by_text(name, exact=False).first.click()

            wait_page_ready(self.page, WAIT_TIME)
            logger.info("选中课程页面加载完成: %s", self.page.url)
            return {"name": name or "未命名课程", "url": url or self.page.url}
        except PlaywrightTimeoutError as exc:
            save_screenshot(self.page, "error_open_selected_course_timeout")
            logger.exception("进入选中课程超时: %s", exc)
            raise CourseError(f"进入选中课程超时: {exc}") from exc
        except Exception as exc:
            save_screenshot(self.page, "error_open_selected_course")
            logger.exception("进入选中课程失败: %s", exc)
            raise

    @staticmethod
    def _merge_course_items(courses: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """按课程名和链接去重，并合并后续条目中更完整的课程字段。"""

        result: List[Dict[str, str]] = []
        index_by_key: Dict[tuple[str, str], int] = {}

        for course in courses:
            name = (course.get("name") or "").strip()
            url = (course.get("url") or "").strip()
            if not name and not url:
                continue
            if name in {"移动到", "添加课程", "退课", "已退课课程"} or "结束" in name:
                continue
            key = (name, url)

            if key not in index_by_key:
                index_by_key[key] = len(result)
                result.append(dict(course))
                continue

            existing = result[index_by_key[key]]
            for field, value in course.items():
                if value and not existing.get(field):
                    existing[field] = value
                elif field == "raw_text" and value and len(value) > len(existing.get(field, "")):
                    existing[field] = value

        return result

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
