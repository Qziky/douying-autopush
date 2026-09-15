"""大模型客户端：统一封装 OpenAI 兼容接口，支持多家供应商切换。

支持 provider: openai / deepseek / moonshot / custom
- openai  : https://api.openai.com/v1
- deepseek: https://api.deepseek.com/v1
- moonshot: https://api.moonshot.cn/v1
- custom  : 自定义 base_url

未配置 API Key 时，is_available() 返回 False，上层自动切换演示生成器。
"""
from __future__ import annotations

import json
from typing import Optional

import requests

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.retry import retry

logger = get_logger("content_gen.llm")

_PROVIDER_DEFAULTS = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "custom": {"base_url": "", "model": ""},
}


class LLMClient:
    """OpenAI 兼容 LLM 客户端"""

    def __init__(self):
        self.config = ConfigManager()
        cfg = self.config.get("content_gen.llm", {})
        provider = cfg.get("provider", "openai")
        defaults = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["openai"])
        self.provider = provider
        self.base_url = (cfg.get("base_url") or defaults["base_url"]).rstrip("/")
        self.api_key = cfg.get("api_key", "") or ""
        self.model = cfg.get("model") or defaults["model"]
        self.temperature = float(cfg.get("temperature", 0.8))
        self.max_tokens = int(cfg.get("max_tokens", 2048))
        self.timeout = int(cfg.get("timeout", 60))
        self.retry_times = int(cfg.get("retry", 3))

    def is_available(self) -> bool:
        return bool(self.api_key and self.base_url)

    @retry(max_retries=3, delay=2, backoff=2.0)
    def chat(self, prompt: str, response_format: Optional[str] = None,
             temperature: Optional[float] = None) -> str:
        """调用对话接口，返回文本内容。

        Args:
            prompt: 用户消息
            response_format: "json" 时强制 JSON 输出（部分模型忽略）
            temperature: 覆盖默认温度
        """
        if not self.is_available():
            raise RuntimeError("LLM 未配置 API Key，无法调用")

        payload: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": self.max_tokens,
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"

        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise ValueError("LLM 返回内容为空")
        return content

    def chat_json(self, prompt: str, temperature: Optional[float] = None) -> dict:
        """调用并解析 JSON 输出（多重容错，适配模型偶发的不规范输出）"""
        raw = self.chat(prompt, response_format="json", temperature=temperature)
        return self._parse_json_lenient(raw)

    @staticmethod
    def _parse_json_lenient(raw: str) -> dict:
        """从可能含代码块/前言/多段/截断的文本中稳健提取 JSON 对象。"""
        import re
        if not raw or not raw.strip():
            raise ValueError("LLM 返回为空")
        text = raw.strip()
        # 1) 剥离 ```json ... ``` 代码块
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1).strip()
        # 2) 整体直接解析
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        # 3) 扫描所有顶层花括号起点，raw_decode 逐个解析（处理 Extra data / 多段输出）
        decoder = json.JSONDecoder()
        candidates = []
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _end = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    candidates.append(obj)
            except json.JSONDecodeError:
                continue
        if candidates:
            # 优先返回真正的脚本对象
            for obj in candidates:
                if "scenes" in obj or "full_script" in obj:
                    return obj
            return candidates[0]
        # 4) 截断修复兜底
        start = text.find("{")
        if start >= 0:
            return json.loads(LLMClient._repair_truncated_json(text[start:]))
        raise ValueError("LLM 返回中未找到合法 JSON")

    @staticmethod
    def _repair_truncated_json(text: str) -> str:
        """修复末尾被截断的 JSON：闭合字符串、数组、对象。"""
        text = text.rstrip().rstrip(",")
        # 统计未闭合的引号
        if text.count('"') % 2 == 1:
            text += '"'
        # 按栈补全括号
        stack = []
        in_str = False
        escaped = False
        for ch in text:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in "[{":
                stack.append("]" if ch == "[" else "}")
            elif ch in "]}" and stack:
                stack.pop()
        return text + "".join(reversed(stack))
