"""调度服务入口：python run_scheduler.py

启动后按 config/settings.yaml 的 scheduler.cron 定时执行全流程，
并开启热点监控触发。Ctrl+C 停止。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scheduler.task_scheduler import run_scheduler  # noqa: E402

if __name__ == "__main__":
    run_scheduler()
