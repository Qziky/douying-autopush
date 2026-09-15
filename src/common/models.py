"""核心数据契约：模块间仅通过这些标准对象交互。

与《技术设计文档》第 3 节保持一致，并补充任务记录（TaskRecord）用于落库。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple


@dataclass
class HotItem:
    """原始热点项"""
    rank: int
    title: str
    hot_value: int
    category: str
    url: str = ""
    tags: List[str] = field(default_factory=list)
    source: str = "api"      # api / playwright / mock，标识数据来源


@dataclass
class SelectedTopic:
    """最终选题"""
    hot_item: HotItem
    score: float
    create_angle: str
    risk_level: int = 0      # 0低危 1中危 2高危


@dataclass
class Scene:
    """单条分镜"""
    index: int
    text: str                # 口播台词
    image_keyword: str       # 画面关键词
    duration: float = 3.0    # 时长(秒)


@dataclass
class VideoScript:
    """标准化视频脚本"""
    topic: SelectedTopic
    title: str
    full_script: str
    tags: List[str]
    scenes: List[Scene]
    total_duration: int
    cover_text: str


@dataclass
class RenderResult:
    """渲染结果"""
    video_path: str
    cover_path: str
    duration: float
    resolution: Tuple[int, int]
    audio_path: str = ""


@dataclass
class PublishResult:
    """发布结果"""
    success: bool
    video_url: Optional[str] = None
    publish_time: Optional[str] = None
    error_msg: Optional[str] = None
    account: str = ""


@dataclass
class TaskRecord:
    """一次任务执行记录（落库）"""
    task_id: str
    started_at: str
    finished_at: Optional[str] = None
    status: str = "pending"          # pending / running / success / failed / skipped
    stage: str = ""                  # 当前阶段
    hot_title: str = ""
    script_title: str = ""
    video_path: str = ""
    publish_url: str = ""
    error_msg: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
