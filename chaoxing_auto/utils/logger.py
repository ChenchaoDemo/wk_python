"""日志工具。"""

import logging
import sys
from logging import Logger
from pathlib import Path

from config.config import LOG_DIR


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

    file_handler = logging.FileHandler(Path(LOG_DIR) / "app.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_format)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_format)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
