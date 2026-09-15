"""脚本生成器：接收热点选题，调用大模型生成结构化分镜脚本。

- LLM 通道：读取 Prompt 模板 -> 调用 LLM -> 强校验 JSON -> 输出 VideoScript
- 演示通道：未配置 API Key 时使用内置确定性模板生成器，保证全流程可联调
- 字数校验：按 4.5 字/秒语速校准场景时长与总时长
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import List, Optional

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.models import HotItem, Scene, SelectedTopic, VideoScript
from src.content_gen.llm_client import LLMClient

logger = get_logger("content_gen.script")

SPEAK_RATE = 4.5  # 字/秒（正常语速）


class ScriptWriter:
    """文案脚本生成器"""

    def __init__(self):
        self.config = ConfigManager()
        self.llm = LLMClient()
        self.prompt_template = self._load_prompt()
        self.duration = int(self.config.get("video.default_duration", 60))
        self.demo_mode = bool(self.config.get("content_gen.demo_mode", True))

    def _load_prompt(self) -> str:
        rel = self.config.get("content_gen.prompt_template",
                              "src/content_gen/prompts/script_gen.tpl")
        path = self.config.resolve_path(rel)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    # ---------------- 对外入口 ----------------
    def generate_script(self, topic: SelectedTopic) -> VideoScript:
        if self.llm.is_available():
            last_exc = None
            # LLM 偶发返回不规范结构时内部重试一次，仍失败再降级演示生成器
            for attempt in (1, 2):
                try:
                    script = self._generate_by_llm(topic)
                    logger.info("脚本生成成功（LLM）：%s", script.title)
                    return script
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    logger.warning("LLM 第 %d 次生成失败(%s)", attempt, exc)
            logger.warning("LLM 两次均失败，降级到演示生成器：%s", last_exc)
            if not self.demo_mode:
                raise last_exc
        script = self._generate_demo(topic)
        logger.info("脚本生成成功（演示生成器）：%s", script.title)
        return script

    # ---------------- LLM 通道 ----------------
    def _generate_by_llm(self, topic: SelectedTopic) -> VideoScript:
        # 用 replace 而非 str.format：模板内含 JSON 花括号示例，format 会误解析
        prompt = (self.prompt_template
                  .replace("{hot_title}", topic.hot_item.title)
                  .replace("{create_angle}", topic.create_angle)
                  .replace("{duration}", str(self.duration)))
        data = self.llm.chat_json(prompt)
        script = self._build_script(topic, data)
        self._validate_script(script)
        return script

    # ---------------- 演示通道 ----------------
    def _generate_demo(self, topic: SelectedTopic) -> VideoScript:
        hot = topic.hot_item
        angle = topic.create_angle
        full_script = self._build_demo_script_text(hot.title, angle)

        scenes: List[Scene] = []
        sentences = self._split_sentences(full_script)
        total = 0
        for i, sent in enumerate(sentences, 1):
            dur = self._duration_for(sent)
            total += dur
            keyword = self._pick_keyword(hot, sent, i)
            scenes.append(Scene(index=i, text=sent, image_keyword=keyword, duration=dur))

        # 标题
        title = self._make_title(hot.title)
        tags = self._make_tags(hot)
        cover_text = title[:10]

        return VideoScript(
            topic=topic,
            title=title,
            full_script=full_script,
            tags=tags,
            scenes=scenes,
            total_duration=max(total, 1),
            cover_text=cover_text,
        )

    @staticmethod
    def _build_demo_script_text(hot_title: str, angle: str) -> str:
        head = f"最近有个消息刷屏了，就是关于「{hot_title}」这件事。"
        body = (
            f"很多朋友第一反应是：这跟我有什么关系？其实关系很大，"
            f"今天就用一分钟，把来龙去脉给你讲清楚。"
            f"先看事实：这件事的核心，是信息正在快速变化，普通人最需要的是可靠的信息源。"
            f"再看影响：无论是生活、工作还是消费选择，它都会在未来一段时间持续起作用。"
            f"最后给建议：不要被标题带节奏，多对照官方渠道核实，再做判断。"
            f"记住一句话：热点会过去，认知会留下。"
        )
        return head + body

    # ---------------- 工具 ----------------
    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        parts = re.split(r"[。！？;；]", text)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _duration_for(sentence: str) -> int:
        secs = math.ceil(len(sentence) / SPEAK_RATE)
        return max(3, min(secs, 10))

    _STOP_WORDS = {
        "这个", "那个", "这些", "那些", "最近", "今天", "其实", "很多", "大家",
        "朋友", "一下", "什么", "怎么", "为什么", "关于", "就是", "可以", "需要",
        "一个", "我们", "你们", "他们", "这种", "时候", "事情", "消息", "第一",
        "最后", "记住", "先看", "再看", "告诉", "关系",
    }

    def _pick_keyword(self, hot: HotItem, sentence: str, index: int) -> str:
        """画面关键词：热点标题实词优先，其次句子实词，最后按分类兜底。"""
        candidates = [hot.title, sentence]
        for text in candidates:
            words = [w for w in re.findall(r"[\u4e00-\u9fa5]{2,6}", text)
                     if w not in self._STOP_WORDS and not any(s in w for s in ("最近", "今天"))]
            if words:
                return words[0]
        return {"科技": "科技实验室", "社会": "城市街景", "教育": "校园课堂",
                "财经": "金融中心大楼", "娱乐": "演出现场", "健康": "健康生活",
                "体育": "运动场馆", "旅游": "旅行风景"}.get(hot.category, "新闻演播室")

    @staticmethod
    def _make_title(hot_title: str) -> str:
        suffix = "，这几点要早知道"
        base = hot_title
        if len(base) > 12:
            # 优先在标点处截断，避免生硬断词
            cut = max(base.rfind("，"), base.rfind("。"), base.rfind("！"), base.rfind("？"))
            base = base[:cut] if 6 <= cut <= 12 else base[:12]
        return (base + suffix)[:24]

    @staticmethod
    def _make_tags(hot) -> List[str]:
        tags = [t for t in hot.tags if t][:3]
        tags += ["热点解读", "知识科普"]
        return list(dict.fromkeys(tags))[:5]

    def _build_script(self, topic: SelectedTopic, data: dict) -> VideoScript:
        scenes_data = data.get("scenes") or []
        scenes = []
        for i, sc in enumerate(scenes_data, 1):
            scenes.append(Scene(
                index=i,
                text=str(sc.get("text", "")).strip(),
                image_keyword=str(sc.get("image_keyword", "")).strip() or "新闻演播室",
                duration=float(sc.get("duration", 4)),
            ))
        return VideoScript(
            topic=topic,
            title=str(data.get("title", "")).strip()[:40],
            full_script=str(data.get("full_script", "")).strip(),
            tags=[str(t) for t in (data.get("tags") or [])][:5],
            scenes=scenes,
            total_duration=int(data.get("total_duration", self.duration)),
            cover_text=str(data.get("cover_text", "")).strip()[:12],
        )

    def _validate_script(self, script: VideoScript) -> None:
        """字数校验：台词总字数应落在目标时长的合理区间（±30%）"""
        if not script.title:
            raise ValueError("脚本校验失败：标题为空")
        if not script.full_script:
            raise ValueError("脚本校验失败：正文为空")
        if not script.scenes:
            raise ValueError("脚本校验失败：分镜为空")
        text_len = sum(len(sc.text) for sc in script.scenes)
        ideal = int(self.duration * SPEAK_RATE)
        if text_len < ideal * 0.5 or text_len > ideal * 1.4:
            logger.warning("字数校验提示：台词 %d 字 vs 目标 %d 字（%d 秒）",
                           text_len, ideal, self.duration)
        # 校准分镜时长
        for sc in script.scenes:
            sc.duration = self._duration_for(sc.text)
        script.total_duration = max(sum(sc.duration for sc in script.scenes), 1)
