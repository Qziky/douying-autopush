"""日志系统：统一日志格式与输出目标，按天切割，分级输出。

- 控制台：标准输出（含时间、级别、模块）
- 文件：data/logs/works.log，按天切割，保留 backup_days 天
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from src.common.config import ConfigManager

_FMT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


class LoggerFactory:
    _initialized = False

    @classmethod
    def _ensure_root(cls) -> None:
        if cls._initialized:
            return
        config = ConfigManager()
        level_name = str(config.get("logging.level", "INFO")).upper()
        level = getattr(logging, level_name, logging.INFO)

        root = logging.getLogger("douyin_auto")
        root.setLevel(level)
        root.handlers.clear()
        root.propagate = False

        # 控制台
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
        root.addHandler(console)

        # 文件（按天切割）
        log_dir = config.resolve_path(str(config.get("logging.dir", "data/logs")))
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_dir / "works.log",
            when="midnight",
            backupCount=int(config.get("logging.backup_days", 7)),
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
        root.addHandler(file_handler)

        cls._initialized = True

    @classmethod
    def get_logger(cls, name: str = "app") -> logging.Logger:
        cls._ensure_root()
        return logging.getLogger(f"douyin_auto.{name}")


def get_logger(name: str = "app") -> logging.Logger:
    return LoggerFactory.get_logger(name)
