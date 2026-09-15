"""素材服务：根据画面关键词自动搜索可商用素材库并本地缓存。

- provider=pexels : 调用 Pexels API（免费可商用），竖屏优先
- 本地素材优先   : use_local_first=True 时，cache/local 下命中优先
- 缓存机制       : 按关键词哈希命名，重复关键词不再下载
- 占位图兜底     : 无 API Key / 搜索失败时，用 Pillow 生成渐变占位图
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Optional

import requests

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.retry import retry

logger = get_logger("video_render.material")


class MaterialService:
    """素材服务"""

    def __init__(self):
        self.config = ConfigManager()
        cfg = self.config.get("material", {})
        self.provider = cfg.get("provider", "pexels")
        pexels = cfg.get("pexels", {})
        self.api_key = pexels.get("api_key", "") or ""
        self.base_url = pexels.get("base_url", "https://api.pexels.com/v1/search")
        self.per_page = int(pexels.get("per_page", 5))
        self.min_width = int(pexels.get("min_width", 1080))
        self.min_height = int(pexels.get("min_height", 1920))
        cache_dir = self.config.resolve_path(str(cfg.get("cache_dir", "cache/material")))
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_dir
        local_dir = self.config.resolve_path(str(cfg.get("local_dir", "cache/material/local")))
        local_dir.mkdir(parents=True, exist_ok=True)
        self.local_dir = local_dir
        self.use_local_first = bool(cfg.get("use_local_first", True))
        self.placeholder = bool(cfg.get("placeholder", True))

    # ---------------- 对外入口 ----------------
    def get_image(self, keyword: str, output_name: Optional[str] = None) -> str:
        """返回一张图片路径（下载/缓存/占位），尺寸统一预处理为配置分辨率。"""
        keyword = (keyword or "风景").strip()
        # 文件名带关键词哈希：避免按场景序号命名时跨脚本命中旧素材
        kw_hash = hashlib.md5(keyword.encode("utf-8")).hexdigest()[:8]
        prefix = output_name or hashlib.md5(keyword.encode("utf-8")).hexdigest()[:12]
        dest = self.cache_dir / f"{prefix}_{kw_hash}.jpg"

        # 1) 本地优先
        if self.use_local_first:
            local = self._find_local(keyword)
            if local is not None:
                logger.debug("素材命中本地库: %s -> %s", keyword, local.name)
                return self._resize_to_target(local, dest)

        # 2) 缓存命中
        if dest.exists() and dest.stat().st_size > 0:
            return str(dest)

        # 3) 远程搜索下载（未配置 API Key 直接占位，避免空转重试）
        if not self.api_key:
            if self.placeholder:
                self._make_placeholder(keyword, dest)
                return str(dest)
            raise RuntimeError(f"素材获取失败（未配置 Pexels API Key）: {keyword}")
        try:
            url = self._search_image(keyword)
            self._download(url, dest)
            return self._resize_to_target(dest, dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("素材搜索失败(%s)，关键词=%s", exc, keyword)

        # 4) 占位图兜底
        if self.placeholder:
            self._make_placeholder(keyword, dest)
            return str(dest)
        raise RuntimeError(f"素材获取失败: {keyword}")

    # ---------------- 本地 ---------------- 
    def _find_local(self, keyword: str) -> Optional[Path]:
        """本地素材库：文件名含关键词即命中（如 城市夜景.jpg）"""
        if not self.local_dir.exists():
            return None
        for p in sorted(self.local_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") \
                    and keyword.lower() in p.stem.lower():
                return p
        return None

    # ---------------- Pexels ----------------
    @retry(max_retries=2, delay=1.5)
    def _search_image(self, keyword: str) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 Pexels API Key（material.pexels.api_key）")
        resp = requests.get(
            self.base_url,
            params={"query": keyword, "per_page": self.per_page, "orientation": "portrait"},
            headers={"Authorization": self.api_key},
            timeout=15,
        )
        resp.raise_for_status()
        photos = (resp.json().get("photos") or [])
        for ph in photos:
            src = ph.get("src") or {}
            for key in ("large2x", "large", "original"):
                url = src.get(key)
                if url:
                    return url
        raise RuntimeError(f"Pexels 无结果: {keyword}")

    @retry(max_retries=2, delay=1.5)
    def _download(self, url: str, dest: Path) -> None:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        tmp.replace(dest)

    # ---------------- 预处理 ----------------
    def _resize_to_target(self, src: Path, dest: Path) -> str:
        """缩放/裁剪到目标分辨率并转 JPG（居中裁剪，保持清晰）"""
        from PIL import Image
        target = tuple(int(x) for x in self.config.get("video.resolution", [1080, 1920]))
        img = Image.open(src).convert("RGB")
        img = _cover_resize(img, target)
        if dest.suffix.lower() != ".jpg":
            dest = dest.with_suffix(".jpg")
        img.save(dest, "JPEG", quality=88)
        return str(dest)

    def _make_placeholder(self, keyword: str, dest: Path) -> None:
        """生成带关键词的渐变占位图（确定性配色，同一关键词颜色稳定）"""
        from PIL import Image, ImageDraw, ImageFont
        w, h = (int(x) for x in self.config.get("video.resolution", [1080, 1920]))
        seed = int(hashlib.md5(keyword.encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        c1 = tuple(rng.randint(60, 200) for _ in range(3))
        c2 = tuple(max(0, v - 60) for v in c1)
        img = Image.new("RGB", (w, h), c1)
        draw = ImageDraw.Draw(img)
        for y in range(h):
            t = y / h
            color = tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))
            draw.line([(0, y), (w, y)], fill=color)
        font = _load_font(min(w, h) // 14)
        text = keyword[:10]
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((w - tw) / 2, (h - th) / 2), text, fill="white", font=font)
        img.save(dest, "JPEG", quality=88)
        logger.info("生成占位图：%s", dest.name)


def _cover_resize(img, target: tuple[int, int]) -> "Image.Image":
    """等比缩放后居中裁剪到目标尺寸"""
    from PIL import Image
    tw, th = target
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    img = img.resize((int(iw * scale) + 1, int(ih * scale) + 1), Image.LANCZOS)
    iw, ih = img.size
    left = (iw - tw) // 2
    top = (ih - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _load_font(size: int):
    """按系统加载中文字体（Windows/macOS/Linux 常见路径）"""
    from PIL import ImageFont
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()
