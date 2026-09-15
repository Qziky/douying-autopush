"""账号管理：多账号配置、Cookie 持久化、登录态检测与扫码登录引导。

- Cookie 持久化到 data/cookies/{account}.json（Playwright storage_state 格式）
- 登录态检测：访问创作服务平台，URL 回跳登录页则判定失效
- 失效时打开有头浏览器，引导用户扫码登录后自动保存
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from src.common.config import ConfigManager
from src.common.logger import get_logger

logger = get_logger("douyin.account")


@dataclass
class AccountInfo:
    """账号信息"""
    name: str
    enabled: bool = True
    remark: str = ""
    cookie_file: str = ""
    extra: Dict = field(default_factory=dict)


class AccountManager:
    """账号管理器"""

    LOGIN_REDIRECT_MARK = ("passport", "login", "sso")

    def __init__(self):
        self.config = ConfigManager()
        accounts_file = self.config.resolve_path(
            str(self.config.get("douyin.accounts_file", "config/accounts.yaml")))
        self.accounts_file = accounts_file
        cookie_dir = self.config.resolve_path(
            str(self.config.get("douyin.cookie_dir", "data/cookies")))
        cookie_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = cookie_dir
        self.home_url = self.config.get("douyin.home_url", "https://www.douyin.com/")
        self.publish_url = self.config.get(
            "douyin.publish_url",
            "https://creator.douyin.com/creator-micro/content/upload")

    # ---------------- 配置 ----------------
    def load_accounts(self) -> List[AccountInfo]:
        if not self.accounts_file.exists():
            return []
        with open(self.accounts_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        accounts = []
        for item in data.get("accounts", []):
            name = item.get("name", "")
            if not name:
                continue
            accounts.append(AccountInfo(
                name=name,
                enabled=bool(item.get("enabled", True)),
                remark=str(item.get("remark", "")),
                cookie_file=str(item.get("cookie_file") or f"data/cookies/{name}.json"),
                extra=item,
            ))
        return [a for a in accounts if a.enabled]

    def get_account(self, name: Optional[str] = None) -> Optional[AccountInfo]:
        accounts = self.load_accounts()
        if not accounts:
            return None
        if name:
            for a in accounts:
                if a.name == name:
                    return a
            return None
        return accounts[0]

    def cookie_path(self, account: AccountInfo) -> Path:
        return self.config.resolve_path(account.cookie_file)

    # ---------------- Cookie ----------------
    def has_cookie(self, account: AccountInfo) -> bool:
        path = self.cookie_path(account)
        return path.exists() and path.stat().st_size > 0

    def save_state(self, context, account: AccountInfo) -> None:
        path = self.cookie_path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(path))
        logger.info("Cookie 已保存：%s", path.name)

    # ---------------- 登录态 ----------------
    @staticmethod
    def _has_session(context) -> bool:
        """只读取 Cookie 判断是否已登录（不产生任何页面跳转，可高频轮询）。"""
        names = {c.get("name") for c in context.cookies()}
        return "sessionid" in names or "sessionid_ss" in names

    def is_logged_in(self, page) -> bool:
        """主动访问首页检测登录态（会产生一次跳转，仅用于启动时的一次性检测）。"""
        try:
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            url = page.url
            if any(mark in url for mark in self.LOGIN_REDIRECT_MARK):
                return False
            # 抖音登录态核心 Cookie：sessionid / sessionid_ss
            return self._has_session(page.context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("登录态检测异常：%s", exc)
            return False

    def ensure_login(self, page, account: AccountInfo) -> bool:
        """确保登录态；已登录直接返回 True，未登录引导扫码。

        注意：
        - 调用方需在创建 context 时通过 storage_state 加载本地 Cookie，
          见 new_context_with_state()。
        - 扫码等待期间只轮询 Cookie，绝不刷新/跳转页面，避免打断用户扫码。
        """
        if self.has_cookie(account):
            logger.info("账号 %s 存在本地 Cookie，检测登录态...", account.name)
            if self.is_logged_in(page):
                logger.info("账号 %s 登录态有效", account.name)
                return True
            logger.warning("账号 %s Cookie 已失效，需要重新扫码", account.name)

        # 引导扫码登录：只跳转一次到发布页（未登录会自动展示登录二维码）
        logger.info("请在弹出的浏览器窗口扫码登录抖音（账号：%s，3 分钟内有效）", account.name)
        page.goto(self.publish_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        deadline = time.time() + 180  # 3 分钟超时
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            # 关键：只看 Cookie，不 goto、不刷新，页面保持在扫码界面
            if self._has_session(page.context):
                page.wait_for_timeout(1500)  # 等待登录后页面跳转完成
                self.save_state(page.context, account)
                logger.info("账号 %s 扫码登录成功", account.name)
                return True
        logger.error("账号 %s 扫码登录超时（3 分钟）", account.name)
        return False

    # ---------------- 独立登录入口 ----------------
    def new_context_with_state(self, browser, account: AccountInfo, **kwargs):
        """创建浏览器上下文；存在本地 Cookie 时自动注入 storage_state。"""
        kwargs.setdefault("locale", "zh-CN")
        kwargs.setdefault("viewport", {"width": 1280, "height": 900})
        if self.has_cookie(account):
            kwargs["storage_state"] = str(self.cookie_path(account))
        return browser.new_context(**kwargs)

    def interactive_login(self, account_name: Optional[str] = None,
                          headless: bool = False) -> bool:
        """独立扫码登录入口：弹出浏览器 -> 扫码 -> 持久化 Cookie -> 关闭。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            logger.error("未安装 playwright：%s", exc)
            return False

        account = self.get_account(account_name)
        if account is None:
            logger.error("未找到可用账号，请先在 config/accounts.yaml 配置账号")
            return False

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = self.new_context_with_state(browser, account)
            page = context.new_page()
            ok = self.ensure_login(page, account)
            if ok:
                self.save_state(context, account)
                logger.info("登录完成，Cookie 已持久化，后续发布无需重复扫码")
            browser.close()
        return ok
