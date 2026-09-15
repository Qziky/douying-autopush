"""热点选题子模块：按账号定位配置选题规则，过滤并排序候选热点，输出 TOP N 选题。

评分模型：
    score = hot_value_norm * w_hot + match_score * w_match + rise_potential * w_rise

- hot_value_norm  : 热度归一化（log 压缩后线性映射到 0~1）
- match_score     : 领域匹配度（分类命中 + 标题关键词命中）
- rise_potential  : 上升潜力（趋势/排名启发式，0~1）

过滤规则（可配置）：
- 领域分类白名单（enable_category_filter / categories）
- 热度阈值（min_hot_value）
- 排除关键词（exclude_keywords）
"""
from __future__ import annotations

import math
import re
from typing import List, Optional

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.models import HotItem, SelectedTopic

logger = get_logger("hot.selector")

# 领域 -> 默认创作角度
_ANGLE_BY_CATEGORY = {
    "科技": "用通俗语言解读最新科技进展，讲清它如何改变普通人生活",
    "社会": "从民生视角观察社会热点，讲现象、讲影响、讲应对建议",
    "教育": "结合教育政策与家长关切，提供实用解读与行动建议",
    "财经": "把财经新闻翻译成普通人听得懂的钱袋子话题",
    "娱乐": "盘点式呈现，快节奏吸引眼球，结尾给情绪共鸣",
    "健康": "科普向解读，纠正误区，给出可执行的健康建议",
    "体育": "赛事与人物速览，突出看点与话题性",
    "文化": "从文化事件切入，讲背景、讲细节、讲价值",
    "国际": "客观梳理事件脉络，讲清来龙去脉与影响",
}


class HotSelector:
    """热点选题器"""

    def __init__(self):
        self.config = ConfigManager()
        cfg = self.config.get("hot_analysis.selector", {})
        self.enable_category_filter = bool(cfg.get("enable_category_filter", True))
        self.categories = list(cfg.get("categories", []))
        self.min_hot_value = int(cfg.get("min_hot_value", 0))
        self.exclude_keywords = list(cfg.get("exclude_keywords", []))
        self.top_n = int(cfg.get("top_n", 1))
        weights = cfg.get("weights", {})
        self.w_hot = float(weights.get("hot_value", 0.5))
        self.w_match = float(weights.get("match_score", 0.3))
        self.w_rise = float(weights.get("rise_potential", 0.2))

    # ---------------- 对外入口 ----------------
    def select(self, hot_list: List[HotItem], top_n: Optional[int] = None) -> List[SelectedTopic]:
        """过滤 + 排序 + 截取 TOP N。"""
        candidates = self._filter(hot_list)
        if not candidates:
            logger.warning("候选热点为空：请检查过滤规则（类别/热度阈值/排除词）")
            return []
        scored = [(self._score(item), item) for item in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        n = top_n or self.top_n
        result = [self._to_selected(score, item) for score, item in scored[:n]]
        for t in result:
            logger.info("选题 #%d | %s | 热度=%d | 评分=%.3f | 角度=%s",
                        t.hot_item.rank, t.hot_item.title, t.hot_item.hot_value,
                        t.score, t.create_angle)
        return result

    def select_best(self, hot_list: List[HotItem]) -> Optional[SelectedTopic]:
        """快捷方式：只取最优选题（无候选时返回 None）。"""
        result = self.select(hot_list, top_n=1)
        return result[0] if result else None

    # ---------------- 过滤 ----------------
    def _filter(self, hot_list: List[HotItem]) -> List[HotItem]:
        out: List[HotItem] = []
        for item in hot_list:
            if item.hot_value < self.min_hot_value:
                continue
            # 分类白名单：未分类（category=热点）的条目不因分类被过滤，
            # 但仍受热度阈值与排除词约束；已分类条目必须命中白名单。
            if self.enable_category_filter and self.categories and item.category != "热点":
                if not any(c in item.category for c in self.categories):
                    continue
            if any(kw in item.title for kw in self.exclude_keywords):
                continue
            if self._risk_hint(item):
                continue
            out.append(item)
        return out

    @staticmethod
    def _risk_hint(item: HotItem) -> bool:
        """简单风险提示：高危词直接剔除（低频运营风险词）"""
        high_risk = ("灾难", "事故", "死亡", "命案", "塌方", "爆炸", "起火", "伤亡")
        return any(w in item.title for w in high_risk)

    # ---------------- 评分 ----------------
    def _score(self, item: HotItem) -> float:
        hot_norm = self._hot_norm(item.hot_value)
        match = self._match_score(item)
        rise = self._rise_potential(item)
        return self.w_hot * hot_norm + self.w_match * match + self.w_rise * rise

    @staticmethod
    def _hot_norm(hot_value: int) -> float:
        """log 压缩归一化到 0~1（参考区间 50万~1000万热度）"""
        if hot_value <= 0:
            return 0.0
        x = math.log10(max(hot_value, 1))
        return min(max((x - 5.0) / 2.0, 0.0), 1.0)

    def _match_score(self, item: HotItem) -> float:
        """领域匹配度：分类命中 0.7，标题再命中账号定位关键词则 +0.3"""
        score = 0.0
        if self.categories and any(c in item.category for c in self.categories):
            score += 0.7
        title = item.title
        if any(kw and kw in title for kw in self.categories):
            score += 0.3
        return min(score, 1.0)

    @staticmethod
    def _rise_potential(item: HotItem) -> float:
        """上升潜力启发式：
        - mock/playwright 数据带 trend 时优先使用
        - 否则用 排名靠前 + 热度适中（尚未到顶）作为潜力代理
        """
        trend = getattr(item, "trend", None)
        if trend is not None:
            return min(max(float(trend), 0.0), 1.0)
        rank_penalty = min((item.rank - 1) / 50.0, 1.0)
        hot_bonus = 1.0 - HotSelector._hot_norm(item.hot_value)
        return max(0.0, min(1.0, 0.6 - rank_penalty * 0.3 + hot_bonus * 0.4))

    # ---------------- 输出 ----------------
    def _to_selected(self, score: float, item: HotItem) -> SelectedTopic:
        angle = _ANGLE_BY_CATEGORY.get(item.category, "结合热点事实做信息增量解读，给出观点与建议")
        return SelectedTopic(
            hot_item=item,
            score=round(score, 4),
            create_angle=angle,
            risk_level=self._risk_level(item),
        )

    @staticmethod
    def _risk_level(item: HotItem) -> int:
        low = ("官宣", "发布", "公布", "上线", "发布", "启动", "举办")
        if any(w in item.title for w in low):
            return 0
        if "争议" in item.title or "质疑" in item.title:
            return 1
        return 0
