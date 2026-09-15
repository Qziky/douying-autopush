"""视频合成器：素材 + 语音 + 字幕 -> 竖屏口播 MP4 + 封面图。

流程:
1. TTS 生成语音（并行生成字幕所需分镜信息）
2. 逐场景匹配素材图
3. 逐场景生成字幕 PNG
4. 按分镜时长拼接画面，叠加字幕，对齐音频
5. 导出 MP4，截取封面

兼容 moviepy 2.x（from moviepy import ...）与 1.x（from moviepy.editor import *）。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.models import RenderResult, VideoScript
from src.video_render.material import MaterialService
from src.video_render.subtitle import SubtitleGenerator
from src.video_render.tts import TTSEngine

logger = get_logger("video_render.composer")

# moviepy 版本兼容导入
try:  # moviepy 2.x
    from moviepy import (
        AudioArrayClip, AudioFileClip, ImageClip, concatenate_videoclips,
    )
    MOVIEPY_V2 = True
except ImportError:  # moviepy 1.x
    from moviepy.editor import (  # type: ignore
        AudioArrayClip, AudioFileClip, ImageClip, concatenate_videoclips,
    )
    MOVIEPY_V2 = False


class VideoComposer:
    """视频合成器"""

    def __init__(self):
        self.config = ConfigManager()
        self.tts = TTSEngine()
        self.material = MaterialService()
        self.subtitle_gen = SubtitleGenerator()
        self.fps = int(self.config.get("video.fps", 30))
        self.codec = str(self.config.get("video.codec", "libx264"))
        self.hw_encoder = str(self.config.get("video.hardware_encoder", "") or "")
        self.preset = str(self.config.get("video.preset", "faster"))
        self.resolution = tuple(int(x) for x in self.config.get("video.resolution", [1080, 1920]))
        self.transition = float(self.config.get("video.transition_seconds", 0.4))
        # 逐句配音时，句间停顿与结尾留白
        self.gap = float(self.config.get("tts.gap_seconds", 0.35))
        self.tail = float(self.config.get("tts.tail_seconds", 0.6))
        self.output_dir = self.config.resolve_path("output")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 对外入口 ----------------
    def compose(self, script: VideoScript, output_name: Optional[str] = None) -> RenderResult:
        """执行完整渲染，返回渲染结果。"""
        logger.info("开始渲染：%s（%d 秒，%d 分镜）",
                    script.title, script.total_duration, len(script.scenes))

        # 1) 逐场景 TTS：每句独立配音，按真实朗读时长对齐画面/字幕
        audio_segments = []
        for i, scene in enumerate(script.scenes, 1):
            path, dur = self.tts.generate(scene.text, output_name=f"audio_s{i:02d}")
            audio_segments.append((path, dur))

        # 用真实音频时长回写分镜时长（句间加停顿、结尾留白），保证音画严格同步
        n = len(script.scenes)
        for i, scene in enumerate(script.scenes):
            pause = self.gap if i < n - 1 else self.tail
            scene.duration = round(audio_segments[i][1] + pause, 2)
        script.total_duration = round(sum(s.duration for s in script.scenes), 2)
        logger.info("逐句配音对齐完成，实际总时长 %.1fs", script.total_duration)

        # 2) 素材图（每场景独立文件，避免文件名冲突）
        image_paths: List[str] = []
        for i, scene in enumerate(script.scenes, 1):
            img = self.material.get_image(scene.image_keyword, output_name=f"scene_{i:02d}")
            image_paths.append(img)

        # 3) 字幕图
        subtitle_paths = self.subtitle_gen.generate(script.scenes, prefix="sub")

        # 4) 拼画面（时长已与各句音频一致）
        video = self._build_video(script, image_paths, subtitle_paths)

        # 5) 拼接音频：numpy 统一波形（每句配音后接停顿），单音轨，严格对齐画面
        final_audio = self._build_audio_track(audio_segments)
        final = video.with_audio(final_audio)

        # 6) 输出
        safe_name = "".join(ch for ch in (output_name or script.title) if ch not in '\\/:*?"<>|')[:30]
        out_path = self.output_dir / f"{safe_name}.mp4"
        encoder = self.hw_encoder or self.codec
        logger.info("导出视频（编码器=%s，分辨率=%dx%d，%d fps）...",
                    encoder, *self.resolution, self.fps)
        final.write_videofile(
            str(out_path), fps=self.fps, codec=encoder, audio_codec="aac",
            threads=4, preset=self.preset,
            logger=None,
        )

        # 7) 封面
        cover_path = self._make_cover(final, out_path)

        result = RenderResult(
            video_path=str(out_path),
            cover_path=cover_path,
            duration=float(final.duration),
            resolution=self.resolution,
            audio_path=audio_segments[0][0] if audio_segments else "",
        )
        logger.info("渲染完成：%s（%.1fs）", out_path.name, result.duration)
        return result

    # ---------------- 画面构建 ----------------
    def _build_video(self, script: VideoScript, image_paths: List[str],
                     subtitle_paths: List[str]):
        import numpy as np
        from PIL import Image

        clips = []
        for i, scene in enumerate(script.scenes):
            dur = max(float(scene.duration), 0.5)
            img_path = image_paths[i] if i < len(image_paths) else image_paths[-1]
            frame = np.asarray(Image.open(img_path).convert("RGB")).copy()
            # 字幕预合成到画面（一次性 numpy 运算，避免逐帧 mask 合成）
            if i < len(subtitle_paths):
                sub = np.asarray(Image.open(subtitle_paths[i]).convert("RGBA"))
                alpha = sub[..., 3:4].astype(np.float32) / 255.0
                frame = (frame.astype(np.float32) * (1 - alpha)
                         + sub[..., :3].astype(np.float32) * alpha).astype(np.uint8)
            clip = ImageClip(frame).with_duration(dur)
            if self.transition > 0:
                fade = min(self.transition, dur / 2)
                clip = clip.with_effects([
                    VFade(duration=fade),
                    VFade(duration=fade, fade_out=True),
                ])
            clips.append(clip)

        return concatenate_videoclips(clips, method="compose")

    # ---------------- 音轨构建 ----------------
    def _build_audio_track(self, audio_segments: List[tuple], audio_fps: int = 44100):
        """把逐句配音波形首尾拼接（句间/结尾插入静音），返回单条 AudioArrayClip。

        用 numpy 直接拼波形而非 concatenate_audioclips，避免不同来源 clip
        的数组布局不一致导致的广播错误；音轨时间轴与分镜画面严格一致。
        """
        import numpy as np
        n = len(audio_segments)
        waves = []
        for i, (path, _dur) in enumerate(audio_segments):
            clip = AudioFileClip(path)
            frames = np.array(list(clip.iter_frames(fps=audio_fps)),
                              dtype=np.float32).squeeze()
            if frames.ndim == 1:  # 单声道 -> 双声道
                frames = np.stack([frames, frames], axis=1)
            waves.append(frames)
            pause = self.gap if i < n - 1 else self.tail
            waves.append(np.zeros((max(int(pause * audio_fps), 1), 2), dtype=np.float32))
            clip.close()
        whole = np.ascontiguousarray(np.concatenate(waves, axis=0))
        # moviepy 2.x AudioArrayClip 输入布局为 (samples, channels)
        return AudioArrayClip(whole, fps=audio_fps)

    # ---------------- 封面 ----------------
    def _make_cover(self, video, out_path: Path) -> str:
        cover_second = self.config.get("video.cover_second")
        cover_second = float(cover_second) if cover_second not in (None, "") else 1.0
        cover_path = out_path.with_suffix(".jpg")
        t = min(cover_second, max(float(video.duration) - 0.1, 0))
        frame = video.get_frame(t)
        # save_frame 对 RGBA 写 JPEG 会失败，这里用 PIL 手动转 RGB
        from PIL import Image
        if frame.ndim == 3 and frame.shape[2] == 4:
            import numpy as _np
            rgb = frame[..., :3].astype(_np.float32)
            alpha = frame[..., 3:4].astype(_np.float32) / 255.0
            frame = (rgb * alpha + 0).astype(_np.uint8)  # alpha 合成到黑底
        Image.fromarray(frame).save(cover_path, "JPEG", quality=90)
        logger.info("封面截取完成：%s", cover_path.name)
        return str(cover_path)


# 轻量转场：淡入/淡出（moviepy 2.x vfx 效果层；1.x 回退到 effects 模块）
def VFade(duration: float, fade_out: bool = False):
    try:
        from moviepy import vfx
        cls = vfx.FadeOut if fade_out else vfx.FadeIn
        return cls(duration=duration)
    except Exception:  # noqa: BLE001
        try:
            from moviepy.editor import vfx as vfx1
            cls = vfx1.FadeOut if fade_out else vfx1.FadeIn
            return cls(duration=duration)
        except Exception:  # noqa: BLE001
            from moviepy import vfx as _vfx
            return _vfx.FadeIn(duration=duration)
