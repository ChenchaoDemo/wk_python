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

    def has_video(self) -> bool:
        """检测当前页面或 iframe 中是否存在 video 标签。"""

        try:
            self._wait_for_video_frame()
            return True
        except VideoNotFoundError:
            return False

    def _wait_for_video_frame(self) -> Frame:
        """等待并返回包含 video 标签的 frame。"""

        elapsed = 0
        while elapsed <= WAIT_TIME:
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
