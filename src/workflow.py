"""全流程编排：热点发现 -> 文案生成 -> 合规校验 -> 视频渲染 -> 自动发布 -> 结果落库。

设计原则：
- 模块间通过标准数据契约（models）单向流转
- 每阶段异常向上抛，由本层统一捕获并记录任务状态
- 合规校验在「文案生成后」与「视频渲染前」各执行一次
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import List, Optional

from src.common.compliance import ComplianceChecker
from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.models import (PublishResult, RenderResult, SelectedTopic,
                               TaskRecord, VideoScript)
from src.common.storage import WorksDB
from src.content_gen.script_writer import ScriptWriter
from src.douyin_adapter.publisher import VideoPublisher
from src.hot_analysis.crawler import HotCrawler
from src.hot_analysis.selector import HotSelector
from src.video_render.composer import VideoComposer

logger = get_logger("workflow")


class Workflow:
    """全流程执行器"""

    def __init__(self):
        self.config = ConfigManager()
        self.db = WorksDB()
        self.compliance = ComplianceChecker()
        self.script_dir = self.config.resolve_path("output/scripts")
        self.script_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 分步执行 ----------------
    def fetch_hot(self) -> List:
        logger.info("== 阶段 1/6：热点采集 ==")
        crawler = HotCrawler()
        hot_list = crawler.fetch_hot_list()
        for h in hot_list[:10]:
            logger.info("  #%02d [%s] %s（热度 %d）", h.rank, h.category, h.title, h.hot_value)
        return hot_list

    def select_topic(self, hot_list: List) -> Optional[SelectedTopic]:
        logger.info("== 阶段 2/6：热点选题 ==")
        selector = HotSelector()
        topic = selector.select_best(hot_list)
        if topic is None:
            raise RuntimeError("无可用选题：请放宽过滤规则（类别/热度阈值/排除词）")
        logger.info("最终选题：%s（评分 %.3f）", topic.hot_item.title, topic.score)
        return topic

    def generate_script(self, topic: SelectedTopic) -> VideoScript:
        logger.info("== 阶段 3/6：文案生成 ==")
        writer = ScriptWriter()
        script = writer.generate_script(topic)
        logger.info("标题：%s | 分镜 %d 幕 | 总时长 %ds",
                    script.title, len(script.scenes), script.total_duration)
        try:
            saved = self.save_script(script)
            logger.info("脚本已持久化：%s", saved)
        except Exception as exc:  # noqa: BLE001
            logger.warning("脚本持久化失败（不影响主流程）：%s", exc)
        return script

    def compliance_check(self, script: VideoScript) -> None:
        logger.info("== 阶段 4/6：内容合规校验 ==")
        result = self.compliance.check_script(script.title, script.full_script, script.tags)
        if not result.passed:
            raise RuntimeError(f"合规校验未通过：{result.reason}")
        logger.info("合规校验通过（%s）", result.level)

    def render_video(self, script: VideoScript) -> RenderResult:
        logger.info("== 阶段 5/6：视频渲染 ==")
        composer = VideoComposer()
        return composer.compose(script, output_name=script.title)

    def publish_video(self, render_result: RenderResult, script: VideoScript,
                      account_name: Optional[str] = None) -> PublishResult:
        logger.info("== 阶段 6/6：发布执行 ==")
        publisher = VideoPublisher()
        return publisher.publish(render_result, script, account_name=account_name)

    # ---------------- 全流程 ----------------
    def run_full(self, account_name: Optional[str] = None,
                 dry_run: Optional[bool] = None,
                 hot_title: Optional[str] = None,
                 script: Optional[VideoScript] = None,
                 render_result: Optional[RenderResult] = None) -> TaskRecord:
        """执行完整流水线。支持注入已生成的脚本/渲染结果以支持分步联调。"""
        if dry_run is not None:
            self.config.set("douyin.dry_run", dry_run)

        record = TaskRecord(
            task_id=uuid.uuid4().hex[:12],
            started_at=dt.datetime.now().isoformat(timespec="seconds"),
        )
        try:
            hot_list = self.fetch_hot()

            if hot_title:
                # 指定热点（用于测试：从榜单中按标题匹配，找不到则构造占位）
                topic = self._find_by_title(hot_list, hot_title)
            else:
                topic = self.select_topic(hot_list)
            record.hot_title = topic.hot_item.title
            record.stage = "generate_script"
            self.db.upsert_task(record)

            if script is None:
                script = self.generate_script(topic)
            record.script_title = script.title
            record.stage = "compliance"
            self.db.upsert_task(record)

            self.compliance_check(script)

            if render_result is None:
                render_result = self.render_video(script)
            record.video_path = render_result.video_path
            record.stage = "publish"
            self.db.upsert_task(record)

            publish = self.publish_video(render_result, script, account_name=account_name)
            if publish.success:
                if self.config.get("douyin.dry_run", True):
                    record.status = "dry_run"   # 演练记录，不计入发布频率
                else:
                    record.status = "success"
                record.publish_url = publish.video_url or ""
                tail = (publish.video_url
                        or ("演练模式未真实发布" if self.config.get("douyin.dry_run", True)
                            else "真实发布已提交（未取到作品链接，请到作品管理核对）"))
                logger.info("全流程完成：%s（%s）", record.status, tail)
            else:
                record.status = "failed"
                record.error_msg = publish.error_msg or "发布失败"
                logger.error("全流程终止：发布失败 - %s", publish.error_msg)
        except Exception as exc:  # noqa: BLE001
            record.status = "failed"
            record.error_msg = str(exc)
            logger.exception("全流程执行失败")
        finally:
            record.finished_at = dt.datetime.now().isoformat(timespec="seconds")
            self.db.upsert_task(record)
        return record

    def save_script(self, script: VideoScript) -> Path:
        """将脚本持久化为 JSON（供 render/publish 分步复用）"""
        path = self.script_dir / f"{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        data = {
            "title": script.title,
            "full_script": script.full_script,
            "tags": script.tags,
            "total_duration": script.total_duration,
            "cover_text": script.cover_text,
            "hot_title": script.topic.hot_item.title,
            "scenes": [{"index": s.index, "text": s.text,
                        "image_keyword": s.image_keyword, "duration": s.duration}
                       for s in script.scenes],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("脚本已保存：%s", path)
        return path

    # ---------------- 工具 ----------------
    @staticmethod
    def _find_by_title(hot_list: List, hot_title: str) -> SelectedTopic:
        for h in hot_list:
            if hot_title in h.title or h.title in hot_title:
                return SelectedTopic(hot_item=h, score=1.0, create_angle="按指定热点创作", risk_level=0)
        from src.common.models import HotItem
        mock = HotItem(rank=0, title=hot_title, hot_value=1_000_000,
                       category="社会", source="manual")
        logger.warning("指定热点不在榜单中，使用手动构造条目")
        return SelectedTopic(hot_item=mock, score=1.0, create_angle="按指定热点创作", risk_level=0)
