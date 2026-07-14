"""学习通考试、作业列表查询模块。"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from playwright.sync_api import Frame, Page

from config.config import COURSE_LIST_URL, WAIT_TIME
from course.course_manager import CourseManager
from utils.helper import save_screenshot, wait_page_ready
from utils.logger import get_logger

logger = get_logger()


class TaskQueryError(RuntimeError):
    """考试或作业列表查询失败。"""


class CourseTaskQueryManager:
    """按课程查询考试和作业。

    学习通不同学校、不同课程使用的页面版本并不完全一致。本模块不依赖单一固定
    接口，而是进入每门课程后尝试点击“考试/作业”入口，再从主页面和 iframe 中
    提取列表项。
    """

    EXAM_LABELS = ("考试", "测验", "考试测验", "在线考试", "试卷")
    HOMEWORK_LABELS = ("作业", "章节作业", "课程作业", "作业考试", "练习")

    _CLICK_ENTRY_SCRIPT = """
        ({ labels }) => {
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && Number(style.opacity || 1) > 0
                    && rect.width > 4
                    && rect.height > 4;
            };
            const candidates = [];
            const selector = 'a,button,[role="tab"],[role="menuitem"],li,span,div';
            document.querySelectorAll(selector).forEach(node => {
                if (!isVisible(node)) return;
                const text = clean(
                    node.getAttribute('title')
                    || node.getAttribute('aria-label')
                    || node.innerText
                    || node.textContent
                    || ''
                );
                if (!text || text.length > 30) return;
                labels.forEach((label, index) => {
                    const exact = text === label;
                    const contains = text.includes(label);
                    if (!exact && !contains) return;
                    const clickable = node.closest('a,button,[role="tab"],[role="menuitem"],li') || node;
                    const tagScore = clickable.tagName === 'A'
                        ? 0
                        : (clickable.tagName === 'BUTTON' ? 1 : 2);
                    candidates.push({
                        node: clickable,
                        text,
                        score: (exact ? 0 : 10) + index * 2 + tagScore + Math.min(text.length, 20) / 100,
                    });
                });
            });

            candidates.sort((left, right) => left.score - right.score);
            if (!candidates.length) {
                return { ok: false, text: '', href: '' };
            }

            const target = candidates[0].node;
            const text = candidates[0].text;
            if (target.tagName === 'A') {
                target.setAttribute('target', '_self');
            }
            target.scrollIntoView({ block: 'center', inline: 'center' });
            if (typeof target.click === 'function') {
                target.click();
            } else {
                target.dispatchEvent(new MouseEvent('click', {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                }));
            }
            return {
                ok: true,
                text,
                href: target.href || target.getAttribute('href') || '',
            };
        }
    """

    _EXTRACT_ITEMS_SCRIPT = """
        ({ kind, fallbackCourseName }) => {
            const result = [];
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const cleanLines = (value) => (value || '')
                .split('\\n')
                .map(line => clean(line))
                .filter(Boolean);
            const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && Number(style.opacity || 1) > 0
                    && rect.width > 2
                    && rect.height > 2;
            };
            const datePattern = '(?:\\\\d{4}[./-]\\\\d{1,2}[./-]\\\\d{1,2}(?:\\\\s+\\\\d{1,2}:\\\\d{2}(?::\\\\d{2})?)?|\\\\d{4}年\\\\d{1,2}月\\\\d{1,2}日(?:\\\\s*\\\\d{1,2}:\\\\d{2}(?::\\\\d{2})?)?|\\\\d{1,2}月\\\\d{1,2}日(?:\\\\s*\\\\d{1,2}:\\\\d{2}(?::\\\\d{2})?)?)';

            const findByLabels = (text, labels) => {
                for (const label of labels) {
                    const match = text.match(new RegExp(`${label}\\\\s*[:：]?\\\\s*(${datePattern})`, 'i'));
                    if (match) return clean(match[1]);
                }
                return '';
            };

            const findRange = (text) => {
                const match = text.match(new RegExp(`(${datePattern})\\\\s*(?:至|到|[-~—–])\\\\s*(${datePattern})`, 'i'));
                return match ? { start: clean(match[1]), end: clean(match[2]) } : { start: '', end: '' };
            };

            const matchesKind = (text) => {
                if (kind === 'exam') {
                    return /考试|测验|试卷|开考|交卷|exam|paper|test/i.test(text);
                }
                const hasHomework = /作业|练习|homework|doHomeWork|workList|work\\/|work\\?/i.test(text)
                    || (/任务/.test(text) && /待做|待完成|未提交|未作答|提交|作答/.test(text));
                const examOnly = /考试|开考|交卷|exam|paper/i.test(text) && !/作业|练习|homework|work/i.test(text);
                return hasHomework && !examOnly;
            };

            const classifyStatus = (text) => {
                if (/已截止|已结束|已过期|已关闭|逾期|过期/.test(text)) {
                    return { status: 'expired', statusText: '已截止' };
                }
                if (
                    /待考试|待考|未开考|未考试|未开始|待做|待完成|未完成|未提交|未作答|待提交|去完成|去提交|写作业|开始作答|开始考试|立即考试|参加考试/.test(text)
                ) {
                    return { status: 'pending', statusText: kind === 'exam' ? '待考试' : '待做' };
                }
                if (/进行中|正在考试|正在进行|作答中|可提交|可考试/.test(text)) {
                    return { status: 'running', statusText: '进行中' };
                }
                if (/已提交|已完成|已批阅|已交卷|已考试|已作答|已做完|查看成绩|批阅完成/.test(text)) {
                    return { status: 'completed', statusText: '已完成' };
                }
                return { status: 'unknown', statusText: '状态未显示' };
            };

            const extractCourseName = (text) => {
                const match = text.match(/(?:所属课程|课程名称|课程)\\s*[:：]\\s*([^\\n]{2,80})/);
                if (match) return clean(match[1]);
                return clean(fallbackCourseName);
            };

            const extractTitle = (node, text) => {
                const attrNode = node.querySelector('[title]');
                const attrTitle = clean(attrNode ? attrNode.getAttribute('title') : '');
                if (attrTitle && attrTitle.length >= 2 && !/开始时间|截止时间|状态|操作/.test(attrTitle)) {
                    return attrTitle;
                }

                const lines = cleanLines(text);
                const keyword = kind === 'exam'
                    ? /考试|测验|试卷|考核/
                    : /作业|练习|任务/;
                const ignored = /^(作业|考试|测验|状态|操作|详情|查看|提交|完成|未完成|未提交|待做|待考试|已完成|已截止)$/;
                const labelLine = /^(开始时间|发布时间|截止时间|结束时间|所属课程|课程名称|满分|成绩|状态)\\s*[:：]?/;

                for (const line of lines) {
                    if (line.length < 2 || line.length > 160 || ignored.test(line) || labelLine.test(line)) continue;
                    if (keyword.test(line)) return line;
                }
                for (const line of lines) {
                    if (line.length < 2 || line.length > 160 || ignored.test(line) || labelLine.test(line)) continue;
                    return line;
                }
                return kind === 'exam' ? '未命名考试' : '未命名作业';
            };

            const extractUrl = (node) => {
                const links = Array.from(node.querySelectorAll('a[href]'));
                const preferred = links.find(link => {
                    const value = `${link.href || ''} ${link.innerText || ''} ${link.getAttribute('onclick') || ''}`;
                    return matchesKind(value);
                }) || links[0] || (node.matches('a[href]') ? node : null);
                if (!preferred) return '';
                try {
                    return new URL(preferred.href || preferred.getAttribute('href') || '', location.href).href;
                } catch (e) {
                    return preferred.href || preferred.getAttribute('href') || '';
                }
            };

            const pushItem = (node) => {
                if (!node || !isVisible(node)) return;
                const rawText = clean(node.innerText || node.textContent || '');
                if (rawText.length < 2 || rawText.length > 1800) return;
                const url = extractUrl(node);
                const actionText = clean([
                    rawText,
                    url,
                    node.className || '',
                    node.getAttribute('onclick') || '',
                ].join(' '));
                if (!matchesKind(actionText)) return;

                const rect = node.getBoundingClientRect();
                if (rawText.length > 600 && rect.height > window.innerHeight * 0.75) return;
                if (/我的课程|个人空间|消息中心|退出登录/.test(rawText) && rawText.length < 80) return;

                const range = findRange(rawText);
                const startTime = findByLabels(
                    rawText,
                    ['开始时间', '开考时间', '考试时间', '发布时间', '发布日期', '开放时间']
                ) || range.start;
                const endTime = findByLabels(
                    rawText,
                    ['截止时间', '截止日期', '作业截止', '提交截止', '考试截止', '结束时间', '结束日期']
                ) || range.end;
                const statusInfo = classifyStatus(rawText);

                result.push({
                    item_type: kind,
                    title: extractTitle(node, rawText),
                    course_name: extractCourseName(rawText),
                    start_time: startTime,
                    end_time: endTime,
                    status: statusInfo.status,
                    status_text: statusInfo.statusText,
                    url,
                    raw_text: rawText,
                    source_url: location.href,
                });
            };

            const nodes = new Set();
            const selectors = [
                'tr',
                'li',
                'dd',
                '.work-item',
                '.homework-item',
                '.exam-item',
                '.task-item',
                '.list-item',
                '.workList li',
                '.examList li',
                '[class*="homework"]',
                '[class*="HomeWork"]',
                '[class*="workItem"]',
                '[class*="work-item"]',
                '[class*="examItem"]',
                '[class*="exam-item"]',
                '[class*="paperItem"]',
                '[class*="paper-item"]'
            ];
            selectors.forEach(selector => {
                document.querySelectorAll(selector).forEach(node => nodes.add(node));
            });
            document.querySelectorAll('a[href],button,[onclick]').forEach(node => {
                const text = clean([
                    node.innerText || node.textContent || '',
                    node.href || '',
                    node.getAttribute('onclick') || '',
                ].join(' '));
                if (!matchesKind(text)) return;
                nodes.add(node.closest('tr,li,dd,.work-item,.homework-item,.exam-item,.task-item,.list-item') || node);
            });

            nodes.forEach(pushItem);
            return result;
        }
    """

    def __init__(
        self,
        page: Page,
        status_callback: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.page = page
        self.status_callback = status_callback
        self.should_stop = should_stop

    def get_pending_exams(self, courses: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """查询所有课程中尚未完成、尚未截止的考试。"""

        course_items = self._exam_items_from_course_cards(courses)
        scanned = self._scan_courses(courses, kind="exam")
        return [item for item in self._merge_items(course_items + scanned) if self._is_pending(item)]

    def get_pending_homeworks(self, courses: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """查询所有课程中待完成的作业。"""

        scanned = self._scan_courses(courses, kind="homework")
        return [item for item in self._merge_items(scanned) if self._is_pending(item)]

    def get_homeworks_by_course(self, courses: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """查询所有作业，保留课程名称供 GUI 按课程展示。"""

        return self._merge_items(self._scan_courses(courses, kind="homework"))

    def _scan_courses(self, courses: List[Dict[str, str]], *, kind: str) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        total = len(courses)
        label = "考试" if kind == "exam" else "作业"

        for index, course in enumerate(courses, start=1):
            if self.should_stop is not None and self.should_stop():
                self._notify(f"已停止查询{label}列表")
                break

            course_name = str(course.get("name") or f"课程 {index}").strip()
            self._notify(f"正在查询课程 [{index}/{total}] 的{label}: {course_name}")
            try:
                course_manager = CourseManager(self.page)
                if not str(course.get("url") or "").strip():
                    self.page.goto(COURSE_LIST_URL, wait_until="domcontentloaded", timeout=WAIT_TIME)
                    wait_page_ready(self.page, WAIT_TIME)
                opened = course_manager.open_course_item(course)
                course_name = str(opened.get("name") or course_name).strip()
                clicked = self._open_task_entry(kind)
                if not clicked:
                    logger.info("课程 [%s] 未找到明确的%s入口，直接检查当前页和 iframe", course_name, label)
                items = self._extract_items(kind=kind, course_name=course_name)
                for item in items:
                    item["course_name"] = str(item.get("course_name") or course_name)
                    item["course_id"] = str(course.get("course_id") or "")
                    item["clazzid"] = str(course.get("clazzid") or course.get("class_id") or "")
                    item["cpi"] = str(course.get("cpi") or "")
                logger.info("课程 [%s] 获取%s数量: %s", course_name, label, len(items))
                results.extend(items)
            except Exception as exc:  # noqa: BLE001 - 单门课程失败不应中断所有课程查询
                logger.warning("课程 [%s] 查询%s失败，继续下一门: %s", course_name, label, exc)
                save_screenshot(self.page, f"error_query_{kind}_{index}")

        return results

    def _open_task_entry(self, kind: str) -> bool:
        labels = self.EXAM_LABELS if kind == "exam" else self.HOMEWORK_LABELS
        for frame in self._frames():
            previous_pages = list(self.page.context.pages)
            try:
                result = frame.evaluate(self._CLICK_ENTRY_SCRIPT, {"labels": list(labels)})
            except Exception as exc:  # noqa: BLE001 - 某个 iframe 不可用时继续尝试其它 frame
                logger.debug("检查课程%s入口 frame 失败: %s", "考试" if kind == "exam" else "作业", exc)
                continue
            if not isinstance(result, dict) or not result.get("ok"):
                continue

            logger.info("已点击课程%s入口: %s", "考试" if kind == "exam" else "作业", result.get("text"))
            self.page.wait_for_timeout(1200)
            new_pages = [page for page in self.page.context.pages if page not in previous_pages]
            if new_pages:
                self.page = new_pages[-1]
                logger.info("课程%s入口打开了新页面，切换到: %s", "考试" if kind == "exam" else "作业", self.page.url)
            try:
                wait_page_ready(self.page, min(WAIT_TIME, 12000))
            except Exception:
                pass
            return True
        return False

    def _extract_items(self, *, kind: str, course_name: str) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        for frame in self._frames():
            try:
                frame_items = frame.evaluate(
                    self._EXTRACT_ITEMS_SCRIPT,
                    {"kind": kind, "fallbackCourseName": course_name},
                )
            except Exception as exc:  # noqa: BLE001 - 部分动态 iframe 可能在查询时被替换
                logger.debug("读取%s列表 frame 失败: %s", "考试" if kind == "exam" else "作业", exc)
                continue
            if not isinstance(frame_items, list):
                continue
            for item in frame_items:
                if isinstance(item, dict):
                    items.append({str(key): str(value or "") for key, value in item.items()})
        return self._merge_items(items)

    def _frames(self) -> List[Frame]:
        """返回当前仍可访问的 frame，主 frame 优先。"""

        frames = list(self.page.frames)
        main_frame = self.page.main_frame
        return [main_frame] + [frame for frame in frames if frame != main_frame]

    @staticmethod
    def _exam_items_from_course_cards(courses: List[Dict[str, str]]) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        for course in courses:
            exam_status = str(course.get("exam_status") or course.get("has_exam") or "").strip()
            raw_text = str(course.get("raw_text") or "")
            if not exam_status or exam_status in {"无考试", "否"}:
                continue
            items.append(
                {
                    "item_type": "exam",
                    "title": f"{course.get('name') or '未命名课程'} - 考试",
                    "course_name": str(course.get("name") or ""),
                    "course_id": str(course.get("course_id") or ""),
                    "start_time": str(course.get("exam_start_time") or ""),
                    "end_time": str(course.get("exam_end_time") or ""),
                    "status": CourseTaskQueryManager._normalize_status(exam_status, raw_text),
                    "status_text": exam_status,
                    "url": str(course.get("url") or ""),
                    "raw_text": clean_text(f"{exam_status} {raw_text}"),
                    "source_url": str(course.get("url") or ""),
                }
            )
        return items

    @staticmethod
    def _normalize_status(status_text: str, raw_text: str = "") -> str:
        text = clean_text(f"{status_text} {raw_text}")
        if re.search(r"已截止|已结束|已过期|已关闭|逾期|过期", text):
            return "expired"
        if re.search(r"待考试|待考|未开考|未考试|未开始|待做|待完成|未完成|未提交|未作答|待提交", text):
            return "pending"
        if re.search(r"进行中|正在考试|正在进行|作答中|可提交|可考试", text):
            return "running"
        if re.search(r"已提交|已完成|已批阅|已交卷|已考试|已作答|查看成绩", text):
            return "completed"
        return "unknown"

    @staticmethod
    def _is_pending(item: Dict[str, str]) -> bool:
        status = str(item.get("status") or "").strip().lower()
        if status in {"pending", "running"}:
            return True
        if status in {"completed", "expired"}:
            return False

        text = clean_text(
            " ".join(
                [
                    str(item.get("status_text") or ""),
                    str(item.get("title") or ""),
                    str(item.get("raw_text") or ""),
                ]
            )
        )
        normalized = CourseTaskQueryManager._normalize_status(text)
        if normalized in {"pending", "running"}:
            return True
        if normalized in {"completed", "expired"}:
            return False
        # 页面只写了“考试/作业”但没有状态时保留，避免漏掉没有状态字段的待处理项。
        return bool(re.search(r"考试|测验|试卷|作业|练习|任务", text))

    @staticmethod
    def _merge_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        result: List[Dict[str, str]] = []
        index_by_key: Dict[tuple[str, ...], int] = {}
        for item in items:
            normalized = {str(key): str(value or "").strip() for key, value in item.items()}
            key = (
                normalized.get("item_type", ""),
                normalized.get("course_name", ""),
                normalized.get("title", ""),
                normalized.get("end_time", ""),
                normalized.get("url", ""),
            )
            if not any(key):
                continue
            if key not in index_by_key:
                index_by_key[key] = len(result)
                result.append(normalized)
                continue

            existing = result[index_by_key[key]]
            for field, value in normalized.items():
                if value and not existing.get(field):
                    existing[field] = value
                elif field == "raw_text" and len(value) > len(existing.get(field, "")):
                    existing[field] = value
        return result

    def _notify(self, message: str) -> None:
        logger.info(message)
        if self.status_callback is not None:
            try:
                self.status_callback(message)
            except Exception:
                logger.exception("考试/作业查询状态回调执行失败")


def clean_text(value: str) -> str:
    """压缩文本中的空白字符。"""

    return re.sub(r"\s+", " ", value or "").strip()
