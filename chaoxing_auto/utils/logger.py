"""日志工具。"""

from datetime import date, datetime
import logging
import sys
from logging import Logger
from pathlib import Path

from config.config import LOG_DIR


class DailyClearFileHandler(logging.FileHandler):
    """每天第一次写入时清空日志文件。

    和按天滚动备份不同，这里满足“app.log 每日清空”的需求：
    - 程序当天第一次启动时，如果 app.log 是昨天或更早生成的，直接覆盖写入；
    - 程序跨天运行时，下一条日志写入前也会清空一次。
    """

    def __init__(self, filename: Path, encoding: str = "utf-8") -> None:
        self._log_date = date.today()
        mode = self._initial_mode(filename, self._log_date)
        super().__init__(filename, mode=mode, encoding=encoding)

    @staticmethod
    def _initial_mode(filename: Path, today: date) -> str:
        path = Path(filename)
        if not path.exists():
            return "a"
        try:
            modified_date = datetime.fromtimestamp(path.stat().st_mtime).date()
        except OSError:
            return "w"
        return "a" if modified_date == today else "w"

    def emit(self, record: logging.LogRecord) -> None:
        today = date.today()
        if today != self._log_date:
            self.acquire()
            try:
                self._log_date = today
                if self.stream:
                    self.stream.flush()
                    self.stream.close()
                self.mode = "w"
                self.stream = self._open()
            finally:
                self.release()
        super().emit(record)


def get_logger(name: str = "chaoxing_auto") -> Logger:
    """获取统一日志对象。

    日志同时输出到控制台和 logs/app.log。
    多次调用不会重复添加 handler。
    """

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    log_format = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = DailyClearFileHandler(Path(LOG_DIR) / "app.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_format)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_format)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
