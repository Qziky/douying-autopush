"""字幕生成：为每个分镜生成对齐画面的字幕 PNG（透明底、白字黑描边）。

用 Pillow 预渲染字幕图，再交给合成器叠加，避免依赖 ImageMagick。
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from PIL import Image, ImageDraw, ImageFont

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.video_render.material import _load_font

logger = get_logger("video_render.subtitle")


class SubtitleGenerator:
    """字幕生成器"""

    def __init__(self):
        self.config = ConfigManager()
        sub_cfg = self.config.get("video.subtitle", {})
        self.enabled = bool(sub_cfg.get("enabled", True))
        self.font_size_ratio = float(sub_cfg.get("font_size_ratio", 0.055))
        self.position = sub_cfg.get("position", "bottom")
        self.margin_ratio = float(sub_cfg.get("margin_ratio", 0.08))
        self.max_chars = int(sub_cfg.get("max_chars_per_line", 14))
        self.color = sub_cfg.get("color", "#FFFFFF")
        self.stroke_color = sub_cfg.get("stroke_color", "#000000")
        self.resolution = tuple(int(x) for x in self.config.get("video.resolution", [1080, 1920]))
        temp_dir = self.config.resolve_path("cache/temp")
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = temp_dir

    def generate(self, scenes: List, prefix: str = "sub") -> List[str]:
        """为每个场景生成字幕 PNG，返回路径列表。"""
        paths: List[str] = []
        for i, scene in enumerate(scenes, 1):
            path = self.temp_dir / f"{prefix}_{i:02d}.png"
            if not scene.text:
                continue
            self._render(str(path), scene.text)
            paths.append(str(path))
        logger.info("字幕生成完成：%d 张", len(paths))
        return paths

    def _render(self, path: str, text: str) -> None:
        w, h = self.resolution
        font_size = max(int(w * self.font_size_ratio), 20)
        font = _load_font(font_size)

        # 按最大宽度自动换行
        lines = self._wrap(text, font, int(w * 0.88))
        line_height = int(font_size * 1.35)

        # 先量尺寸
        tmp = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(tmp)
        max_w = max(draw.textlength(line, font=font) for line in lines)
        text_h = line_height * len(lines)

        # 再画到透明底图（居中）
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        y0 = int(h * (1 - self.margin_ratio)) - text_h if self.position == "bottom" else (h - text_h) // 2
        y0 = max(20, y0)
        for line in lines:
            tw = draw.textlength(line, font=font)
            x = (w - tw) // 2
            draw.text((x, y0), line, font=font, fill=self.color,
                      stroke_width=max(font_size // 18, 2), stroke_fill=self.stroke_color)
            y0 += line_height
        img.save(path, "PNG")

    @staticmethod
    def _wrap(text: str, font: ImageFont.ImageFont, max_width: int) -> List[str]:
        """按字符数 + 像素宽度双重约束换行"""
        chars = list(text)
        lines: List[str] = []
        cur = ""
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        for ch in chars:
            if draw.textlength(cur + ch, font=font) > max_width and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines
