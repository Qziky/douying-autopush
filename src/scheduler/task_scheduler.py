"""调度触发模块：APScheduler 调度内核，支持定时/间隔/热点监控触发。

- 定时触发：Cron 表达式（默认每天 8/12/18 点）
- 热点监控：定期抓取热榜 Top3，变化时触发全流程
- 全局异常捕获：任务异常记录日志，不崩溃退出
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.hot_analysis.crawler import HotCrawler
from src.workflow import Workflow

logger = get_logger("scheduler")


class TaskScheduler:
    """任务调度器"""

    def __init__(self):
        self.config = ConfigManager()
        self.scheduler = BlockingScheduler(timezone=self.config.get("project.timezone", "Asia/Shanghai"))
        self.workflow = Workflow()
        self._last_top3_hash: Optional[str] = None

    # ---------------- 任务定义 ----------------
    def _job_full_workflow(self) -> None:
        """定时全流程任务"""
        logger.info("【定时任务】开始全流程")
        try:
            self.workflow.run_full()
        except Exception as exc:  # noqa: BLE001
            logger.exception("定时任务执行异常")

    def _job_hot_monitor(self) -> None:
        """热点监控：Top3 榜单变化时触发创作"""
        try:
            hot_list = HotCrawler().fetch_hot_list()
            top3 = [h.title for h in hot_list[:3]]
            digest = hashlib.md5("|".join(top3).encode("utf-8")).hexdigest()
            if self._last_top3_hash is not None and digest != self._last_top3_hash:
                logger.info("热点榜 Top3 发生变化，触发全流程创作")
                self._job_full_workflow()
            self._last_top3_hash = digest
        except Exception as exc:  # noqa: BLE001
            logger.warning("热点监控检查失败：%s", exc)

    # ---------------- 注册与启动 ----------------
    def setup(self) -> None:
        cron_expr = str(self.config.get("scheduler.cron", "0 8,12,18 * * *"))
        self.scheduler.add_job(
            self._job_full_workflow,
            CronTrigger.from_crontab(cron_expr, timezone=self.config.get("project.timezone", "Asia/Shanghai")),
            id="full_workflow",
            name="每日定时全流程",
            misfire_grace_time=3600,
            coalesce=True,
        )
        logger.info("已注册定时任务：cron=%s", cron_expr)

        monitor_seconds = int(self.config.get("hot_analysis.refresh_interval", 1800))
        self.scheduler.add_job(
            self._job_hot_monitor,
            IntervalTrigger(seconds=monitor_seconds),
            id="hot_monitor",
            name="热点监控触发",
            misfire_grace_time=600,
            coalesce=True,
        )
        logger.info("已注册热点监控任务：每 %d 秒检查", monitor_seconds)

    def start(self) -> None:
        self.setup()
        logger.info("调度器启动，Ctrl+C 退出")
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("调度器已停止")


def run_scheduler() -> None:
    TaskScheduler().start()


if __name__ == "__main__":
    run_scheduler()
