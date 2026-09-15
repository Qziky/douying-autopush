"""手动执行入口。

用法::

    python main.py hot                      # 抓取热点榜
    python main.py select                   # 抓取 + 选题
    python main.py script --hot-title "标题" # 生成脚本（可选指定热点）
    python main.py render --script scripts/xxx.json   # 渲染视频
    python main.py publish --video output/xxx.mp4 --account main --real   # 发布（真实）
    python main.py all --dry-run            # 全流程（演练，默认）
    python main.py all --real               # 全流程（真实发布）
    python main.py status                   # 查看最近任务记录
    python main.py scheduler                # 启动定时调度
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

# 保证从任意工作目录运行都能导入 src
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.logger import get_logger  # noqa: E402
from src.common.models import Scene, SelectedTopic, VideoScript  # noqa: E402
from src.common.storage import WorksDB  # noqa: E402
from src.content_gen.script_writer import ScriptWriter  # noqa: E402
from src.douyin_adapter.publisher import VideoPublisher  # noqa: E402
from src.hot_analysis.crawler import HotCrawler  # noqa: E402
from src.hot_analysis.selector import HotSelector  # noqa: E402
from src.scheduler.task_scheduler import TaskScheduler  # noqa: E402
from src.video_render.composer import VideoComposer  # noqa: E402
from src.workflow import Workflow  # noqa: E402

logger = get_logger("main")


# ---------------- 子命令实现 ----------------
def cmd_hot(args) -> int:
    items = Workflow().fetch_hot()
    print(f"\n共 {len(items)} 条热点：")
    for h in items:
        print(f"  #{h.rank:02d} [{h.category}] {h.title}  (热度 {h.hot_value}, 来源 {h.source})")
    return 0


def cmd_select(args) -> int:
    wf = Workflow()
    hot_list = wf.fetch_hot()
    topic = wf.select_topic(hot_list)
    print(f"\n最终选题：{topic.hot_item.title}")
    print(f"  分类    : {topic.hot_item.category}")
    print(f"  热度    : {topic.hot_item.hot_value}")
    print(f"  评分    : {topic.score}")
    print(f"  角度    : {topic.create_angle}")
    return 0


def cmd_script(args) -> int:
    wf = Workflow()
    if args.hot_title:
        hot_list = wf.fetch_hot()
        topic = wf._find_by_title(hot_list, args.hot_title)
    else:
        hot_list = wf.fetch_hot()
        topic = wf.select_topic(hot_list)
    script = ScriptWriter().generate_script(topic)
    print(f"\n标题：{script.title}")
    print(f"话题：{' '.join('#' + t for t in script.tags)}")
    print(f"总时长：{script.total_duration}s | 分镜：{len(script.scenes)} 幕")
    for sc in script.scenes:
        print(f"  [{sc.index:02d}] ({sc.duration}s) {sc.text}  -> 画面: {sc.image_keyword}")
    path = wf.save_script(script)
    print(f"\n脚本已保存：{path}")
    return 0


def cmd_render(args) -> int:
    wf = Workflow()
    if args.script:
        script = _load_script(args.script)
    else:
        hot_list = wf.fetch_hot()
        topic = wf.select_topic(hot_list)
        script = ScriptWriter().generate_script(topic)
    result = VideoComposer().compose(script, output_name=script.title)
    print(f"\n渲染完成：{result.video_path}")
    print(f"封面：{result.cover_path} | 时长 {result.duration:.1f}s | 分辨率 {result.resolution}")
    return 0


def _apply_visibility(args) -> None:
    """把 --private/--visibility 参数写入运行时配置（publisher 读取）。"""
    vis = getattr(args, "visibility", None)
    if getattr(args, "private", False):
        vis = "private"  # --private 是 --visibility private 的快捷方式
    if vis:
        from src.common.config import ConfigManager
        ConfigManager().set("douyin.visibility", vis)


def cmd_publish(args) -> int:
    if not args.video:
        print("发布需要指定视频文件：--video output/xxx.mp4（可先运行 render 生成）")
        return 2
    script = _load_script(args.script) if args.script else _dummy_script()
    from src.common.models import RenderResult
    result = RenderResult(video_path=args.video, cover_path="", duration=0.0, resolution=(0, 0))
    _apply_visibility(args)
    if args.real:
        from src.common.config import ConfigManager
        ConfigManager().set("douyin.dry_run", False)
    pub = VideoPublisher().publish(result, script, account_name=args.account)
    print(f"\n发布结果：success={pub.success}")
    if pub.error_msg:
        print(f"  原因：{pub.error_msg}")
    if pub.video_url:
        print(f"  链接：{pub.video_url}")
    # 落库（真实发布计入频率；演练标记为 dry_run）
    import datetime as _dt
    from src.common.models import TaskRecord
    record = TaskRecord(
        task_id=uuid.uuid4().hex[:12],
        started_at=_dt.datetime.now().isoformat(timespec="seconds"),
        status="success" if (pub.success and args.real) else ("dry_run" if pub.success else "failed"),
        stage="publish",
        script_title=script.title,
        video_path=args.video,
        publish_url=pub.video_url or "",
        error_msg=pub.error_msg or "",
    )
    WorksDB().upsert_task(record)
    return 0 if pub.success else 1


def cmd_all(args) -> int:
    _apply_visibility(args)
    wf = Workflow()
    dry_run = not args.real
    record = wf.run_full(account_name=args.account, dry_run=dry_run, hot_title=args.hot_title)
    print(f"\n任务 {record.task_id}：{record.status}")
    if record.error_msg:
        print(f"  错误：{record.error_msg}")
    print(f"  热点：{record.hot_title}")
    print(f"  标题：{record.script_title}")
    if record.video_path:
        print(f"  视频：{record.video_path}")
    return 0 if record.status in ("success", "dry_run") else 1


def cmd_status(args) -> int:
    records = WorksDB().recent_tasks(limit=args.limit)
    if not records:
        print("暂无任务记录")
        return 0
    print(f"{'ID':<14}{'状态':<10}{'开始时间':<22}{'热点/标题'}")
    for r in records:
        name = (r["script_title"] or r["hot_title"] or "")[:36]
        print(f"{r['id']:<14}{r['status']:<10}{r['started_at']:<22}{name}")
    return 0


def cmd_scheduler(args) -> int:
    TaskScheduler().start()
    return 0


def cmd_login(args) -> int:
    """独立登录：弹出浏览器扫码，Cookie 持久化到 data/cookies/"""
    from src.douyin_adapter.account import AccountManager
    mgr = AccountManager()
    account = mgr.get_account(args.account)
    if account is None:
        print("未找到可用账号：请先在 config/accounts.yaml 中配置账号")
        return 2
    print(f"正在为账号「{account.name}」打开登录窗口，请用抖音 App 扫码登录（3 分钟内有效）...")
    ok = mgr.interactive_login(account_name=args.account, headless=False)
    if ok:
        print(f"\n登录成功，Cookie 已保存到 {mgr.cookie_path(account)}")
        print("之后运行发布/全流程将自动复用登录态，无需重复扫码")
    else:
        print("\n登录失败或超时，请重试")
    return 0 if ok else 1


# ---------------- 工具 ----------------
def _load_script(path: str) -> VideoScript:
    p = Path(path)
    if not p.exists():
        p = Path(ROOT) / "output" / "scripts" / path
    if not p.exists():
        raise FileNotFoundError(f"脚本文件不存在: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    topic = SelectedTopic(
        hot_item=_dummy_hot(data.get("hot_title", "")),
        score=1.0, create_angle="",
    )
    return VideoScript(
        topic=topic,
        title=data["title"],
        full_script=data["full_script"],
        tags=data.get("tags", []),
        scenes=[Scene(**s) for s in data.get("scenes", [])],
        total_duration=data.get("total_duration", 60),
        cover_text=data.get("cover_text", ""),
    )


def _dummy_script() -> VideoScript:
    return VideoScript(
        topic=_dummy_topic(),
        title="手动发布测试",
        full_script="手动发布测试",
        tags=["测试"],
        scenes=[Scene(index=1, text="手动发布测试", image_keyword="测试", duration=3)],
        total_duration=3,
        cover_text="测试",
    )


def _dummy_topic() -> SelectedTopic:
    return SelectedTopic(hot_item=_dummy_hot("手动发布"), score=1.0, create_angle="")


def _dummy_hot(title: str):
    from src.common.models import HotItem
    return HotItem(rank=0, title=title, hot_value=0, category="社会", source="manual")


# ---------------- 入口 ----------------
def main() -> int:
    parser = argparse.ArgumentParser(prog="douyin-auto", description="抖音自动化内容生产系统")
    sub = parser.add_subparsers(dest="command", required=True)

    p_hot = sub.add_parser("hot", help="抓取热点榜")
    p_hot.set_defaults(func=cmd_hot)

    p_sel = sub.add_parser("select", help="抓取 + 选题")
    p_sel.set_defaults(func=cmd_select)

    p_script = sub.add_parser("script", help="生成口播脚本")
    p_script.add_argument("--hot-title", help="指定热点标题（不填则自动选题）")
    p_script.set_defaults(func=cmd_script)

    p_render = sub.add_parser("render", help="渲染视频")
    p_render.add_argument("--script", help="脚本 JSON 路径（output/scripts 下）")
    p_render.set_defaults(func=cmd_render)

    p_pub = sub.add_parser("publish", help="发布视频")
    p_pub.add_argument("--video", required=True, help="视频文件路径")
    p_pub.add_argument("--script", help="脚本 JSON 路径（提供标题/话题）")
    p_pub.add_argument("--account", help="账号名（config/accounts.yaml）")
    p_pub.add_argument("--real", action="store_true", help="真实发布（默认演练）")
    p_pub.add_argument("--private", action="store_true", help="私密发布（仅自己可见）")
    p_pub.add_argument("--visibility", choices=["public", "friends", "private"],
                   help="谁可以看：public 公开 / friends 好友可见 / private 仅自己可见")
    p_pub.set_defaults(func=cmd_publish)

    p_all = sub.add_parser("all", help="全流程")
    p_all.add_argument("--account", help="账号名")
    p_all.add_argument("--hot-title", help="指定热点标题（不填则自动选题）")
    p_all.add_argument("--real", action="store_true", help="真实发布（默认演练 dry-run）")
    p_all.add_argument("--private", action="store_true", help="私密发布（仅自己可见）")
    p_all.add_argument("--visibility", choices=["public", "friends", "private"],
                   help="谁可以看：public 公开 / friends 好友可见 / private 仅自己可见")
    p_all.set_defaults(func=cmd_all)

    p_status = sub.add_parser("status", help="任务记录")
    p_status.add_argument("--limit", type=int, default=10, help="显示条数")
    p_status.set_defaults(func=cmd_status)

    p_login = sub.add_parser("login", help="扫码登录抖音并持久化 Cookie")
    p_login.add_argument("--account", help="账号名（默认 accounts.yaml 第一个）")
    p_login.set_defaults(func=cmd_login)

    p_sch = sub.add_parser("scheduler", help="启动定时调度")
    p_sch.set_defaults(func=cmd_scheduler)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.error("命令执行失败：%s", exc)
        print(f"\n执行失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
