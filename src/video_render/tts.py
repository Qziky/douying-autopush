"""语音合成：统一 TTS 接口，支持多引擎切换。

- engine=edge: 微软 edge-tts 免费在线语音（无需 API Key）
- mock=True  : 生成静音占位 WAV（无网络/演示环境，保证全流程可跑通）

对外接口::

    engine = TTSEngine()
    audio_path, duration = engine.generate(text, output_name="demo")
"""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Tuple

from src.common.config import ConfigManager
from src.common.logger import get_logger

logger = get_logger("video_render.tts")


class TTSEngine:
    """语音合成引擎"""

    def __init__(self):
        self.config = ConfigManager()
        cfg = self.config.get("tts", {})
        self.engine = cfg.get("engine", "edge")
        self.voice = cfg.get("voice", "zh-CN-XiaoxiaoNeural")
        self.rate = cfg.get("rate", "+0%")
        self.volume = cfg.get("volume", "+0%")
        self.mock = bool(cfg.get("mock", False))
        temp_dir = self.config.resolve_path("cache/temp")
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_dir

    def generate(self, text: str, output_name: str = "audio") -> Tuple[str, float]:
        """生成语音，返回 (音频文件路径, 时长秒)。"""
        if not text or not text.strip():
            raise ValueError("TTS 输入文本为空")
        if self.mock:
            return self._gen_mock(text, output_name)
        if self.engine == "edge":
            return self._gen_edge(text, output_name)
        raise ValueError(f"不支持的 TTS 引擎: {self.engine}")

    # ---------------- edge-tts ----------------
    def _gen_edge(self, text: str, output_name: str) -> Tuple[str, float]:
        try:
            import edge_tts
        except ImportError as exc:
            logger.warning("edge-tts 未安装，降级为静音音频：%s", exc)
            return self._gen_mock(text, output_name)

        out_path = self.temp_dir / f"{output_name}.mp3"
        rate = self.rate if self.rate.startswith(("+", "-")) else f"+{self.rate}"
        last_exc = None
        for attempt in range(1, 4):  # 偶发 “No audio received” 时重试，最多 3 次
            try:
                asyncio.run(edge_tts.Communicate(
                    text, voice=self.voice, rate=rate, volume=self.volume
                ).save(str(out_path)))
                if out_path.exists() and out_path.stat().st_size > 0:
                    duration = self._duration_of(out_path)
                    logger.info("TTS 完成：%s，%.1fs", out_path.name, duration)
                    return str(out_path), duration
                last_exc = RuntimeError("输出音频为空")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("edge-tts 第 %d 次合成失败(%s)，重试...", attempt, exc)
                import time as _time
                _time.sleep(1.2 * attempt)
        logger.warning("edge-tts 多次失败，降级为静音音频：%s", last_exc)
        return self._gen_mock(text, output_name)

    # ---------------- mock 静音 ----------------
    def _gen_mock(self, text: str, output_name: str) -> Tuple[str, float]:
        seconds = max(len(text) / 4.5, 3.0)  # 4.5字/秒估算时长
        out_path = self.temp_dir / f"{output_name}_mock.wav"
        rate, amp = 44100, 100
        frames = int(seconds * rate)
        with wave.open(str(out_path), "w") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(rate)
            f.writeframes(b"\x00\x00" * frames)
        logger.info("TTS(mock 静音)完成：%s，%.1fs", out_path.name, seconds)
        return str(out_path), seconds

    # ---------------- 工具 ----------------
    def _duration_of(self, audio_path: Path) -> float:
        try:
            from moviepy import AudioFileClip
            with AudioFileClip(str(audio_path)) as clip:
                return float(clip.duration)
        except Exception:  # noqa: BLE001
            # 退而求其次：按文件大小估算（128kbps mp3 约 16KB/s）
            size = audio_path.stat().st_size
            return max(size / 16_000, 1.0)
