"""项目配置文件。

优先从环境变量读取配置，便于后续接入后台服务或容器部署；
没有环境变量时使用下方默认值。
"""

from pathlib import Path
import os

# 项目根目录
BASE_DIR = Path(__file__).resolve().parents[1]

# =========================
# 账号配置
# =========================
USERNAME: str = os.getenv("CHAOXING_USERNAME", "18369731161")
PASSWORD: str = os.getenv("CHAOXING_PASSWORD", "wangqian12345@")

# =========================
# 学习通地址
# =========================
LOGIN_URL: str = os.getenv("CHAOXING_LOGIN_URL", "https://passport2.chaoxing.com/login")
# 我的课程页：学习通页面会随时间调整，后续可在这里集中维护入口地址
COURSE_LIST_URL: str = os.getenv("CHAOXING_COURSE_LIST_URL", "https://mooc1-1.chaoxing.com/visit/courses")

# 默认课程名称，可通过命令行 --course 覆盖
COURSE_NAME: str = os.getenv("CHAOXING_COURSE_NAME", "马克思主义基本原理概论")

# =========================
# 浏览器配置
# =========================
# 默认有界面，方便观察和调试
HEADLESS: bool = os.getenv("CHAOXING_HEADLESS", "false").lower() in {"1", "true", "yes", "y"}
SLOW_MO: int = int(os.getenv("CHAOXING_SLOW_MO", "0"))
VIEWPORT_WIDTH: int = int(os.getenv("CHAOXING_VIEWPORT_WIDTH", "1366"))
VIEWPORT_HEIGHT: int = int(os.getenv("CHAOXING_VIEWPORT_HEIGHT", "900"))

# =========================
# 等待与轮询配置，单位：毫秒
# =========================
WAIT_TIME: int = int(os.getenv("CHAOXING_WAIT_TIME", "30000"))
SHORT_WAIT_TIME: int = int(os.getenv("CHAOXING_SHORT_WAIT_TIME", "5000"))
VIDEO_POLL_INTERVAL: int = int(os.getenv("CHAOXING_VIDEO_POLL_INTERVAL", "2000"))
VIDEO_MAX_WAIT: int = int(os.getenv("CHAOXING_VIDEO_MAX_WAIT", "7200000"))  # 默认最多等待 2 小时
VIDEO_COMPLETE_RATE: float = float(os.getenv("CHAOXING_VIDEO_COMPLETE_RATE", "0.95"))

# =========================
# 文件路径
# =========================
AUTH_FILE: Path = BASE_DIR / "auth.json"
LOG_DIR: Path = BASE_DIR / "logs"
SCREENSHOT_DIR: Path = BASE_DIR / "screenshots"

# =========================
# 调试配置
# =========================
DEBUG_MODE: bool = os.getenv("CHAOXING_DEBUG", "false").lower() in {"1", "true", "yes", "y"}
PAUSE_ON_ERROR: bool = os.getenv("CHAOXING_PAUSE_ON_ERROR", "true").lower() in {"1", "true", "yes", "y"}
