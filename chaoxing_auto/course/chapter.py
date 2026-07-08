"""章节处理模块。"""

from __future__ import annotations

from typing import Dict, List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from config.config import WAIT_TIME
from utils.helper import deduplicate_items, save_screenshot, wait_page_ready
from utils.logger import get_logger

logger = get_logger()


class ChapterError(RuntimeError):
    """章节处理异常。"""


class ChapterManager:
    """负责获取章节列表和打开章节。"""

    def __init__(self, page: Page) -> None:
        self.page = page

    def get_chapters(self) -> List[Dict[str, str]]:
        """获取课程章节列表。"""

        try:
            logger.info("开始获取章节列表")
            self.page.wait_for_selector("body", timeout=WAIT_TIME)
            wait_page_ready(self.page, WAIT_TIME)

            chapters = self.page.evaluate(
                """
                () => {
                    const result = [];
                    const pushChapter = (title, url) => {
                        title = (title || '').replace(/\\s+/g, ' ').trim();
                        url = (url || '').trim();
                        if (/^(javascript:|#)/i.test(url)) url = '';
                        if (!title || title.length < 2) return;
                        if (/首页|讨论|通知|作业|考试|统计|资料|更多|返回|退出/.test(title)) return;
                        result.push({ title, url });
                    };

                    const selectors = [
                        'a[href*="knowledge"]',
                        'a[href*="studentstudy"]',
                        'a[href*="mooc2"]',
                        'a[href*="jobid"]',
                        'a[href*="chapter"]',
                        '.chapter a',
                        '.chapter_item a',
                        '.chapterItem a',
                        '.catalog a',
                        '.catalogue a',
                        '.units a',
                        '.leveltwo a',
                        '.clearfix a'
                    ];
                    const nodes = new Set();
                    selectors.forEach(selector => {
                        document.querySelectorAll(selector).forEach(node => nodes.add(node));
                    });

                    nodes.forEach(node => {
                        const text = node.innerText || node.textContent || node.getAttribute('title') || '';
                        let href = node.href || node.getAttribute('data') || node.getAttribute('data-url') || '';
                        try { href = href ? new URL(href, location.href).href : ''; } catch (e) {}
                        const classText = `${node.className || ''} ${node.parentElement ? node.parentElement.className : ''}`;
                        const looksLikeChapter = /章|节|课时|任务点|视频|[0-9]+\\.[0-9]+/.test(text)
                            || /knowledge|studentstudy|chapter|jobid|mooc2/i.test(href)
                            || /chapter|catalog|units|level/i.test(classText);
                        if (looksLikeChapter) pushChapter(text, href);
                    });

                    // 章节标题不一定是链接，尝试寻找最近的可点击链接。
                    document.querySelectorAll('.chapter,.chapter_item,.chapterItem,.catalog,.catalogue,.units,.leveltwo').forEach(item => {
                        const text = item.innerText || item.textContent || '';
                        const link = item.querySelector('a[href]');
                        let href = link ? link.href : '';
                        try { href = href ? new URL(href, location.href).href : ''; } catch (e) {}
                        const firstLine = text.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
                        if (firstLine) pushChapter(firstLine, href);
                    });

                    return result;
                }
                """
            )
            cleaned = deduplicate_items(chapters, key_fields=("title", "url"))
            logger.info("获取章节数量: %s", len(cleaned))
            for chapter in cleaned:
                logger.info("章节: %s -> %s", chapter.get("title"), chapter.get("url"))
            return cleaned
        except PlaywrightTimeoutError as exc:
            save_screenshot(self.page, "error_get_chapters_timeout")
            logger.exception("获取章节列表超时: %s", exc)
            raise ChapterError(f"获取章节列表超时: {exc}") from exc
        except PlaywrightError as exc:
            save_screenshot(self.page, "error_get_chapters_playwright")
            logger.exception("获取章节列表失败: %s", exc)
            raise ChapterError(f"获取章节列表失败: {exc}") from exc
        except Exception as exc:
            save_screenshot(self.page, "error_get_chapters")
            logger.exception("获取章节列表异常: %s", exc)
            raise

    def open_chapter(self, chapter: Dict[str, str] | str) -> Dict[str, str]:
        """打开指定章节。

        Args:
            chapter: 可以是 {"title": "", "url": ""}，也可以是章节标题字符串。
        """

        try:
            if isinstance(chapter, str):
                title = chapter
                url = ""
            else:
                title = chapter.get("title", "")
                url = chapter.get("url", "")

            if not title and not url:
                raise ChapterError("章节标题和链接均为空")

            logger.info("打开章节: %s", title or url)
            if url:
                self.page.goto(url, wait_until="domcontentloaded", timeout=WAIT_TIME)
            else:
                self.page.get_by_text(title, exact=False).first.click()

            wait_page_ready(self.page, WAIT_TIME)
            logger.info("章节页面加载完成: %s", self.page.url)
            return {"title": title, "url": url or self.page.url}
        except PlaywrightTimeoutError as exc:
            save_screenshot(self.page, "error_open_chapter_timeout")
            logger.exception("打开章节超时: %s", exc)
            raise ChapterError(f"打开章节超时: {exc}") from exc
        except Exception as exc:
            save_screenshot(self.page, "error_open_chapter")
            logger.exception("打开章节失败: %s", exc)
            raise

    @staticmethod
    def find_chapter(chapters: List[Dict[str, str]], title: str) -> Optional[Dict[str, str]]:
        """按标题查找章节。"""

        normalized = title.strip().lower()
        for chapter in chapters:
            if chapter.get("title", "").strip().lower() == normalized:
                return chapter
        for chapter in chapters:
            name = chapter.get("title", "").strip().lower()
            if normalized in name or name in normalized:
                return chapter
        return None
