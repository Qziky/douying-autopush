"""配置管理：单例模式，统一读取 YAML 配置，支持默认配置合并与热加载。

配置读取优先级：
1. 项目根目录 config/settings.yaml（主配置）
2. 用户自定义覆盖文件 config/settings.local.yaml（可选，git 忽略）

所有相对路径基于项目根目录解析，支持在任意工作目录下运行。
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Any, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class ConfigManager:
    """全局配置单例"""
    _instance: Optional["ConfigManager"] = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._load_config()
                cls._instance = inst
            return cls._instance

    def _load_config(self) -> None:
        main_path = PROJECT_ROOT / "config" / "settings.yaml"
        if not main_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {main_path}")
        with open(main_path, "r", encoding="utf-8") as f:
            self.config: dict = yaml.safe_load(f) or {}

        # 用户自定义覆盖（可选）
        local_path = PROJECT_ROOT / "config" / "settings.local.yaml"
        if local_path.exists():
            with open(local_path, "r", encoding="utf-8") as f:
                local = yaml.safe_load(f) or {}
            self._deep_merge(self.config, local)

        # 规范化项目根路径
        root_cfg = self.config.setdefault("project", {})
        root_cfg["root"] = str(PROJECT_ROOT)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> None:
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                ConfigManager._deep_merge(base[k], v)
            else:
                base[k] = v

    def get(self, key: str, default: Any = None) -> Any:
        """点分路径取值：config.get('video.default_duration', 60)"""
        keys = key.split(".")
        value: Any = self.config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default

    def set(self, key: str, value: Any) -> None:
        """运行时覆盖某项配置（支持点分路径）"""
        keys = key.split(".")
        target = self.config
        for k in keys[:-1]:
            target = target.setdefault(k, {})
        target[keys[-1]] = value

    def reload(self) -> None:
        with self._lock:
            self._load_config()

    def resolve_path(self, rel: str) -> Path:
        """将配置中的相对路径解析为基于项目根目录的绝对路径"""
        p = Path(rel)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p.resolve()

    @property
    def root(self) -> Path:
        return PROJECT_ROOT


def get_config() -> ConfigManager:
    return ConfigManager()
