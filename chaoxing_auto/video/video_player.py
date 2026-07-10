"""视频播放控制模块。"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page, TimeoutError as PlaywrightTimeoutError

from config.config import VIDEO_COMPLETE_RATE, VIDEO_MAX_WAIT, VIDEO_POLL_INTERVAL, WAIT_TIME
from utils.helper import save_screenshot
from utils.logger import get_logger

logger = get_logger()


class VideoNotFoundError(RuntimeError):
    """页面中未找到 video 标签。"""


class VideoPlayer:
    """HTML5 video 自动播放与进度检测。"""

    def __init__(
        self,
        page: Page,
        complete_rate: float = VIDEO_COMPLETE_RATE,
        poll_interval: int = VIDEO_POLL_INTERVAL,
        max_wait: int = VIDEO_MAX_WAIT,
    ) -> None:
        self.page = page
        self.complete_rate = complete_rate
        self.poll_interval = poll_interval
        self.max_wait = max_wait

    def play(
        self,
        on_progress: Optional[Callable[[float, Dict[str, float]], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """播放当前页面视频，并等待播放进度达到 complete_rate。

        Args:
            on_progress: 进度回调，参数为 progress 和视频状态信息。
            should_stop: 停止回调，返回 True 时主动停止等待。
        """

        try:
            frame = self._wait_for_video_frame()
            self._start_video(frame)
            logger.info("视频开始播放")

            elapsed = 0
            last_logged_progress = -1.0
            while elapsed <= self.max_wait:
                if should_stop is not None and should_stop():
                    logger.info("收到停止信号，停止等待视频完成")
                    self._pause_video(frame)
                    return False

                state = self._get_video_state(frame)
                current_time = state.get("currentTime", 0.0)
                duration = state.get("duration", 0.0)
                paused = state.get("paused", 1.0)
                ended = state.get("ended", 0.0)

                if duration > 0:
                    progress = min(current_time / duration, 1.0)
                elif ended:
                    progress = 1.0
                else:
                    progress = 0.0

                progress_percent = round(progress * 100, 2)
                if on_progress is not None:
                    on_progress(progress_percent, state)

                # 降低日志量：进度每增长 5% 输出一次。
                if progress_percent - last_logged_progress >= 5 or progress >= self.complete_rate:
                    logger.info(
                        "视频播放进度: %.2f%% 当前 %.2fs / 总 %.2fs paused=%s ended=%s",
                        progress_percent,
                        current_time,
                        duration,
                        bool(paused),
                        bool(ended),
                    )
                    last_logged_progress = progress_percent

                if progress >= self.complete_rate or ended:
                    logger.info("视频播放完成，进度 %.2f%%", progress_percent)
                    return True

                # 如果视频意外暂停，尝试恢复播放。
                if paused and not ended:
                    logger.warning("检测到视频暂停，尝试恢复播放")
                    self._start_video(frame)

                self.page.wait_for_timeout(self.poll_interval)
                elapsed += self.poll_interval

            raise TimeoutError(f"等待视频完成超时，已等待 {self.max_wait} ms")
        except VideoNotFoundError:
            save_screenshot(self.page, "error_video_not_found")
            logger.exception("当前页面未找到 video 标签")
            raise
        except PlaywrightTimeoutError as exc:
            save_screenshot(self.page, "error_video_timeout")
            logger.exception("视频等待超时: %s", exc)
            raise
        except PlaywrightError as exc:
            save_screenshot(self.page, "error_video_playwright")
            logger.exception("视频播放失败: %s", exc)
            raise
        except Exception as exc:
            save_screenshot(self.page, "error_video")
            logger.exception("视频播放异常: %s", exc)
            raise

    def has_video(self, timeout: Optional[int] = None) -> bool:
        """检测当前页面或 iframe 中是否存在 video 标签。"""

        try:
            self._wait_for_video_frame(max_wait=timeout)
            return True
        except VideoNotFoundError:
            return self._has_video_iframe_on_page()

    def _has_video_iframe_on_page(self) -> bool:
        """检测主页面是否已出现学习通视频 iframe/容器。

        有些页面 iframe 先出现，内部 video 稍后才加载；此时不能直接按“无视频”跳过。
        """

        try:
            return bool(
                self.page.evaluate(
                    """
                    () => {
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
                        const nodes = Array.from(document.querySelectorAll([
                            '.ans-attach-ct.videoContainer iframe',
                            '.videoContainer iframe',
                            'iframe.ans-insertvideo-online',
                            'iframe[src*="/ananas/modules/video"]',
                            'iframe[objectid][jobid]'
                        ].join(',')));
                        return nodes.some(node => isVisible(node) || isVisible(node.closest('.videoContainer,.ans-attach-ct')));
                    }
                    """
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("检测主页面视频 iframe 失败: %s", exc)
            return False

    def get_video_task_status(self, timeout: Optional[int] = None) -> Dict[str, str]:
        """检测当前视频任务点状态。

        学习通视频上方通常会显示“任务点已完成 / 任务点未完成”。
        这里优先读取包含 video 的 frame；如果 frame 内没有状态，再读取主页面上的
        任务点状态元素。只要明确看到“任务点已完成”，当前视频就可以跳过。
        """

        page_status = self._detect_task_status_on_page()
        if page_status.get("status") != "unknown":
            return page_status

        try:
            frame = self._wait_for_video_frame(max_wait=timeout)
            frame_status = self._detect_task_status_in_frame(frame)
            if frame_status.get("status") != "unknown":
                return frame_status
        except VideoNotFoundError:
            logger.debug("检测视频任务点状态时尚未找到 iframe 内部 video，按 unknown 返回并交给播放流程继续等待")

        return {
            "status": "unknown",
            "completed": "false",
            "source": "unknown",
            "text": "",
        }

    def _wait_for_video_frame(self, max_wait: Optional[int] = None) -> Frame:
        """等待并返回包含 video 标签的 frame。"""

        elapsed = 0
        wait_limit = WAIT_TIME if max_wait is None else max_wait
        while elapsed <= wait_limit:
            try:
                return self._find_video_frame()
            except VideoNotFoundError:
                self.page.wait_for_timeout(500)
                elapsed += 500
        raise VideoNotFoundError("页面和所有 iframe 中均未发现 video 标签")

    def _find_video_frame(self) -> Frame:
        """在主页面和所有 iframe 中查找 video 标签。"""

        for frame in self.page.frames:
            try:
                count = frame.locator("video").count()
                if count > 0:
                    return frame
            except Exception:
                continue
        raise VideoNotFoundError("未找到 video 标签")

    def _detect_task_status_in_frame(self, frame: Frame) -> Dict[str, str]:
        """在指定 frame 内检测视频任务点状态。"""

        try:
            result = frame.evaluate(
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
                    const normalizeStatus = (text, source) => {
                        text = clean(text);
                        if (!text) {
                            return { status: 'unknown', completed: 'false', source, text: '' };
                        }

                        // 注意：完成条件说明里可能有“未完成任务点前, 当前视频不可拖拽”，
                        // 这不是任务点状态，不能因为包含“未完成”二字就误判。
                        // 所以这里优先看明确属性/类名/完整短语。
                        const explicitCompleted = /aria-label=["']任务点已完成["']|title=["']任务点已完成["']|任务点已完成/i.test(text)
                            || /(^|\\s)(ans-job-finished|job-finished|icon_Completed|jobFinish)(\\s|$)/i.test(text);
                        if (explicitCompleted) {
                            return { status: 'completed', completed: 'true', source, text };
                        }

                        const explicitUnfinished = /aria-label=["']任务点未完成["']|title=["']任务点未完成["']|任务点未完成|待完成/i.test(text)
                            || /(^|\\s)(ans-job-unfinished|job-unfinished|orange01|jobCount|noJob)(\\s|$)/i.test(text);
                        if (explicitUnfinished) {
                            return { status: 'unfinished', completed: 'false', source, text };
                        }
                        return { status: 'unknown', completed: 'false', source, text };
                    };
                    const infoOf = (el) => clean([
                        el.innerText || el.textContent || '',
                        el.className || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('aria-label') || '',
                        el.outerHTML || '',
                    ].join(' '));

                    const videos = Array.from(document.querySelectorAll('video'));
                    const video = videos.find(v => Number.isFinite(v.duration) && v.duration > 0) || videos[0] || null;
                    if (!video) {
                        return { status: 'unknown', completed: 'false', source: 'frame_no_video', text: '' };
                    }

                    const containers = [];
                    let node = video;
                    for (let i = 0; node && i < 8; i += 1, node = node.parentElement) {
                        containers.push(node);
                    }

                    for (const container of containers) {
                        const selectors = [
                            '.ans-job-finished',
                            '.ans-job-unfinished',
                            '.job-finished',
                            '.job-unfinished',
                            '.jobFinish',
                            '.icon_Completed',
                            '[title*="任务点"]',
                            '[aria-label*="任务点"]',
                            '[class*="job"]',
                            '[class*="finish"]'
                        ].join(',');
                        const nodes = Array.from(container.querySelectorAll(selectors)).filter(isVisible);
                        for (const item of nodes) {
                            const status = normalizeStatus(infoOf(item), 'video_frame_near_video');
                            if (status.status !== 'unknown') return status;
                        }

                        const directStatus = normalizeStatus(infoOf(container), 'video_frame_container');
                        if (directStatus.status !== 'unknown') return directStatus;
                    }

                    const explicitNodes = Array.from(document.querySelectorAll([
                        '.ans-job-finished',
                        '.ans-job-unfinished',
                        '.job-finished',
                        '.job-unfinished',
                        '.jobFinish',
                        '.icon_Completed',
                        '[title*="任务点"]',
                        '[aria-label*="任务点"]'
                    ].join(','))).filter(isVisible);
                    for (const item of explicitNodes) {
                        const status = normalizeStatus(infoOf(item), 'video_frame_explicit_node');
                        if (status.status !== 'unknown') return status;
                    }

                    // 最后兜底：只看 frame 内是否明确出现“任务点已完成/任务点未完成”。
                    const bodyText = clean(document.body ? document.body.innerText : '');
                    if (/任务点已完成|任务点未完成/.test(bodyText)) {
                        return normalizeStatus(bodyText, 'video_frame_body_text');
                    }
                    return { status: 'unknown', completed: 'false', source: 'video_frame_unknown', text: '' };
                }
                """
            )
            return result or {"status": "unknown", "completed": "false", "source": "frame_empty", "text": ""}
        except Exception as exc:  # noqa: BLE001
            logger.debug("检测 frame 内视频任务点状态失败: %s", exc)
            return {"status": "unknown", "completed": "false", "source": "frame_error", "text": str(exc)}

    def _detect_task_status_on_page(self) -> Dict[str, str]:
        """在主页面检测当前视频任务点状态。"""

        try:
            result = self.page.evaluate(
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
                    const normalizeStatus = (text, source) => {
                        text = clean(text);
                        if (!text) {
                            return { status: 'unknown', completed: 'false', source, text: '' };
                        }

                        // 注意：完成条件说明里可能有“未完成任务点前, 当前视频不可拖拽”，
                        // 这不是任务点状态，不能因为包含“未完成”二字就误判。
                        // 所以这里优先看明确属性/类名/完整短语。
                        const explicitCompleted = /aria-label=["']任务点已完成["']|title=["']任务点已完成["']|任务点已完成/i.test(text)
                            || /(^|\\s)(ans-job-finished|job-finished|icon_Completed|jobFinish)(\\s|$)/i.test(text);
                        if (explicitCompleted) {
                            return { status: 'completed', completed: 'true', source, text };
                        }

                        const explicitUnfinished = /aria-label=["']任务点未完成["']|title=["']任务点未完成["']|任务点未完成|待完成/i.test(text)
                            || /(^|\\s)(ans-job-unfinished|job-unfinished|orange01|jobCount|noJob)(\\s|$)/i.test(text);
                        if (explicitUnfinished) {
                            return { status: 'unfinished', completed: 'false', source, text };
                        }
                        return { status: 'unknown', completed: 'false', source, text };
                    };
                    const infoOf = (el) => clean([
                        el.innerText || el.textContent || '',
                        el.className || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('aria-label') || '',
                        el.outerHTML || '',
                    ].join(' '));

                    const collectPreferredStatus = (statuses) => {
                        const unfinished = statuses.find(item => item.status === 'unfinished');
                        if (unfinished) return unfinished;
                        const completed = statuses.find(item => item.status === 'completed');
                        if (completed) return completed;
                        return { status: 'unknown', completed: 'false', source: 'page_unknown', text: '' };
                    };

                    const statusFromVideoContainer = (container) => {
                        const status = normalizeStatus(infoOf(container), 'page_video_container');
                        if (status.status !== 'unknown') return status;

                        const className = ` ${container.className || ''} `;
                        if (/(^|\\s)(ans-job-finished|job-finished)(\\s|$)/i.test(className)) {
                            return {
                                status: 'completed',
                                completed: 'true',
                                source: 'page_video_container_class',
                                text: infoOf(container),
                            };
                        }

                        const icon = container.querySelector('.ans-job-icon[aria-label*="任务点"], [aria-label*="任务点"]');
                        const iconLabel = icon ? clean(icon.getAttribute('aria-label') || icon.getAttribute('title') || '') : '';
                        if (/任务点已完成/.test(iconLabel)) {
                            return {
                                status: 'completed',
                                completed: 'true',
                                source: 'page_video_container_icon',
                                text: iconLabel,
                            };
                        }
                        if (/任务点未完成/.test(iconLabel)) {
                            return {
                                status: 'unfinished',
                                completed: 'false',
                                source: 'page_video_container_icon',
                                text: iconLabel,
                            };
                        }

                        // 学习通当前结构里，视频任务容器有 ans-job-icon 但没有 ans-job-finished 时，
                        // 通常就是“任务点未完成”。这可以覆盖用户提供的未完成页面结构。
                        if (container.querySelector('.ans-job-icon') && !/(^|\\s)ans-job-finished(\\s|$)/i.test(className)) {
                            return {
                                status: 'unfinished',
                                completed: 'false',
                                source: 'page_video_container_no_finished_marker',
                                text: infoOf(container),
                            };
                        }

                        return status;
                    };

                    const videoContainers = Array.from(document.querySelectorAll([
                        '.ans-attach-ct.videoContainer',
                        '.videoContainer',
                        '.ans-attach-ct'
                    ].join(','))).filter(container => {
                        const hasVideoEntry = container.querySelector([
                            'iframe.ans-insertvideo-online',
                            'iframe[src*="/ananas/modules/video"]',
                            'iframe[objectid][jobid]',
                            'video'
                        ].join(','));
                        return hasVideoEntry
                            && (isVisible(container) || isVisible(hasVideoEntry));
                    });
                    const containerStatuses = [];
                    for (const container of videoContainers) {
                        const status = statusFromVideoContainer(container);
                        if (status.status !== 'unknown') containerStatuses.push(status);
                    }
                    const preferredContainerStatus = collectPreferredStatus(containerStatuses);
                    if (preferredContainerStatus.status !== 'unknown') return preferredContainerStatus;

                    const selectors = [
                        '.ans-job-finished',
                        '.ans-job-unfinished',
                        '.job-finished',
                        '.job-unfinished',
                        '.jobFinish',
                        '.icon_Completed',
                        '[title*="任务点"]',
                        '[aria-label*="任务点"]'
                    ].join(',');
                    const nodes = Array.from(document.querySelectorAll(selectors)).filter(isVisible);
                    const explicitStatuses = [];
                    for (const node of nodes) {
                        const status = normalizeStatus(infoOf(node), 'page_explicit_node');
                        if (status.status !== 'unknown') explicitStatuses.push(status);
                    }
                    const preferredExplicitStatus = collectPreferredStatus(explicitStatuses);
                    if (preferredExplicitStatus.status !== 'unknown') return preferredExplicitStatus;

                    const active = document.querySelector('.tabtags span.currents, .tabtags span.current, .tabtags span.active');
                    if (active) {
                        const container = active.closest('.tabtags, .ans-attach-ct, .ans-job, .chapter, .content, div') || active;
                        const status = normalizeStatus(infoOf(container), 'page_active_card_container');
                        if (status.status !== 'unknown') return status;
                    }

                    const bodyText = clean(document.body ? document.body.innerText : '');
                    if (/任务点已完成|任务点未完成/.test(bodyText)) {
                        return normalizeStatus(bodyText, 'page_body_text');
                    }
                    return { status: 'unknown', completed: 'false', source: 'page_unknown', text: '' };
                }
                """
            )
            return result or {"status": "unknown", "completed": "false", "source": "page_empty", "text": ""}
        except Exception as exc:  # noqa: BLE001
            logger.debug("检测主页面视频任务点状态失败: %s", exc)
            return {"status": "unknown", "completed": "false", "source": "page_error", "text": str(exc)}

    def _start_video(self, frame: Frame) -> None:
        """通过 JS 启动 HTML5 video。"""

        # 尝试点击视频区域以满足浏览器自动播放策略。
        try:
            video = frame.locator("video").first
            if video.is_visible(timeout=2000):
                video.click(timeout=2000, force=True)
        except Exception:
            pass

        result = frame.evaluate(
            """
            async () => {
                const videos = Array.from(document.querySelectorAll('video'));
                if (!videos.length) {
                    return { ok: false, message: 'video not found' };
                }
                const video = videos.find(v => Number.isFinite(v.duration) && v.duration > 0) || videos[0];
                video.muted = true;
                video.volume = 0;
                video.playbackRate = 1.0;
                video.removeAttribute('disablePictureInPicture');
                try {
                    const promise = video.play();
                    if (promise && typeof promise.then === 'function') {
                        await promise;
                    }
                    return { ok: true, message: 'playing' };
                } catch (error) {
                    return { ok: false, message: String(error && error.message ? error.message : error) };
                }
            }
            """
        )
        if not result or not result.get("ok"):
            raise RuntimeError(f"调用 video.play() 失败: {result}")

    def _pause_video(self, frame: Frame) -> None:
        """暂停当前 frame 中的 HTML5 video，供 GUI 暂停任务时使用。"""

        try:
            frame.evaluate(
                """
                () => {
                    const videos = Array.from(document.querySelectorAll('video'));
                    videos.forEach(video => {
                        try { video.pause(); } catch (e) {}
                    });
                }
                """
            )
        except Exception as exc:  # noqa: BLE001 - 暂停失败不影响停止自动化循环
            logger.warning("暂停视频失败: %s", exc)

    def _get_video_state(self, frame: Frame) -> Dict[str, float]:
        """通过 JS 获取播放进度。"""

        state = frame.evaluate(
            """
            () => {
                const videos = Array.from(document.querySelectorAll('video'));
                const video = videos.find(v => Number.isFinite(v.duration) && v.duration > 0) || videos[0];
                if (!video) {
                    return null;
                }
                return {
                    currentTime: Number(video.currentTime || 0),
                    duration: Number(Number.isFinite(video.duration) ? video.duration : 0),
                    paused: video.paused ? 1 : 0,
                    ended: video.ended ? 1 : 0,
                    readyState: Number(video.readyState || 0),
                    playbackRate: Number(video.playbackRate || 1)
                };
            }
            """
        )
        if state is None:
            raise VideoNotFoundError("读取视频状态时未找到 video 标签")
        return state

    def get_progress(self) -> Tuple[float, Dict[str, float]]:
        """获取当前视频进度百分比。"""

        frame = self._find_video_frame()
        state = self._get_video_state(frame)
        duration = state.get("duration", 0.0)
        current_time = state.get("currentTime", 0.0)
        progress = 0.0 if duration <= 0 else min(current_time / duration * 100, 100.0)
        return round(progress, 2), state
