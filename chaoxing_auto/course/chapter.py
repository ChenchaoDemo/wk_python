"""章节处理模块。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
                    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const currentUrl = new URL(location.href);
                    const getParamFromUrl = (url, key) => {
                        if (!url) return '';
                        try {
                            const parsed = new URL(url, location.href);
                            return parsed.searchParams.get(key) || '';
                        } catch (e) {
                            const match = String(url).match(new RegExp(`${key}\\\\s*[=:]\\\\s*([0-9A-Za-z_-]+)`, 'i'));
                            return match ? match[1] : '';
                        }
                    };
                    const getCurrentParam = (key) => currentUrl.searchParams.get(key) || '';
                    const extractIds = (rawUrl, onclick, node) => {
                        const text = [rawUrl, onclick, node ? (node.outerHTML || '') : ''].join(' ');
                        const ids = {
                            course_id: getParamFromUrl(rawUrl, 'courseId') || getCurrentParam('courseId'),
                            clazzid: getParamFromUrl(rawUrl, 'clazzid') || getCurrentParam('clazzid'),
                            chapter_id: getParamFromUrl(rawUrl, 'chapterId'),
                            cpi: getParamFromUrl(rawUrl, 'cpi') || getCurrentParam('cpi'),
                        };

                        const ajaxMatch = text.match(/getTeacherAjax\\s*\\(\\s*['"]?([0-9A-Za-z_-]+)['"]?\\s*,\\s*['"]?([0-9A-Za-z_-]+)['"]?\\s*,\\s*['"]?([0-9A-Za-z_-]+)['"]?/i);
                        if (ajaxMatch) {
                            ids.course_id = ids.course_id || ajaxMatch[1];
                            ids.clazzid = ids.clazzid || ajaxMatch[2];
                            ids.chapter_id = ids.chapter_id || ajaxMatch[3];
                        }

                        if (!ids.chapter_id && node) {
                            const curNode = node.closest('[id^="cur"]');
                            const idMatch = curNode && String(curNode.id || '').match(/^cur([0-9A-Za-z_-]+)$/i);
                            if (idMatch) ids.chapter_id = idMatch[1];
                        }

                        if (!ids.chapter_id) {
                            const match = text.match(/chapterId\\s*[=:]\\s*([0-9A-Za-z_-]+)/i);
                            if (match) ids.chapter_id = match[1];
                        }
                        if (!ids.course_id) {
                            const match = text.match(/courseId\\s*[=:]\\s*([0-9A-Za-z_-]+)/i);
                            if (match) ids.course_id = match[1];
                        }
                        if (!ids.clazzid) {
                            const match = text.match(/clazzid\\s*[=:]\\s*([0-9A-Za-z_-]+)/i);
                            if (match) ids.clazzid = match[1];
                        }
                        if (!ids.cpi) {
                            const match = text.match(/cpi\\s*[=:]\\s*([0-9A-Za-z_-]+)/i);
                            if (match) ids.cpi = match[1];
                        }
                        return ids;
                    };
                    const getClassText = (node) => {
                        if (!node) return '';
                        return [
                            node.className || '',
                            node.getAttribute ? (node.getAttribute('title') || '') : '',
                            node.getAttribute ? (node.getAttribute('aria-label') || '') : '',
                        ].join(' ');
                    };
                    const getStatusContainer = (node) => {
                        return node
                            ? (node.closest('h3.clearfix,h3,.posCatalog_select,.ncells,h4[id^="cur"],div[id^="cur"],li[id^="cur"],dd[id^="cur"],.chapter,.chapter_item,.chapterItem,.catalog,.catalogue,.units,.leveltwo,li,dd,dt') || node)
                            : null;
                    };
                    const getStatusInfo = (node) => {
                        // 新页面规则：
                        //   <em class="openlock"></em> => 当前章节已完成
                        //   没有 openlock              => 未完成，后续进入卡片/视频判断
                        // 页面规则：
                        //   roundpointStudent blue                  => 已完成
                        //   roundpointStudent noJob                 => 未完成/无任务
                        //   roundpointStudent orange01 a002 jobCount => 未完成
                        const container = node
                            ? getStatusContainer(node)
                            : null;
                        const openlock = container ? container.querySelector('em.openlock,.openlock') : null;
                        if (openlock) {
                            const classText = clean(openlock.className || '');
                            const statusText = clean(`${openlock.outerHTML || ''} ${classText}`);
                            return {
                                completed: true,
                                source: 'page_openlock',
                                statusClass: classText,
                                statusDecision: 'em.openlock => 已完成，登记为已完成，不加入待刷列表',
                                statusText,
                            };
                        }

                        const point = container ? container.querySelector('.roundpointStudent') : null;
                        if (point) {
                            const classText = clean(point.className || '');
                            const statusText = clean(`${point.outerHTML || ''} ${classText} ${point.innerText || point.textContent || ''}`);
                            const isIncomplete = /(^|\\s)(noJob|orange01|a002|jobCount)(\\s|$)/i.test(classText);
                            const isCompleted = /(^|\\s)blue(\\s|$)/i.test(classText) && !isIncomplete;
                            let statusDecision = 'roundpointStudent 其它状态 => 未完成，进入卡片/视频判断';
                            if (isCompleted) {
                                statusDecision = 'roundpointStudent blue => 已完成，直接跳过到下一节';
                            } else if (/(^|\\s)noJob(\\s|$)/i.test(classText)) {
                                statusDecision = 'roundpointStudent noJob => 未完成（没有工作），进入卡片/视频判断';
                            } else if (/(^|\\s)(orange01|a002|jobCount)(\\s|$)/i.test(classText)) {
                                statusDecision = 'roundpointStudent orange01/a002/jobCount => 未完成，进入卡片/视频判断';
                            }
                            return {
                                completed: isCompleted,
                                source: 'page_roundpoint',
                                statusClass: classText,
                                statusDecision,
                                statusText,
                            };
                        }

                        // 兼容少数旧页面的“已完成”图标；没有明确完成标记时一律未完成。
                        const indicators = container
                            ? Array.from(container.querySelectorAll('.icon_Completed,.ans-job-finished,.job-finished,.jobFinish,[title="已完成"],[aria-label="已完成"],[title*="任务点已完成"],[aria-label*="任务点已完成"]'))
                            : [];
                        const statusText = [
                            indicators.map(indicator => `${indicator.innerText || indicator.textContent || ''} ${indicator.outerHTML || ''} ${getClassText(indicator)}`).join(' '),
                            node ? getClassText(node) : '',
                        ].join(' ');
                        return {
                            completed: indicators.length > 0,
                            source: indicators.length > 0 ? 'page_explicit_completed' : 'page_no_completed_marker',
                            statusClass: '',
                            statusDecision: indicators.length > 0
                                ? '旧页面明确已完成图标 => 已完成，直接跳过到下一节'
                                : '未找到 roundpointStudent/明确完成图标 => 未完成，进入卡片/视频判断',
                            statusText,
                        };
                    };
                    const pushChapter = (title, url, node = null, extra = {}) => {
                        title = (title || '').replace(/\\s+/g, ' ').trim();
                        const rawUrl = (url || '').trim();
                        const rawOnclick = extra.onclick || (node && node.getAttribute ? clean(node.getAttribute('onclick') || '') : '');
                        const ids = extractIds(rawUrl, rawOnclick, node);
                        url = rawUrl;
                        if (/^(javascript:|#)/i.test(url)) url = '';
                        if (!title || title.length < 2) return;
                        if (/首页|讨论|通知|作业|考试|统计|资料|更多|返回|退出/.test(title)) return;
                        const statusInfo = getStatusInfo(node);
                        const actionText = `${rawUrl} ${rawOnclick} ${node ? (node.outerHTML || '') : ''}`;
                        const hasLearningAction = /getTeacherAjax|studentstudy|chapterId|knowledge|jobid|mooc2/i.test(actionText);
                        const hasPageStatus = statusInfo.source === 'page_openlock'
                            || statusInfo.source === 'page_roundpoint'
                            || statusInfo.source === 'page_explicit_completed';
                        // 过滤“第1章/第2章”这类父级标题：它们没有 roundpointStudent，也没有真正的学习入口，
                        // 否则会被当成章节打开后出现“章节视频不存在”。
                        if (!hasLearningAction && !hasPageStatus) return;
                        const container = node ? node.closest('.posCatalog_select,h4[id^="cur"],div[id^="cur"],li[id^="cur"],dd[id^="cur"]') : null;
                        result.push({
                            title,
                            url,
                            course_id: ids.course_id,
                            clazzid: ids.clazzid,
                            chapter_id: ids.chapter_id,
                            cpi: ids.cpi,
                            element_id: extra.element_id || (node ? clean(node.id || '') : ''),
                            container_id: extra.container_id || (container ? clean(container.id || '') : ''),
                            onclick: rawOnclick,
                            completed: statusInfo.completed ? 'true' : 'false',
                            status_source: statusInfo.source,
                            status_class: clean(statusInfo.statusClass || ''),
                            status_decision: clean(statusInfo.statusDecision || ''),
                            status_text: clean(statusInfo.statusText),
                        });
                    };

                    const selectors = [
                        '.posCatalog_select .posCatalog_name',
                        '.posCatalog_name[onclick]',
                        'span[onclick*="getTeacherAjax"]',
                        'a[href*="getTeacherAjax"]',
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
                        const text = node.getAttribute('title')
                            || (node.querySelector && node.querySelector('.articlename[title]') ? node.querySelector('.articlename[title]').getAttribute('title') : '')
                            || node.innerText
                            || node.textContent
                            || '';
                        const onclick = node.getAttribute('onclick') || node.getAttribute('href') || '';
                        let href = node.href || node.getAttribute('href') || node.getAttribute('data') || node.getAttribute('data-url') || '';
                        try { href = href ? new URL(href, location.href).href : ''; } catch (e) {}
                        const classText = `${node.className || ''} ${node.parentElement ? node.parentElement.className : ''}`;
                        const looksLikeChapter = node.classList.contains('posCatalog_name')
                            || /章|节|课时|任务点|视频|[0-9]+\\.[0-9]+/.test(text)
                            || /knowledge|studentstudy|chapter|jobid|mooc2/i.test(href)
                            || /getTeacherAjax/i.test(onclick)
                            || /chapter|catalog|units|level/i.test(classText);
                        if (looksLikeChapter) {
                            const container = node.closest('.posCatalog_select,h4[id^="cur"],div[id^="cur"],li[id^="cur"],dd[id^="cur"]');
                            pushChapter(text, href, node, {
                                container_id: container ? clean(container.id || '') : '',
                                onclick,
                            });
                        }
                    });

                    // 章节标题不一定是链接，尝试寻找最近的可点击链接。
                    document.querySelectorAll('.chapter,.chapter_item,.chapterItem,.catalog,.catalogue,.units,.leveltwo,.ncells,h4[id^="cur"]').forEach(item => {
                        const text = item.innerText || item.textContent || '';
                        const link = item.querySelector('a[href]');
                        const clickable = item.querySelector('[onclick*="getTeacherAjax"]') || link;
                        let href = link ? (link.href || link.getAttribute('href') || '') : '';
                        const onclick = clickable ? (clickable.getAttribute('onclick') || clickable.getAttribute('href') || '') : '';
                        try { href = href ? new URL(href, location.href).href : ''; } catch (e) {}
                        const firstLine = text.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
                        if (firstLine) pushChapter(firstLine, href, item, { onclick });
                    });

                    return result;
                }
                """
            )
            cleaned = deduplicate_items(chapters, key_fields=("chapter_id", "title", "url"))
            logger.info("获取章节数量: %s", len(cleaned))
            for chapter in cleaned:
                logger.info(
                    "章节: %s chapterId=%s completed=%s source=%s class=%s decision=%s status=%s -> %s",
                    chapter.get("title"),
                    chapter.get("chapter_id") or "未显示",
                    chapter.get("completed"),
                    chapter.get("status_source") or "未显示",
                    chapter.get("status_class") or "未显示",
                    chapter.get("status_decision") or "未显示",
                    chapter.get("status_text") or "未显示",
                    chapter.get("url"),
                )
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
                element_id = ""
                container_id = ""
            else:
                title = chapter.get("title", "")
                url = chapter.get("url", "")
                element_id = chapter.get("element_id", "")
                container_id = chapter.get("container_id", "")

            if not title and not url:
                raise ChapterError("章节标题和链接均为空")

            logger.info("打开章节: %s", title or url)
            if url:
                self.page.goto(url, wait_until="domcontentloaded", timeout=WAIT_TIME)
            elif element_id:
                self.page.locator(f"#{element_id}").first.click(timeout=WAIT_TIME, force=True)
            elif container_id:
                self.page.locator(f"#{container_id} .posCatalog_name, #{container_id}").first.click(
                    timeout=WAIT_TIME,
                    force=True,
                )
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

    def get_cards(self, retry: bool = True) -> List[Dict[str, str]]:
        """获取当前章节页里的卡片/页签列表。

        学习通有些章节不是单一页面，而是在章节页内使用 `.tabtags` 卡片切换内容，
        例如“目标及任务 / 学习内容 / 实训 / 测验 / 主题讨论”。这些卡片可能各自
        包含视频或任务点，因此需要逐个点击处理。
        """

        try:
            cards = self.page.evaluate(
                """
                () => {
                    const result = [];
                    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const detectCompleted = (text) => {
                        text = clean(text);
                        if (!text) return false;
                        return /已完成|任务点已完成/.test(text)
                            || /icon_Completed|ans-job-finished|job-finished|jobFinish/i.test(text);
                    };
                    const getStatusText = (node) => {
                        const indicators = node
                            ? Array.from(node.querySelectorAll('.icon_Completed,.ans-job-finished,.job-finished,.jobFinish,[title="已完成"],[aria-label="已完成"],[title*="任务点已完成"],[aria-label*="任务点已完成"]'))
                            : [];
                        return [
                            node ? (node.innerText || node.textContent || '') : '',
                            node ? (node.className || '') : '',
                            node ? (node.getAttribute('title') || '') : '',
                            node ? (node.getAttribute('aria-label') || '') : '',
                            indicators.map(indicator => `${indicator.outerHTML || ''} ${indicator.className || ''}`).join(' '),
                        ].join(' ');
                    };
                    const pushCard = (node, fallbackIndex) => {
                        const title = clean(node.getAttribute('title') || node.innerText || node.textContent || `卡片 ${fallbackIndex}`);
                        const cardid = clean(node.getAttribute('cardid') || node.getAttribute('data-cardid') || '');
                        const id = clean(node.id || '');
                        const onclick = clean(node.getAttribute('onclick') || '');
                        const className = clean(node.className || '');
                        const statusText = getStatusText(node);
                        if (!title || /上一节|下一节/.test(title)) return;
                        result.push({
                            title,
                            cardid,
                            id,
                            onclick,
                            class_name: className,
                            index: String(fallbackIndex),
                            completed: detectCompleted(statusText) ? 'true' : 'false',
                            status_text: clean(statusText),
                        });
                    };

                    const nodes = [];
                    document.querySelectorAll('.tabtags span[cardid], .tabtags span[id^="dct"], .tabtags span[onclick*="changeDisplayContent"]').forEach(node => {
                        if (!nodes.includes(node)) nodes.push(node);
                    });
                    document.querySelectorAll('span[cardid][onclick*="changeDisplayContent"]').forEach(node => {
                        if (!nodes.includes(node)) nodes.push(node);
                    });

                    nodes.forEach((node, index) => pushCard(node, index + 1));
                    return result;
                }
                """
            )
            cleaned = deduplicate_items(cards, key_fields=("title", "cardid", "id"))
            if cleaned:
                logger.info("当前章节检测到卡片数量: %s", len(cleaned))
                for card in cleaned:
                    logger.info(
                        "章节卡片: [%s] %s cardid=%s id=%s completed=%s",
                        card.get("index"),
                        card.get("title"),
                        card.get("cardid"),
                        card.get("id"),
                        card.get("completed"),
                    )
            return cleaned
        except Exception as exc:
            if retry and "Execution context was destroyed" in str(exc):
                logger.info("页面正在跳转，等待后重试获取章节卡片")
                try:
                    wait_page_ready(self.page, WAIT_TIME)
                    self.page.wait_for_timeout(1500)
                except Exception:
                    pass
                return self.get_cards(retry=False)
            logger.warning("获取章节卡片失败，按普通章节继续处理: %s", exc)
            return []

    def _activate_card_by_dom(
        self,
        *,
        title: str,
        cardid: str,
        element_id: str,
        index: str,
    ) -> Dict[str, Any]:
        """通过页面 DOM/JS 激活章节卡片，兼容隐藏的 `.tabtags` 页签。"""

        return self.page.evaluate(
            """
            ({ title, cardid, elementId, index }) => {
                const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const candidates = [];
                const add = (node, source) => {
                    if (node && !candidates.some(item => item.node === node)) {
                        candidates.push({ node, source });
                    }
                };
                const isVisible = (node) => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) > 0
                        && rect.width > 0
                        && rect.height > 0;
                };
                const isActive = (node) => {
                    const className = ` ${node.className || ''} `;
                    return /\\s(currents?|active|selected|on)\\s/i.test(className);
                };
                const cardNodes = () => Array.from(document.querySelectorAll([
                    '.tabtags span[cardid]',
                    '.tabtags span[id^="dct"]',
                    '.tabtags span[onclick*="changeDisplayContent"]',
                    'span[cardid][onclick*="changeDisplayContent"]'
                ].join(',')));

                if (elementId) {
                    add(document.getElementById(elementId), 'id');
                }
                if (cardid) {
                    cardNodes()
                        .filter(node => clean(node.getAttribute('cardid') || node.getAttribute('data-cardid') || '') === cardid)
                        .forEach(node => add(node, 'cardid'));
                }
                if (title) {
                    const wanted = clean(title);
                    cardNodes()
                        .filter(node => {
                            const text = clean(node.getAttribute('title') || node.innerText || node.textContent || '');
                            return text === wanted || text.includes(wanted) || wanted.includes(text);
                        })
                        .forEach(node => add(node, 'title'));
                }
                if (index) {
                    const numericIndex = Number.parseInt(index, 10);
                    const nodes = cardNodes();
                    if (Number.isFinite(numericIndex) && numericIndex > 0 && numericIndex <= nodes.length) {
                        add(nodes[numericIndex - 1], 'index');
                    }
                }

                if (!candidates.length) {
                    return {
                        ok: false,
                        reason: 'not_found',
                        method: '',
                        visible: 'false',
                        active: 'false',
                    };
                }

                const { node, source } = candidates[0];
                const visible = isVisible(node);
                const active = isActive(node);

                try {
                    if (typeof node.click === 'function') {
                        node.click();
                    } else {
                        node.dispatchEvent(new MouseEvent('click', {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                        }));
                    }
                    return {
                        ok: true,
                        reason: active ? 'active_dom_click' : 'dom_click',
                        method: source,
                        visible: String(visible),
                        active: String(active),
                    };
                } catch (clickError) {
                    const onclick = node.getAttribute('onclick') || '';
                    if (onclick && typeof node.onclick === 'function') {
                        try {
                            node.onclick.call(node, new MouseEvent('click', {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                            }));
                            return {
                                ok: true,
                                reason: 'inline_onclick',
                                method: source,
                                visible: String(visible),
                                active: String(active),
                            };
                        } catch (inlineError) {
                            return {
                                ok: false,
                                reason: `click_failed: ${clickError.message || clickError}; onclick_failed: ${inlineError.message || inlineError}`,
                                method: source,
                                visible: String(visible),
                                active: String(active),
                            };
                        }
                    }
                    return {
                        ok: false,
                        reason: `click_failed: ${clickError.message || clickError}`,
                        method: source,
                        visible: String(visible),
                        active: String(active),
                    };
                }
            }
            """,
            {
                "title": title,
                "cardid": cardid,
                "elementId": element_id,
                "index": index,
            },
        )

    def open_card(self, card: Dict[str, str]) -> Dict[str, str]:
        """点击当前章节内的指定卡片。"""

        title = (card.get("title") or "").strip()
        cardid = (card.get("cardid") or "").strip()
        element_id = (card.get("id") or "").strip()
        index = (card.get("index") or "").strip()

        try:
            target = title or cardid or element_id or index
            logger.info("打开章节卡片: %s", target)
            result = self._activate_card_by_dom(
                title=title,
                cardid=cardid,
                element_id=element_id,
                index=index,
            )
            if not result.get("ok"):
                raise ChapterError(f"未找到可点击的章节卡片: {target} ({result.get('reason')})")

            logger.info(
                "章节卡片已激活: %s method=%s reason=%s visible=%s active=%s",
                target,
                result.get("method") or "unknown",
                result.get("reason") or "unknown",
                result.get("visible") or "unknown",
                result.get("active") or "unknown",
            )

            # 卡片切换多数是页面内 JS 动态加载，不一定触发导航；这里给 DOM/iframe 一点刷新时间。
            self.page.wait_for_timeout(1200)
            try:
                self.page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                logger.debug("章节卡片切换后等待 networkidle 超时，继续处理")

            return {"title": title, "cardid": cardid, "id": element_id, "index": index}
        except PlaywrightTimeoutError as exc:
            save_screenshot(self.page, "error_open_card_timeout")
            logger.exception("打开章节卡片超时: %s", exc)
            raise ChapterError(f"打开章节卡片超时: {exc}") from exc
        except Exception as exc:
            save_screenshot(self.page, "error_open_card")
            logger.exception("打开章节卡片失败: %s", exc)
            raise

    @staticmethod
    def is_completed_item(item: Dict[str, str]) -> bool:
        """判断章节/卡片条目是否已完成。"""

        completed = str(item.get("completed") or "").strip().lower()
        if completed in {"1", "true", "yes", "y"}:
            return True

        status_text = str(item.get("status_text") or "")
        if "openlock" in status_text:
            return True

        if "roundpointStudent" in status_text:
            has_blue = "blue" in status_text
            has_incomplete = any(word in status_text for word in ("noJob", "orange01", "a002", "jobCount"))
            return has_blue and not has_incomplete

        return any(word in status_text for word in ("已完成", "任务点已完成", "icon_Completed", "ans-job-finished"))

    def is_current_content_completed(self) -> bool:
        """检测当前打开的章节/卡片内容是否已完成。

        该方法只做保守判断：优先看当前卡片、任务点图标、完成状态类名，不依赖整页普通文字，
        避免因为页面其它位置存在“已完成”而误跳过。
        """

        try:
            return bool(
                self.page.evaluate(
                    """
                    () => {
                        const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && Number(style.opacity || 1) > 0
                                && rect.width > 0
                                && rect.height > 0;
                        };
                        const detectCompleted = (text) => {
                            text = clean(text);
                            if (!text) return false;
                            const hasIncomplete = /未完成|未学完|未开始|待完成|未提交|未观看|进行中/.test(text);
                            const hasFullProgress = /100\\s*%|进度\\s*[:：]?\\s*100/.test(text);
                            if (hasIncomplete && !hasFullProgress) return false;
                            return /已完成|已学完|学习完成|任务点已完成|任务点完成|观看完成|播放完成|完成\\s*100|100\\s*%/.test(text)
                                || /ans-job-finished|finished|finish|complete|completed|done|pass|passed|jobFinish|ywc/i.test(text);
                        };
                        const infoOf = (el) => [
                            el.innerText || el.textContent || '',
                            el.className || '',
                            el.getAttribute('title') || '',
                            el.getAttribute('aria-label') || '',
                            el.outerHTML || '',
                        ].join(' ');

                        const activeCard = document.querySelector('.tabtags span.currents, .tabtags span.current, .tabtags span.active');
                        if (activeCard && detectCompleted(infoOf(activeCard))) return true;

                        const selectors = [
                            '.ans-job-finished',
                            '.job-finished',
                            '.jobFinish',
                            '[class*="finished"]',
                            '[class*="complete"]',
                            '[class*="done"]',
                            '[class*="pass"]',
                            '[title*="已完成"]',
                            '[aria-label*="已完成"]',
                            '[title*="任务点已完成"]',
                            '[aria-label*="任务点已完成"]'
                        ];
                        const nodes = Array.from(document.querySelectorAll(selectors.join(',')))
                            .filter(isVisible);
                        return nodes.some(node => detectCompleted(infoOf(node)));
                    }
                    """
                )
            )
        except Exception as exc:
            logger.debug("检测当前内容完成状态失败，按未完成处理: %s", exc)
            return False

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
