"""作品数据存储：SQLite 轻量数据库，记录任务执行与发布结果。

表结构:
    tasks(id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT,
          stage TEXT, hot_title TEXT, script_title TEXT, video_path TEXT,
          publish_url TEXT, error_msg TEXT, extra TEXT)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.models import TaskRecord

logger = get_logger("common.storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    stage TEXT,
    hot_title TEXT,
    script_title TEXT,
    video_path TEXT,
    publish_url TEXT,
    error_msg TEXT,
    extra TEXT
);
"""


class WorksDB:
    """作品数据库（单例）"""

    _instance: Optional["WorksDB"] = None

    def __new__(cls):
        if cls._instance is None:
            inst = super().__new__(cls)
            cfg = ConfigManager()
            db_path = cfg.resolve_path(str(cfg.get("storage.db_path", "data/works.db")))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            inst.path = db_path
            inst._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            inst._conn.execute(_SCHEMA)
            inst._conn.commit()
            cls._instance = inst
        return cls._instance

    def upsert_task(self, record: TaskRecord) -> None:
        row = (
            record.task_id, record.started_at, record.finished_at, record.status,
            record.stage, record.hot_title, record.script_title, record.video_path,
            record.publish_url, record.error_msg,
            json.dumps(record.extra, ensure_ascii=False),
        )
        self._conn.execute(
            """INSERT INTO tasks (id, started_at, finished_at, status, stage,
                                  hot_title, script_title, video_path, publish_url, error_msg, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 finished_at=excluded.finished_at, status=excluded.status, stage=excluded.stage,
                 hot_title=excluded.hot_title, script_title=excluded.script_title,
                 video_path=excluded.video_path, publish_url=excluded.publish_url,
                 error_msg=excluded.error_msg, extra=excluded.extra""",
            row)
        self._conn.commit()

    def get_task(self, task_id: str) -> Optional[dict]:
        cur = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        d["extra"] = json.loads(d["extra"] or "{}")
        return d

    def recent_tasks(self, limit: int = 20) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM tasks ORDER BY started_at DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["extra"] = json.loads(d["extra"] or "{}")
            rows.append(d)
        return rows

    def count_publish_today(self, day: str) -> int:
        """统计某日（YYYY-MM-DD）真实成功发布次数，用于发布限流（不含 dry-run 演练）"""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='success' AND started_at LIKE ?",
            (day + "%",))
        return int(cur.fetchone()[0])

    def close(self) -> None:
        self._conn.close()
