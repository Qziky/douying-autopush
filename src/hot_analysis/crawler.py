"""热点采集子模块：抓取抖音网页版热点榜，输出标准化的原始热点列表。

采集通道（按配置优先级降级）:
1. api        - 直接请求抖音热榜接口（先取 ttwid cookie，再带 Cookie 请求）
2. playwright - 渲染热榜页并拦截接口响应（页面 DOM 结构易变，优先抓接口）
3. mock       - 内置演示数据（标注 source=mock，仅用于演示与联调）

设计原则：采集逻辑与决策逻辑严格分离，本模块只负责“拿到数据”。
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import asdict
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.models import HotItem
from src.common.retry import retry

logger = get_logger("hot.crawler")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_MOCK_HOT_LIST: List[dict] = [
    {"rank": 1, "title": "2026世界人工智能大会在上海开幕", "hot_value": 9860000, "category": "科技",
     "tags": ["人工智能", "科技"], "trend": 0.12},
    {"rank": 2, "title": "多地公布中秋国庆假期安排", "hot_value": 8520000, "category": "社会",
     "tags": ["假期", "民生"], "trend": 0.08},
    {"rank": 3, "title": "教育部发布义务教育新课程方案", "hot_value": 7310000, "category": "教育",
     "tags": ["教育", "政策"], "trend": 0.18},
    {"rank": 4, "title": "央行宣布下调存款准备金率", "hot_value": 6680000, "category": "财经",
     "tags": ["财经", "货币政策"], "trend": 0.05},
    {"rank": 5, "title": "国产大飞机C919新增国际航线", "hot_value": 5900000, "category": "科技",
     "tags": ["航空", "国产"], "trend": 0.22},
    {"rank": 6, "title": "国庆档多部影片定档", "hot_value": 5240000, "category": "娱乐",
     "tags": ["电影", "国庆"], "trend": 0.10},
    {"rank": 7, "title": "医保谈判药品目录更新落地", "hot_value": 4890000, "category": "社会",
     "tags": ["医保", "民生"], "trend": 0.15},
    {"rank": 8, "title": "秋季人才招聘会启动", "hot_value": 4320000, "category": "社会",
     "tags": ["就业", "招聘"], "trend": 0.07},
    {"rank": 9, "title": "新能源汽车出口创新高", "hot_value": 3980000, "category": "财经",
     "tags": ["新能源", "出口"], "trend": 0.20},
    {"rank": 10, "title": "多地博物馆推出夜游活动", "hot_value": 3550000, "category": "社会",
     "tags": ["文化", "旅游"], "trend": 0.11},
    {"rank": 11, "title": "电竞亚运集训名单公布", "hot_value": 3120000, "category": "娱乐",
     "tags": ["电竞", "体育"], "trend": 0.09},
    {"rank": 12, "title": "科学家发现新型可降解材料", "hot_value": 2860000, "category": "科技",
     "tags": ["材料", "科研"], "trend": 0.16},
    {"rank": 13, "title": "秋季进补指南引发热议", "hot_value": 2410000, "category": "健康",
     "tags": ["健康", "养生"], "trend": 0.06},
    {"rank": 14, "title": "中小学课后服务再升级", "hot_value": 2030000, "category": "教育",
     "tags": ["教育", "双减"], "trend": 0.13},
    {"rank": 15, "title": "中秋月饼市场新趋势", "hot_value": 1760000, "category": "财经",
     "tags": ["消费", "节日经济"], "trend": 0.04},
]


class HotCrawler:
    """热点采集器"""

    def __init__(self):
        self.config = ConfigManager()
        cfg = self.config.get("hot_analysis", {})
        self.source_order = cfg.get("source_order", ["api", "playwright", "mock"])
        self.limit = int(cfg.get("fetch_limit", 30))
        self.timeout = int(cfg.get("api.timeout", 15))
        self.headers = {"User-Agent": _UA, "Referer": "https://www.douyin.com/"}

    # ---------------- 对外入口 ----------------
    def fetch_hot_list(self) -> List[HotItem]:
        """按优先级抓取热榜，逐级降级，返回标准化热点列表。"""
        last_error: Optional[Exception] = None
        for source in self.source_order:
            method = getattr(self, f"_fetch_by_{source}", None)
            if method is None:
                logger.warning("未知采集通道: %s，跳过", source)
                continue
            try:
                items = method()
                if not items:
                    logger.warning("通道 %s 返回空数据，降级", source)
                    continue
                items = items[:self.limit]
                logger.info("热点采集成功：通道=%s，共 %d 条", source, len(items))
                return items
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("通道 %s 采集失败(%s)，降级到下一通道", source, exc)
        raise RuntimeError(f"所有采集通道均失败: {last_error}")

    # ---------------- api 通道 ----------------
    def _fetch_by_api(self) -> List[HotItem]:
        session = requests.Session()
        session.headers.update(self.headers)
        # 1) 先访问首页取 ttwid cookie
        home = self.config.get("hot_analysis.api.url", "").rsplit("/aweme", 1)[0] or "https://www.douyin.com/"
        try:
            session.get(home, timeout=self.timeout)
        except requests.RequestException:
            pass  # cookie 拿不到也不致命，仍尝试接口

        api_url = self.config.get("hot_analysis.api.url")
        params = {
            "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
            "detail_list": "1", "source": "6", "pc_client_type": "1",
            "version_code": "170400", "version_name": "17.4.0",
            "cookie_enabled": "true", "browser_language": "zh-CN",
            "browser_platform": "Win32", "browser_name": "Chrome",
            "browser_version": "126.0.0.0", "browser_online": "true",
            "engine_name": "Blink", "os_name": "Windows", "os_version": "10",
            "platform": "PC",
        }
        resp = self._request_with_retry(session, api_url, params=params)
        data = resp.json()
        word_list = (data.get("data") or {}).get("word_list") or []
        items: List[HotItem] = []
        for w in word_list:
            # 跳过置顶/异常条目（无排名或无热度值）
            if w.get("position") is None or not (w.get("hot_value") or 0):
                continue
            label_list = w.get("label_list") or []
            tags = [lb.get("name", "") for lb in label_list if lb.get("name")]
            category = tags[0] if tags else self._infer_category(str(w.get("word", "")))
            items.append(HotItem(
                rank=int(w.get("position", len(items) + 1)),
                title=w.get("word", "").strip(),
                hot_value=int(w.get("hot_value", 0) or 0),
                category=category,
                url=w.get("schema_url", "") or "",
                tags=tags or [category],
                source="api",
            ))
        if not items:
            raise ValueError("api 接口返回数据为空（可能需要登录 Cookie）")
        return items

    @staticmethod
    def _infer_category(title: str) -> str:
        """接口未给分类标签时，按标题关键词推断领域分类。"""
        rules = [
            ("体育", ["足球", "篮球", "女足", "男篮", "世界杯", "国足", "中超", "CBA", "奥运",
                      "亚运", "比赛", "赛事", "夺冠", "晋级", "出线", "U20", "U23", "乒乓", "羽毛球"]),
            ("财经", ["央行", "证券", "股市", "基金", "房价", "经济", "出口", "贸易", "消费",
                      "价格", "财报", "上市", "融资", "服贸", "金融", "利率", "汇率", "预算"]),
            ("科技", ["人工智能", "AI", "芯片", "机器人", "航天", "卫星", "手机", "华为", "苹果",
                      "新能源", "5G", "大模型", "科技", "软件", "算力", "量子", "生物科技", "材料"]),
            ("教育", ["高考", "考研", "学校", "学生", "教师", "大学", "中小学", "课程", "考试",
                      "录取", "双减", "教育", "招生", "志愿"]),
            ("娱乐", ["演唱会", "电影", "电视剧", "综艺", "明星", "娱乐圈", "票房", "音乐", "歌手",
                      "影视", "节目", "颁奖"]),
            ("健康", ["健康", "医院", "疾病", "医保", "养生", "医生", "药品", "疫苗", "体检"]),
            ("旅游", ["旅行", "旅游", "景点", "目的地", "假期", "出游", "地标", "博物馆", "文旅"]),
            ("国际", ["国际", "美国", "欧盟", "俄罗斯", "联合国", "全球", "金砖", "海外"]),
        ]
        for category, keywords in rules:
            if any(kw in title for kw in keywords):
                return category
        return "社会"

    @retry(max_retries=3, delay=2)
    def _request_with_retry(self, session: requests.Session, url: str, params: dict) -> requests.Response:
        resp = session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    # ---------------- playwright 通道 ----------------
    def _fetch_by_playwright(self) -> List[HotItem]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("未安装 playwright，请先 pip install playwright && playwright install chromium") from exc

        hot_url = self.config.get("hot_analysis.playwright.url", "https://www.douyin.com/hot")
        timeout_ms = int(self.config.get("hot_analysis.playwright.timeout", 30000))

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_UA,
                locale="zh-CN",
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()

            captured: dict = {}

            def _on_response(response):
                if "hot/search/list" in response.url and not captured:
                    try:
                        captured["data"] = response.json()
                    except Exception:  # noqa: BLE001
                        pass

            page.on("response", _on_response)
            try:
                page.goto(hot_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(6000)  # 等待接口返回
            finally:
                pass

            items: List[HotItem] = []
            data = captured.get("data") or {}
            word_list = (data.get("data") or {}).get("word_list") or []
            for w in word_list:
                label_list = w.get("label_list") or []
                tags = [lb.get("name", "") for lb in label_list if lb.get("name")]
                items.append(HotItem(
                    rank=int(w.get("position", len(items) + 1)),
                    title=w.get("word", "").strip(),
                    hot_value=int(w.get("hot_value", 0) or 0),
                    category=tags[0] if tags else "热点",
                    url=w.get("schema_url", "") or "",
                    tags=tags,
                    source="playwright",
                ))
            browser.close()
            if not items:
                raise RuntimeError("playwright 通道未捕获到热榜接口数据")
            return items

    # ---------------- mock 通道 ----------------
    def _fetch_by_mock(self) -> List[HotItem]:
        enabled = self.config.get("hot_analysis.mock.enabled", True)
        if not enabled:
            raise RuntimeError("mock 通道已在配置中禁用")
        time.sleep(0.2)
        items = []
        for row in _MOCK_HOT_LIST:
            items.append(HotItem(
                rank=row["rank"],
                title=row["title"],
                hot_value=row["hot_value"],
                category=row["category"],
                url="",
                tags=row["tags"],
                source="mock",
            ))
        logger.warning("当前使用内置演示数据（mock），仅用于联调，请勿用于真实发布决策")
        return items

    # ---------------- 工具 ----------------
    @staticmethod
    def _bs4_parse(html: str) -> List[HotItem]:
        """备用的纯 BS4 解析逻辑（对 SSR 页面可用），当前主链路未使用。"""
        soup = BeautifulSoup(html, "html.parser")
        items: List[HotItem] = []
        for idx, node in enumerate(soup.select(".hot-item, [class*=hot-item]"), 1):
            title = node.get_text(strip=True)
            if not title:
                continue
            items.append(HotItem(rank=idx, title=title, hot_value=0, category="热点",
                                 url="", tags=[], source="api"))
        return items
