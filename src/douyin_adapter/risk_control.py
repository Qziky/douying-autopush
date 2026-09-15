"""风控引擎：操作随机延迟、鼠标轨迹模拟、打字速度模拟、发布频率限流、失败熔断。

- 所有对外操作都经过本模块包装，避免机械操作特征
- 发布频率基于 SQLite 中成功发布记录统计（单日上限 + 最小间隔）
- 连续失败达到阈值自动熔断当日任务
"""
from __future__ import annotations

import datetime as dt
import random
import time
from typing import Optional, Tuple

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.storage import WorksDB

logger = get_logger("douyin.risk")


class RiskController:
    """风控引擎"""

    def __init__(self):
        self.config = ConfigManager()
        cfg = self.config.get("risk_control", {})
        self.enabled = bool(cfg.get("enabled", True))
        self.delay_range = tuple(float(x) for x in cfg.get("delay_range", [3, 10]))
        self.human_typing = bool(cfg.get("human_typing", True))
        self.typing_speed = tuple(float(x) for x in cfg.get("typing_speed", [0.05, 0.15]))
        self.mouse_trajectory = bool(cfg.get("mouse_trajectory", True))
        freq = cfg.get("publish_frequency", {})
        self.max_per_day = int(freq.get("max_per_day", 3))
        self.min_interval_hours = float(freq.get("min_interval_hours", 2))
        self.breaker = int(cfg.get("circuit_breaker", 3))
        self._consecutive_failures = 0
        # 自行维护鼠标当前位置（Playwright Mouse 不暴露 position）
        self._last_pos = [640.0, 450.0]
        self._db = WorksDB()

    # ---------------- 延迟 ----------------
    def random_delay(self, lo: Optional[float] = None, hi: Optional[float] = None) -> None:
        """随机延迟（默认 3~10 秒），模拟人类操作节奏"""
        lo = lo if lo is not None else self.delay_range[0]
        hi = hi if hi is not None else self.delay_range[1]
        wait = random.uniform(lo, hi)
        logger.debug("风控：等待 %.1fs", wait)
        time.sleep(wait)

    # ---------------- 鼠标轨迹 ----------------
    def human_move(self, page, x: float, y: float, steps: int = 18) -> None:
        """贝塞尔曲线鼠标移动：从当前位置平滑移动到目标点"""
        x, y = float(x), float(y)
        if not self.mouse_trajectory:
            page.mouse.move(x, y)
            self._last_pos = [x, y]
            return
        from_x, from_y = self._last_pos
        ctrl1 = (from_x + random.uniform(-120, 120), from_y + random.uniform(-120, 120))
        ctrl2 = (x + random.uniform(-80, 80), y + random.uniform(-80, 80))
        for i in range(1, steps + 1):
            t = i / steps
            # 三次贝塞尔
            bx = ((1 - t) ** 3 * from_x + 3 * (1 - t) ** 2 * t * ctrl1[0]
                  + 3 * (1 - t) * t ** 2 * ctrl2[0] + t ** 3 * x)
            by = ((1 - t) ** 3 * from_y + 3 * (1 - t) ** 2 * t * ctrl1[1]
                  + 3 * (1 - t) * t ** 2 * ctrl2[1] + t ** 3 * y)
            page.mouse.move(bx, by)
            time.sleep(random.uniform(0.008, 0.03))
        self._last_pos = [x, y]

    # ---------------- 打字 ----------------
    def human_type(self, page, selector: str, text: str) -> None:
        """模拟人类打字：逐字符输入 + 随机停顿"""
        self.random_delay(0.8, 2.0)
        locator = page.locator(selector)
        locator.click()
        locator.fill("")
        if not self.human_typing:
            locator.fill(text)
            return
        for ch in text:
            locator.press_sequentially(ch, delay=random.uniform(*self.typing_speed) * 1000)

    # ---------------- 发布频率 ----------------
    def check_publish_allowed(self) -> Tuple[bool, str]:
        """发布频率校验：单日上限 + 最小发布间隔"""
        today = dt.date.today().isoformat()
        count = self._db.count_publish_today(today)
        if count >= self.max_per_day:
            return False, f"单日发布数已达上限（{count}/{self.max_per_day}）"

        recent = self._db.recent_tasks(10)
        for r in recent:
            # 仅统计真实发布（status=success），dry-run 演练不占额度
            if r.get("status") == "success" and r.get("started_at"):
                try:
                    last = dt.datetime.fromisoformat(r["started_at"])
                except ValueError:
                    continue
                elapsed = (dt.datetime.now() - last).total_seconds() / 3600
                if elapsed < self.min_interval_hours:
                    remain = self.min_interval_hours - elapsed
                    return False, f"距上次发布不足最小间隔（还需 {remain:.1f} 小时）"
        return True, "ok"

    # ---------------- 熔断 ----------------
    def on_failure(self) -> bool:
        """发布失败时调用，返回 True 表示已熔断（当日停止）"""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.breaker:
            logger.error("连续失败 %d 次，触发熔断，当日停止发布", self._consecutive_failures)
            return True
        return False

    def on_success(self) -> None:
        self._consecutive_failures = 0
