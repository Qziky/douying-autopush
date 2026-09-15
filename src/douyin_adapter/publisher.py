"""发布执行：基于 Playwright 模拟人工上传发布抖音作品。

- 元素选择器集中管理（页面改版只需改这一处）
- dry_run 演练模式：不真实发布，仅打印动作（默认开启，防止误发）
- 真实模式：上传视频 -> 填标题 -> 加话题 -> 选封面 -> 点发布
- 元素定位失败自动重试并截图保存现场

注意：抖音页面结构可能随版本变化，选择器失效时请依据截图更新 SELECTORS。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from src.common.config import ConfigManager
from src.common.logger import get_logger
from src.common.models import PublishResult, RenderResult, VideoScript
from src.douyin_adapter.account import AccountManager, AccountInfo
from src.douyin_adapter.risk_control import RiskController

logger = get_logger("douyin.publisher")

# 页面元素选择器集中管理（发布页，依据 2026-09 真实 DOM 校准）
SELECTORS = {
    "file_input": "input[type='file']",
    # 作品标题：普通 input（semi-input），placeholder 精确含“作品标题”（避开付费视频标题框）
    "title_input": "input[placeholder*='作品标题']",
    # 作品简介/话题：slate contenteditable 富文本编辑器
    "desc_editor": ("div.editor-kit-container[contenteditable='true'], "
                    "div[data-slate-editor='true'][contenteditable='true'], "
                    "div[contenteditable='true'][data-placeholder*='简介']"),
    # 话题候选下拉（# 触发）
    "topic_dropdown": ("[class*='mention'] [class*='item'], [class*='suggest'] li, "
                       "[class*='topic'] [class*='item'], li[class*='option']"),
    "cover_select": "[class*='cover'] img, [class*='poster'] img",
}

# 上传完成的页面文本标志 / 上传中文本标志
_UPLOAD_READY_MARKS = ("重新上传", "设置封面", "选择封面", "作品标题", "添加作品简介")
_UPLOADING_MARKS = ("上传中", "正在上传", "上传失败重试")
# 需要关闭的引导气泡按钮文本
_GUIDE_MARKS = ("我知道了", "知道了", "暂不", "以后再说", "关闭引导")

# “谁可以看”取值 -> 页面单选项文本
VISIBILITY_LABELS = {
    "public": "公开",
    "friends": "好友可见",
    "private": "仅自己可见",
}

_UPLOAD_WAIT_MS = 180_000
_PUBLISH_WAIT_MS = 60_000
_SECURITY_VERIFY_WAIT_MS = 240_000  # 短信/扫码等本人安全验证的等待窗口


class VideoPublisher:
    """抖音自动发布器"""

    def __init__(self):
        self.config = ConfigManager()
        self.account_mgr = AccountManager()
        self.risk_ctrl = RiskController()
        self.publish_url = self.config.get("douyin.publish_url")
        self.dry_run = bool(self.config.get("douyin.dry_run", True))
        self.headless = bool(self.config.get("douyin.headless", False))
        # 谁可以看：public 公开 / friends 好友可见 / private 仅自己可见
        self.visibility = str(self.config.get("douyin.visibility", "public")).lower()
        self.timeout_upload = int(self.config.get("douyin.timeout.upload", _UPLOAD_WAIT_MS))
        self.timeout_publish = int(self.config.get("douyin.timeout.publish", _PUBLISH_WAIT_MS))
        self.timeout_verify = int(self.config.get("douyin.timeout.security_verify",
                                                  _SECURITY_VERIFY_WAIT_MS))
        self.capture_dir = self.config.resolve_path("data/logs/fails")
        self.capture_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 对外入口 ----------------
    def publish(self, render_result: RenderResult, script: VideoScript,
                account_name: Optional[str] = None) -> PublishResult:
        """发布一个渲染好的视频。

        优先使用 dry_run 演练模式；真实发布前必须完成登录并关闭 dry_run。
        """
        account = self.account_mgr.get_account(account_name)
        if account is None:
            return PublishResult(success=False, error_msg="未配置可用账号（config/accounts.yaml）")

        # 频率校验
        allowed, reason = self.risk_ctrl.check_publish_allowed()
        if not allowed:
            logger.warning("发布被风控拦截：%s", reason)
            return PublishResult(success=False, error_msg=reason, account=account.name)

        if self.dry_run:
            return self._dry_run(render_result, script, account)

        return self._publish_real(render_result, script, account)

    # ---------------- 演练模式 ----------------
    def _dry_run(self, render_result: RenderResult, script: VideoScript,
                 account: AccountInfo) -> PublishResult:
        logger.info("【演练模式】以下为将真实执行的动作（dry_run=true 不会真发）：")
        logger.info("  账号    : %s", account.name)
        logger.info("  视频    : %s", render_result.video_path)
        logger.info("  标题    : %s", script.title)
        logger.info("  话题    : %s", " ".join(f"#{t}" for t in script.tags))
        logger.info("  封面    : %s", render_result.cover_path)
        logger.info("  谁可以看: %s", VISIBILITY_LABELS.get(self.visibility, self.visibility))
        logger.info("  发布页  : %s", self.publish_url)
        self.risk_ctrl.random_delay(0.5, 1.0)
        return PublishResult(
            success=True,
            video_url="(dry-run 未真实发布)",
            publish_time=time.strftime("%Y-%m-%d %H:%M:%S"),
            error_msg=None,
            account=account.name,
        )

    # ---------------- 真实发布 ----------------
    def _publish_real(self, render_result: RenderResult, script: VideoScript,
                      account: AccountInfo) -> PublishResult:
        if not Path(render_result.video_path).exists():
            return PublishResult(success=False, error_msg=f"视频文件不存在: {render_result.video_path}",
                                 account=account.name)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            return PublishResult(success=False, error_msg=f"未安装 playwright: {exc}",
                                 account=account.name)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                # 存在本地 Cookie 时自动注入登录态
                context = self.account_mgr.new_context_with_state(browser, account)
                page = context.new_page()

                # 登录态
                if not self.account_mgr.ensure_login(page, account):
                    browser.close()
                    return PublishResult(success=False, error_msg="登录失败或超时", account=account.name)

                # 进入发布页（网络抖动时自动重试）
                self.risk_ctrl.random_delay()
                self._goto(page, self.publish_url)
                page.wait_for_timeout(3000)

                # 上传视频
                page.set_input_files(SELECTORS["file_input"], render_result.video_path)
                self._wait_upload(page)

                # 填写标题（普通 input）
                title_sel = self._find_selector(page, ["title_input"])
                self.risk_ctrl.human_type(page, title_sel, script.title)
                self.risk_ctrl.random_delay(1.0, 2.0)

                # 添加话题（在 contenteditable 简介编辑器内，# 触发候选后回车选中）
                self._fill_topics(page, script.tags)

                # 选择封面（可选，默认使用 AI 推荐首帧；失败不阻断）
                try:
                    cover = page.locator(SELECTORS["cover_select"]).first
                    if cover.count() > 0:
                        self.risk_ctrl.human_move(page, *self._center(cover))
                        cover.click()
                        self.risk_ctrl.random_delay(2, 4)
                except Exception:  # noqa: BLE001
                    logger.info("封面选择跳过（使用默认封面）")

                # 设置可见性（公开 / 好友可见 / 仅自己可见）
                self._set_visibility(page)

                # 发布前再次关闭可能出现的引导气泡
                self._dismiss_guides(page)

                # 精确点击主区域“发布”按钮（排除左上角“作品发布”），未跳走则补点
                self._click_publish(page)

                # 等待结果
                ok, msg = self._wait_publish(page)
                # 保存 Cookie
                self.account_mgr.save_state(context, account)
                browser.close()

                if ok:
                    self.risk_ctrl.on_success()
                    return PublishResult(
                        success=True,
                        video_url=msg,
                        publish_time=time.strftime("%Y-%m-%d %H:%M:%S"),
                        error_msg=None,
                        account=account.name,
                    )
                if self.risk_ctrl.on_failure():
                    return PublishResult(success=False, error_msg=f"已熔断: {msg}", account=account.name)
                return PublishResult(success=False, error_msg=msg, account=account.name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("发布异常")
            return PublishResult(success=False, error_msg=str(exc), account=account.name)

    # ---------------- 工具 ----------------
    def _goto(self, page, url: str, tries: int = 3) -> None:
        """带退避重试的页面导航，容忍 ERR_CONNECTION_CLOSED 等瞬时网络错误。"""
        last_exc = None
        for i in range(1, tries + 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("页面打开失败(%s)，第 %d/%d 次重试...", exc, i, tries)
                page.wait_for_timeout(2500 * i)
        raise RuntimeError(f"页面多次打开失败：{url}（{last_exc}）")

    def _wait_upload(self, page) -> None:
        """等待视频上传与转码完成。

        真实页面没有“上传成功”字样，且 DOM 中会残留隐藏的“上传中”文本，
        因此以“重新上传”（仅在上传完成后出现）为确定性完成标志，
        “设置封面 + 作品标题”同时出现作为辅助判据。
        """
        deadline = time.time() + self.timeout_upload / 1000
        last_log = time.time()
        while time.time() < deadline:
            try:
                body = page.locator("body").inner_text(timeout=1000)
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(1500)
                continue
            # 确定性完成标志
            done = "重新上传" in body
            # 辅助判据：编辑区完整出现
            if not done and ("设置封面" in body and "作品标题" in body):
                done = True
            if done:
                logger.info("视频上传完成")
                page.wait_for_timeout(1500)
                self._dismiss_guides(page)
                return
            if time.time() - last_log > 10:
                logger.info("上传中……")
                last_log = time.time()
            page.wait_for_timeout(1500)
        self._capture(page, "upload_timeout")
        raise TimeoutError("视频上传超时")

    def _dismiss_guides(self, page) -> None:
        """关闭“我知道了”等新手引导气泡，避免遮挡后续点击。"""
        for text in _GUIDE_MARKS:
            try:
                loc = page.get_by_text(text, exact=True)
                if loc.count() > 0 and loc.first.is_visible(timeout=500):
                    loc.first.click(timeout=1000)
                    logger.info("关闭引导气泡：%s", text)
                    page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                continue

    def _fill_topics(self, page, tags: list) -> None:
        """在简介编辑器内逐个输入 #话题，回车采纳首个候选话题。"""
        if not tags:
            return
        try:
            editor = page.locator(SELECTORS["desc_editor"]).first
            editor.wait_for(state="visible", timeout=8000)
            editor.click()
            page.wait_for_timeout(600)
        except Exception as exc:  # noqa: BLE001
            logger.warning("话题编辑器定位失败，跳过话题：%s", exc)
            return

        for tag in tags:
            token = f"#{tag}"
            try:
                # 逐字输入以触发话题候选下拉
                editor.press_sequentially(token, delay=70)
                page.wait_for_timeout(1400)
                dropdown = page.locator(SELECTORS["topic_dropdown"])
                if dropdown.count() > 0:
                    page.keyboard.press("Enter")  # 采纳首个候选
                else:
                    page.keyboard.press("Space")  # 无候选则作为普通文本
                page.wait_for_timeout(400)
                page.keyboard.type("  ")
                self.risk_ctrl.random_delay(0.8, 1.6)
            except Exception as exc:  # noqa: BLE001
                logger.warning("话题 %s 输入异常：%s", tag, exc)

    def _set_visibility(self, page) -> None:
        """在“发布设置-谁可以看”中选择公开/好友可见/仅自己可见（私密）。"""
        label_text = VISIBILITY_LABELS.get(self.visibility)
        if not label_text or label_text == "公开":
            logger.info("谁可以看：公开（默认）")
            return
        try:
            # 选项是 radio label，直接点文本会被 label 拦截，需定位 label 本身
            target = page.locator(
                f"xpath=//label[normalize-space(.)='{label_text}']"
            ).first
            if target.count() == 0:  # 兜底：从文本向上找 label
                target = page.get_by_text(label_text, exact=True) \
                    .locator("xpath=ancestor::label[1]")
            target.wait_for(state="visible", timeout=8000)
            target.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(400)
            target.click()
            page.wait_for_timeout(800)
            checked = target.get_attribute("data-checked")
            logger.info("已设置「谁可以看」为：%s（选中状态=%s）", label_text, checked)
        except Exception as exc:  # noqa: BLE001
            logger.warning("设置可见性为 %s 失败(%s)，保持页面默认（公开）", label_text, exc)

    def _click_publish(self, page, max_try: int = 3) -> None:
        """点击主“发布”按钮；点击后若仍停留在上传页则补点，确保提交。"""
        for attempt in range(1, max_try + 1):
            self.risk_ctrl.random_delay(1.5, 3.0)
            btn = self._find_publish_btn(page)
            try:
                btn.scroll_into_view_if_needed(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            self.risk_ctrl.human_move(page, *self._center(btn))
            page.wait_for_timeout(300)
            btn.click()
            logger.info("已点击发布按钮（第 %d/%d 次）", attempt, max_try)
            # 观察最多 6 秒：离开上传页、或弹出本人安全验证，都视为提交已触发
            t0 = time.time()
            while time.time() - t0 < 6:
                if "upload" not in page.url or "/content/manage" in page.url:
                    return
                if self._has_security_check(page):
                    logger.info("点击已触发安全验证弹窗，等待本人完成验证")
                    return
                page.wait_for_timeout(500)
            logger.warning("点击后仍停留在发布页，准备补点发布按钮")
        logger.warning("多次点击后仍未跳转，交由结果等待逻辑继续确认")

    def _find_publish_btn(self, page):
        """精确定位主区域文本为“发布”的按钮（排除左上角“作品发布”）。"""
        buttons = page.locator("button")
        total = buttons.count()
        for i in range(total):
            btn = buttons.nth(i)
            try:
                text = (btn.inner_text(timeout=500) or "").strip()
                if text in ("发布", "发 布") and btn.is_visible():
                    return btn
            except Exception:  # noqa: BLE001
                continue
        # 兜底：role 精确匹配
        return page.get_by_role("button", name="发布", exact=True).first

    def _has_security_check(self, page) -> bool:
        """检测抖音本人安全验证弹窗（短信验证码 / 原设备扫码）。"""
        try:
            body = page.locator("body").inner_text(timeout=800)
        except Exception:  # noqa: BLE001
            return False
        if "接收短信验证码" in body or "使用原设备扫码" in body:
            return True
        return "请输入验证码" in body and "获取验证码" in body

    def _wait_publish(self, page) -> tuple[bool, str]:
        """等待发布结果。

        硬判据：URL 离开上传页并跳转到内容管理页 /content/manage。
        注意：不能用正文“作品管理”字样判断——左侧导航栏常驻该词，会瞬间误判。
        若出现短信/扫码安全验证弹窗，则暂停等待用户本人完成（有头浏览器中操作）。
        """
        deadline = time.time() + self.timeout_publish / 1000
        verify_deadline = None
        verify_warned = False
        while True:
            now = time.time()
            url = page.url
            if "/content/manage" in url:
                logger.info("检测到跳转作品管理页，发布成功：%s", url)
                return True, url

            # 本人安全验证：延长等待，交由用户在浏览器手动完成
            if self._has_security_check(page):
                if not verify_warned:
                    logger.warning("=" * 56)
                    logger.warning("检测到抖音本人安全验证（短信验证码 / 原设备扫码）！")
                    logger.warning("请在弹出的浏览器窗口中手动完成验证，程序将自动等待并继续…")
                    logger.warning("=" * 56)
                    verify_warned = True
                if verify_deadline is None:
                    verify_deadline = now + self.timeout_verify / 1000
                if now > verify_deadline:
                    self._capture(page, "verify_timeout")
                    return False, "安全验证等待超时（未在限定时间内完成短信/扫码验证）"
                page.wait_for_timeout(2000)
                continue

            if now > deadline:
                break
            page.wait_for_timeout(2000)

        self._capture(page, "publish_timeout")
        return False, "发布结果等待超时（可能已发布，请在作品管理后台人工确认）"

    def _find_selector(self, page, keys: list[str]) -> str:
        """按优先级取第一个可见的选择器"""
        for key in keys:
            sel = SELECTORS[key]
            try:
                if page.locator(sel).first.is_visible(timeout=1500):
                    return sel
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f"未找到页面元素: {keys}")

    @staticmethod
    def _center(locator) -> tuple[float, float]:
        box = locator.bounding_box()
        if not box:
            return 500, 400
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def _capture(self, page, tag: str) -> None:
        name = self.capture_dir / f"{tag}_{int(time.time())}.png"
        try:
            page.screenshot(path=str(name))
            logger.info("现场截图已保存：%s", name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("截图失败：%s", exc)
