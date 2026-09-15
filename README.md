# 抖音自动化内容生产系统（个人版）

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![uv](https://img.shields.io/badge/package%20manager-uv-111) ![License](https://img.shields.io/badge/License-MIT-green)

「热点自动发现 → 文案自动生成 → 视频自动渲染 → 作品自动发布」的全自动内容生产流水线，面向个人抖音创作者与自媒体运营者，支持资讯类、科普类、盘点类标准化口播视频的批量生产。

> 合规边界：仅用于个人学习与非盈利运营，严格遵守抖音社区规范。自动化操作存在账号限流 / 封禁风险，请先用小号测试。



***

## 一、系统架构



```
调度触发层  ──▶  热点分析  ──▶  文案生成  ──▶  合规校验  ──▶  视频渲染  ──▶  抖音发布

&#x20;(APScheduler)   (采集+选题)   (LLM脚本)     (两级校验)    (TTS+合成)    (Playwright)
```

六大模块 + 通用支撑（配置 / 日志 / 重试 / 合规 / 存储），模块间通过 `dataclass` 数据契约单向流转，单点失败不影响全局。



| 模块   | 技术实现                                                 |
| ---- | ---------------------------------------------------- |
| 热点分析 | Requests + Playwright（三级降级：api → playwright → mock）  |
| 文案生成 | OpenAI 兼容 LLM + Prompt 模板，未配置 Key 时自动切换内置演示生成器       |
| 视频渲染 | edge-tts 语音 + Pexels 素材 + Pillow 字幕 + MoviePy 合成     |
| 抖音适配 | Playwright 自动上传发布，Cookie 持久化，风控引擎（延迟 / 轨迹 / 限流 / 熔断） |
| 调度触发 | APScheduler：Cron 定时 + 热点榜 Top3 变化触发                  |
| 数据存储 | SQLite 任务落库 + YAML 配置 + 分级日志                         |

## 二、环境要求与安装



* Python 3.10+（uv 可自动下载管理 Python）

* 无需单独安装 FFmpeg（MoviePy 自动携带 imageio-ffmpeg 二进制）

* 推荐使用 [uv](https://docs.astral.sh/uv/) 管理环境（已配置 `pyproject.toml` + `uv.lock`）

### 方式一：uv（推荐）

```
# 0. 安装 uv（已安装可跳过；Windows PowerShell）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 1. 创建虚拟环境并按 uv.lock 精确安装全部依赖
uv sync

# 2. 安装 Playwright 浏览器内核（真实发布需要，只需一次）
uv run playwright install chromium

# 之后所有命令都通过 uv run 在项目虚拟环境中执行，无需手动激活
uv run python main.py hot
```

> 依赖声明在 `pyproject.toml`，精确版本锁定在 `uv.lock`；新增依赖用 `uv add <包名>`，移除用 `uv remove <包名>`。

### 方式二：pip（备选）

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 三、配置说明（config/settings.yaml）



| 配置项                       | 说明                                                                                 | 默认                |
| ------------------------- | ---------------------------------------------------------------------------------- | ----------------- |
| `content_gen.llm.api_key` | 大模型 API Key（OpenAI 兼容，支持 deepseek/moonshot/custom）                                 | 空 = 演示生成器         |
| `material.pexels.api_key` | Pexels 素材 API Key（[https://www.pexels.com/api/](https://www.pexels.com/api/) 免费申请） | 空 = 渐变占位图         |
| `douyin.dry_run`          | 演练模式：不真实发布，只打印动作                                                                   | `true`（安全默认）      |
| `hot_analysis.selector.*` | 选题规则：领域白名单 / 热度阈值 / 排除词 / 评分权重                                                     | 见文件               |
| `scheduler.cron`          | 定时全流程 Cron 表达式                                                                     | `0 8,12,18 * * *` |
| `risk_control.*`          | 风控参数：随机延迟 / 单日发布上限 / 最小间隔 / 熔断阈值                                                   | 见文件               |

账号配置见 `config/accounts.yaml`（Cookie 文件在登录后自动生成到 `data/cookies/`）。

### 私有配置与密钥安全（重要）

主配置 `config/settings.yaml` 可安全提交，**所有密钥都不要写在里面**，而是写进本地私有配置：

```powershell
# 复制示例为本地私有配置（该文件已被 .gitignore 忽略，永不提交）
copy config\settings.local.example.yaml config\settings.local.yaml
copy config\accounts.example.yaml       config\accounts.yaml
```

`settings.local.yaml` 会与主配置**深度合并**并覆盖同名项，LLM 的 `api_key`、Pexels Key 等只写在这里即可。仓库已默认忽略 `settings.local.yaml`、`accounts.yaml`、`data/`（含登录 Cookie）、`.env`，提交前也可用 `git status` 再次确认没有敏感文件被纳入。

## 四、使用



```
# 【首次必做】扫码登录抖音，Cookie 持久化到 data/cookies/（只需一次，失效后重跑）

uv run python main.py login

# 抓取热点榜

python main.py hot

# 抓取 + 选题（uv 环境下统一用 uv run 前缀；pip 环境去掉前缀即可）

uv run python main.py select

# 生成脚本（可指定热点；未配置 API Key 时用演示生成器）

uv run python main.py script --hot-title "国产大飞机C919新增国际航线"

# 渲染视频（脚本自动保存到 output/scripts/，可复用）

uv run python main.py render --script 20260914_193626.json

# 发布（默认演练；--real 为真实发布，需先完成账号登录）

uv run python main.py publish --video output/xxx.mp4 --account main --real

# 全流程：热点→文案→合规→渲染→发布（默认演练）

uv run python main.py all

uv run python main.py all --real          # 真实发布

# 私密发布（仅自己可见）：加 --private；也可用 --visibility 选 public/friends/private

uv run python main.py all --real --private

uv run python main.py publish --video output/xxx.mp4 --real --visibility friends

uv run python main.py all --account main  # 指定账号

# 任务记录

uv run python main.py status

# 定时调度（Cron + 热点监控触发）

uv run python run_scheduler.py
```

**登录说明**：推荐先单独运行 `uv run python main.py login`，会弹出浏览器，用抖音 App 扫码后 Cookie 自动保存到 `data/cookies/`，之后发布/全流程自动复用，无需重复扫码；Cookie 失效（约数天到数周）后重跑该命令即可。也可直接运行 `publish --real` / `all --real`，未登录时会自动弹出扫码窗口。

**发布安全验证（重要）**：抖音对新环境/自动化发布可能在点击「发布」后弹出「接收短信验证码」或要求「使用原设备扫码」的本人校验，这一步无法自动绕过。程序检测到该弹窗会暂停并等待（默认 240 秒，可由 `douyin.timeout.security_verify` 配置），请在弹出的浏览器窗口中手动点「获取验证码」、填入手机短信验证码（或用抖音 App 扫码）后点「验证」，程序会自动继续并以跳转作品管理页作为发布成功的硬判据。真实发布必须使用有头浏览器（`douyin.headless=false`）。

## 五、模块结构与扩展点



```
├── config/                  # settings.yaml 主配置；*.example.yaml 为模板；local/accounts 私有且被 git 忽略

├── src/

│   ├── common/              # 通用支撑：config / logger / retry / compliance / models / storage

│   ├── hot_analysis/        # crawler.py（三级降级采集） / selector.py（规则+评分选题）

│   ├── content_gen/         # llm_client.py / script_writer.py / prompts/script_gen.tpl

│   ├── video_render/        # tts.py / material.py / subtitle.py / composer.py

│   ├── douyin_adapter/      # account.py / risk_control.py / publisher.py

│   ├── scheduler/           # task_scheduler.py

│   └── workflow.py          # 全流程编排

├── cache/                   # 素材缓存（git 忽略）

├── data/                    # 日志 + works.db（git 忽略）

├── output/                  # 成品视频/封面/脚本

├── main.py                  # 手动执行入口

└── run_scheduler.py         # 调度服务入口
```

扩展点：



* **LLM**：在 `content_gen.llm` 切换 provider 或填自定义 `base_url`，接口为 OpenAI 兼容格式

* **素材源**：`material.py` 的 `_search_image` 可替换为任意免费可商用素材库

* **TTS**：`tts.py` 预留多引擎结构，`engine` 配置项可扩展

* **页面选择器**：`publisher.py` 顶部 `SELECTORS` 集中管理，抖音改版只改这一处

## 六、注意事项



1. **账号安全**：自动化发布存在限流 / 封禁风险，建议小号测试、控制发布频率、固定 IP 与设备

2. **内容合规**：已内置两级校验（敏感词库 + 可选 LLM 复检），发布前仍建议人工审核

3. **素材版权**：Pexels 为免费可商用，占位图仅用于联调，正式发布请配置真实素材源

4. **版本兼容**：抖音页面改版可能导致发布选择器失效，运行 `--real` 失败时查看 `data/logs/fails/` 下的现场截图并更新 `SELECTORS`

## 七、免责声明与开源协议

- 本项目仅供**个人学习与技术研究**使用，请勿用于任何违反平台规则或法律法规的用途；因使用本项目产生的账号限流、封禁、内容合规等后果由使用者自行承担。
- 本项目不内置、不分发任何受版权保护的素材或账号凭证；请确保你发布的内容与素材拥有合法授权。
- 开源协议：[MIT License](LICENSE)。