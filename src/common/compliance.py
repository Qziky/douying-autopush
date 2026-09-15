"""内容合规校验：关键词初筛 + LLM 复检两级校验。

- 第一级：本地敏感词库前缀树（Trie）匹配，快速拦截明确违规内容
- 第二级：可选 LLM 语义复检（由调用方注入 llm_check 回调，识别隐性违规）
- 命中敏感词默认终止流程（block_on_hit）
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from src.common.config import ConfigManager
from src.common.logger import get_logger

logger = get_logger("common.compliance")


@dataclass
class ComplianceResult:
    """合规校验结果"""
    passed: bool
    blocked_words: List[str] = field(default_factory=list)
    level: str = "pass"          # pass / warn / block
    reason: str = ""


class SensitiveWordTrie:
    """敏感词前缀树"""

    def __init__(self):
        self._root: dict = {}
        self._size = 0

    def add(self, word: str) -> None:
        node = self._root
        for ch in word:
            node = node.setdefault(ch, {})
        node["#"] = True
        self._size += 1

    def search(self, text: str) -> List[str]:
        """返回文本中命中的所有敏感词"""
        hits: List[str] = []
        n = len(text)
        i = 0
        while i < n:
            node = self._root
            j = i
            word = ""
            while j < n and text[j] in node:
                node = node[text[j]]
                word += text[j]
                if node.get("#"):
                    hits.append(word)
                j += 1
            i += 1
        return hits

    @property
    def size(self) -> int:
        return self._size


_PUNCT = "，。！？、；：\"\"''（）【】《》 ,.!?;:'\"()<>|/\\-_*~`@#$%^&+={}[]"


def _normalize(text: str) -> str:
    """归一化：全角转半角、大写转小写、去除空白与标点，防止变形绕过"""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub("[" + re.escape(_PUNCT) + r"\s\u3000]", "", text)
    return text


class ComplianceChecker:
    """合规校验器（单例）"""

    _instance: Optional["ComplianceChecker"] = None

    def __new__(cls):
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._load_words()
            cls._instance = inst
        return cls._instance

    def _load_words(self) -> None:
        cfg = ConfigManager()
        path = cfg.resolve_path(str(cfg.get("compliance.sensitive_words_file", "config/sensitive_words.txt")))
        self.trie = SensitiveWordTrie()
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    word = line.strip()
                    if word and not word.startswith("#"):
                        self.trie.add(_normalize(word))
        self.llm_second_check = bool(cfg.get("compliance.llm_second_check", False))
        self.block_on_hit = bool(cfg.get("compliance.block_on_hit", True))
        logger.info("敏感词库加载完成：%d 词", self.trie.size)

    def check(self, text: str, llm_check: Optional[Callable[[str], str]] = None) -> ComplianceResult:
        """对单段文本做合规校验。

        Args:
            text: 待校验文本
            llm_check: 可选 LLM 语义复检回调，入参为文本，返回 'pass'/'block'/'warn'
        """
        normalized = _normalize(text)
        hits = self.trie.search(normalized)
        if hits:
            reason = f"命中敏感词: {', '.join(sorted(set(hits))[:5])}"
            logger.warning("合规校验未通过：%s", reason)
            return ComplianceResult(passed=False, blocked_words=sorted(set(hits)), level="block", reason=reason)

        if self.llm_second_check and llm_check:
            verdict = llm_check(text)
            if verdict in ("block", "warn"):
                reason = f"LLM 语义复检未通过: {verdict}"
                logger.warning("合规校验未通过：%s", reason)
                return ComplianceResult(passed=False, level=verdict, reason=reason)

        return ComplianceResult(passed=True, level="pass", reason="校验通过")

    def check_script(self, title: str, full_script: str, tags: list[str],
                     llm_check: Optional[Callable[[str], str]] = None) -> ComplianceResult:
        """对完整脚本（标题+正文+话题）做合规校验。"""
        combined = "\n".join([title, full_script, " ".join(tags)])
        result = self.check(combined, llm_check=llm_check)
        return result


def normalize(text: str) -> str:
    return _normalize(text)
