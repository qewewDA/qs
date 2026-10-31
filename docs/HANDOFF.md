# Stellaris AI Autopilot 交接文档

交接日期：2026-10-31。仓库：`qewewDA/qs`，工作目录 `/workspace/qs`，包名 `stellaris-ai-autopilot`，当前未发布版本 `0.1.0`。

## 1. 用户目标与当前进度

目标是让 LLM 在单人《群星》中真实经营玩家自己的帝国：读取状态、制定经济/科研/军事/外交策略、执行获准操作、读取新状态验证，并持续运行。目标游戏版本已从 4.5.2 **更正为当前最新稳定版**；中文界面、2560×1440，有时启用 UI Mod。UI Mod 名称、UI 缩放和真实存档尚未提供。

当前交付是**可安装运行的基础首版**，不是已经实机验收的全面自治系统：

| 能力 | 已有实现 | 验证边界 |
| --- | --- | --- |
| 状态读取 | ZIP `gamestate` / 明文 PDX、玩家/战役识别、资源、预算、科技、舰队、行星、战争 | 合成 fixture 与结构回归通过；没有用户最新版本的真实存档 |
| 策略 | 离线优先级规则、OpenAI-compatible function calling、四领域策略 | 真 HTTP 本地服务验证接口；未调用真实 LLM 提供商 |
| 游戏执行 | Windows UI 宏、固定 `saa.20` 防御集结事件 | 模拟输入测试；没有 Windows 桌面或游戏客户端 |
| 验证与恢复 | 新存档后置条件、SQLite 先登记后派单、未确认阻断、人工核对恢复 | 超时、中断、并发与故障注入测试 |
| MCP | `observe_empire` / `list_actions` / `plan_action` / `status` | 真 stdio 会话；只读和预览，不暴露 live 执行 |
| 分发 | wheel、源码包、附带 Mod、Launcher 安装命令 | 独立虚拟环境从 `/tmp` 运行；Windows CI 尚未远端执行 |

经济/科研通过**本机预先校准的 UI 宏**实现，不能自动识别所有面板并生成通用点击计划；没有通用造舰/建设队列接口、完整外交执行、智能战线、OCR 或视觉定位。原生 AI modifier 不能冒充玩家订单，因此没有采用这种捷径。

## 2. 接手后先读和先运行

先读根目录 `AGENTS.md`、`README.md`，再读本文、`docs/validation.md`、`docs/compatibility.md`、`docs/research.md`。使用现有 checkout；云任务已经隔离，不自行创建 worktree。

```bash
cd /workspace/qs
uv sync --frozen --extra dev --extra mcp
uv run --frozen stellaris-autopilot demo
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv build
```

`demo` 是临时目录内的 replay adapter：实际运行解析、授权、模拟派单、新存档读取、后置条件和 SQLite，但**不连接游戏**。不能把它的 `verified` 写成实机操作成功。测试中的 HTTP 服务仅在 loopback，MCP 会启动子进程；执行器若限制 socket/子进程，应通过正常工具权限运行，不能删除或跳过这些测试伪造通过。

依赖由 `uv.lock` 锁定；Python ≥3.11。云中验证的解释器为 3.12.14。`.github/workflows/ci.yml` 配置 Ubuntu/Windows × Python 3.11/3.12/3.13；配置存在不等于这些平台已跑通。

## 3. 模块与数据流

```text
SaveObserver ──稳定存档+SHA-256──▶ Planner ──一个动作ID──▶ Engine
     ▲                                               │
     │                                               ▼
  新存档后置条件 ◀── 游戏/新保存 ◀── WindowsDriver ◀── Journal.claim
                                                     │
                                      verified / unverified / reconcile
```

| 文件 | 职责与修改注意事项 |
| --- | --- |
| `pdx.py` | 保留重复键和匿名值的嵌套文本解析器；不以正则替代嵌套结构 |
| `saves.py` | 提取玩家视角；未知数据标记缺失，不默认为 0 或和平；`fleets_by_id` / `planets_by_id` 提供稳定 ID 路径 |
| `observe.py` | 稳定快照、文件签名缓存、有界读取及日志轮转；缓存不能掩盖文件替换或被调用者修改 |
| `planner.py` | 模型只返回已配置动作 ID；远端仅 HTTPS，不跟随重定向；错误不回显提供商响应正文 |
| `config.py` / `predicates.py` | 用户授权与有限谓词语言；`wait` 为保留 ID；不执行配置中的 Python/表达式 |
| `engine.py` | 校验国家、战役、版本、时效、前置条件；派单后必须等待新存档满足后置条件 |
| `journal.py` | SQLite WAL、原子认领、防重复派单、状态迁移和有界规划历史 |
| `windows.py` | 前台 exe/标题/PID/客户区检查，固定事件白名单，输入中停止检查 |
| `mcp_server.py` | 全部只读工具；不能通过请求参数升级为 live，不能修改原 journal 的附属文件 |
| `cli.py` | 安装/检查/规划/执行/停止/核对入口；规划历史有界，失败退出码可用于调度 |
| `mod/` | Launcher 描述、on_actions、scripted effects、国家事件、中英 BOM 本地化 |

数据格式以现有 `inspect examples/empire.txt` 输出为准。库存通常来自 `country.modules.standard_economy_module.resources`；预算优先已结束的 `last_month`，回退时明确记录 `budget_period`。不要把 `player.id` 当国家 ID，国家识别必须来自 `player.country`。未知战争结构要保留 unknown，不能据此判定和平。

## 4. 实机启动与配置

Windows PowerShell，仓库根目录：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[windows,mcp,dev]"
$saa = ".\.venv\Scripts\stellaris-autopilot.exe"
& $saa inspect "实际存档.sav"
Copy-Item examples\config.json config.local.json
```

编辑本地配置，至少核对：

- `save_path`：同一局、实际会被新保存更新的文件；本版不自动选择其他目录中的最新一局。
- `country_id`、`campaign_id`：来自 `inspect`；`campaign_id` 必须可识别且匹配。缺失战役身份时只能观察，不可 live。
- `verified_game_version`：本机验证完成后填写的完整存档版本字符串；**不是自动兼容认证**。升级后重做验证。
- `resolution`：实际客户区像素，不是桌面分辨率的猜测。用 `calibrate --delay 5` 获取。
- `state_dir`：复制示例到根目录后改为 `./.autopilot`。所有控制器应使用同一 journal；不要用多个不同 state_dir 并行操纵游戏。
- `actions` / `allowed_actions`：示例坐标、科技和舰队/星系 ID 是占位值，默认没有授权动作。宏只能使用本地配置的有限步骤。
- `save_steps` / `log_path`：按本机验证快速保存宏及 `game.log`；没有新存档就没有结果验证。

原版和 UI Mod 分别维护本地配置并校准。改变 UI Mod、缩放、分辨率、布局或菜单初始状态后不能沿用旧坐标。前台进程检查**不能证明屏幕上加载的战役与存档相同**，也不能消除焦点切换与发送输入之间的瞬间竞态；运行期间不要切换游戏实例、战役或菜单布局。

CLI 在启动时读取配置，运行中编辑权限不会自动更新当前进程；先停止，再修改并重启。MCP 则在每次工具调用时重新读取配置。

```powershell
& $saa doctor --config config.local.json
& $saa run --config config.local.json --planner rules --goal "科研优先"
& $saa install-mod --mod-dir "$env:USERPROFILE\Documents\Paradox Interactive\Stellaris\mod"
```

Mod 路径可能在 OneDrive。启用播放集后先手动测试 `event saa.1 <国家ID>`，查 `game.log` 和 `error.log`。`saa.20` 会**清除所有未交战自有移动军事舰队的原命令**并排入移向首都的命令；不保证安全路径或全部舰队抵达。`saa.2` 停用桥接，但不撤销已有订单。

LLM 配置变量为 `AUTOPILOT_LLM_BASE_URL`、`AUTOPILOT_LLM_MODEL`、按需设置的 `AUTOPILOT_LLM_API_KEY`。本地 Ollama 默认 `http://localhost:11434/v1`；需要支持 tools 的模型。不要把 key 写入交接文件、测试、日志或提交。

```powershell
& $saa run --config config.local.json --planner llm --live --continuous --years 10 --goal "未来十年发展科技、维持合金盈余、避免战争，遇袭防守"
```

默认有 5 秒切回游戏；十年按本次启动时的完整游戏日期计算。重启程序会重新计算期限，**尚未实现跨进程持久化任务期限**。`rules` 不理解自然语言，只按固定优先级选动作。

## 5. 停止、失败与恢复

| 状态 | 含义 | 接手操作 |
| --- | --- | --- |
| `dry_run` | 没有发送输入 | 核对提案及后置条件 |
| `waiting` | 无适用动作或目标条件已成立 | 等待新存档 |
| `blocked` | 权限/数据/条件不满足或有未决操作 | 排查原因，不放宽守卫凑通过 |
| `pending` / `dispatched` | 已登记或已发送，尚未确认 | 可能已经部分执行，不能盲目重发 |
| `verified` | 新存档满足配置的后置条件 | 只证明该谓词成立，因果强度取决于条件设计 |
| `unverified` | 超时或发生无法确认的故障 | 在游戏里核对队列/订单再 reconcile |

`run` 遇 `blocked` / `unverified` 返回 2；正常停止/等待/演示返回 0；Ctrl+C 返回 130。`doctor` policy 检查失败也返回 2。STOP 文件和鼠标角落 fail-safe 用于停止；已发送输入不能撤销，部分控制台输入可能需要人工清理。

```powershell
& $saa stop --config config.local.json
& $saa journal --config config.local.json
& $saa reconcile --config config.local.json --id 1 --note "在游戏中已核实实际结果，说明是否已执行"
```

核对后才能删除 `state_dir/STOP` 并恢复。保留 `journal.sqlite` 及其 WAL 数据；不要在运行中只复制主数据库或删除 WAL。人工 reconcile 不是成功证明，也不自动撤销/重试动作。不要在仍在运行的派单过程中手动 reconcile；状态迁移冲突会阻断，而不是覆盖终态。

## 6. 本轮检查优化

- 修复异常配置字段导致的 traceback；支持 Windows UTF-8 BOM；拒绝保留动作 ID `wait`、非法类型、溢出数值和不合法谓词。
- 改善初始化顺序：错误 live 配置不创建 journal；规划只读取最近 100 条摘要，不为每次 tick 加载全部 `before_json`。
- 稳定存档签名包含设备、inode、大小、mtime、ctime；Windows 另读原生 ChangeTime（Windows 的 ctime 是创建时间），该 Win32 调用仍需 Windows 验证。未变存档复用隔离缓存，哈希读取也有大小上限，错误的非普通文件路径在打开前拒绝。
- SQLite 失败认领会回滚；终态不能被迟到的执行更新降级；派单前重新检查存档时效。
- 修正 MCP 读取 WAL journal 仍会创建原目录 sidecar 的问题；通过稳定私有副本查询并包含 WAL，不能用会忽略 WAL 的 `immutable=1` 替代。主库+WAL 总上限 64MiB，最多三次稳定读取；持续并发修改时拒绝预览，不锁定写入进程，也不保证返回后状态仍最新。
- Windows 输入中响应 STOP/fail-safe，等待期间约每 50ms 检查，绑定首次确认的游戏 PID，游戏重启后须重启自动驾驶；错误时不盲目切换控制台状态。无法撤销已经发出的单次系统输入。
- Mod 安装以独占方式创建外部 Launcher 文件，避免覆盖安装期间并发出现的用户文件。

最终测试数、构建和独立 wheel 验证结果见 `docs/validation.md`；不要引用第一轮 209 项作为今后每次变更后的默认结论。

## 7. 网络、许可证与下一步

四个指定 GitHub 项目的 README、目录及相关源码已经审阅，精确提交和许可证结论在 `docs/research.md`。三个项目有 MIT 正文；`stellaris-mcp-interface` 只有 package 元数据 MIT 声明，没有独立 LICENSE 正文。本项目独立实现，没有复制它们的源码。

指定 Wiki 和 Steam 官方新闻接口的正常代理请求均曾返回 403。草稿已保存以下自定义域名需求：`stellaris.paradoxwikis.com`、`main--pdxdoc-next.netlify.app`、`qunxing.huijiwiki.com`、`api.steampowered.com`。安装脚本与启动说明也在环境草稿中，**保存草稿不等于网络已应用或环境已发布**；运行时状态应重新检查，不能绕过代理/TLS。

建议按顺序推进：

1. **P0 实机验收**：确认官方最新稳定版本，获得用户的非铁人单人真实存档（注意隐私），补充可合法使用的脱敏回归 fixture；在 Windows 上检查最新版本 PDXScript `error.log` 和实际舰队订单。
2. **P0 UI 适配**：确定具体 UI Mod/缩放，分别校准科研、付费建设与保存宏，验证一次单动作后才持续运行。不能把坐标占位模板当成开箱即用功能。
3. **P1 可观察动作目录**：增加版本适配器、明确成本/所有权/冷却守卫及队列字段；以真实新存档结果验收，不使用免费建筑、资源或 AI 属性作弊。
4. **P1 长期运行**：持久化目标与终止日期、任务预算、决策日志轮转/数据库保留策略、UI 状态识别。当前新观察会重规划，但未知执行结果仍须人工核对。
5. **P2 扩展**：智能防守、更多外交动作、必要时再引入 TypeScript/视觉模块；先明确许可证和真实游戏接口，避免空工具或伪成功。

## 8. 提交与分发注意

初始交接时尚未 commit、push 或发布；用户随后明确授权将源码同步到 GitHub `qewewDA/qs` 的 `main`。后续接手时以 `git status --short`、`git log` 和远程分支为准，不要用本文的初始阶段记录判断当前提交状态，也不要清除未提交的本地工作。`.venv`、`.autopilot`、`.sav` 存档、数据库、本地配置及 `dist` 均被忽略，不应提交凭据或个人游戏数据。

`uv build` 生成 `dist/stellaris_ai_autopilot-0.1.0-py3-none-any.whl` 和源码 tarball，包含 Mod 及本地化；源包通过 `MANIFEST.in` 包含文档、示例和 lockfile。每次代码更新后重新构建并用独立环境安装验证。本次 GitHub 源码同步不包含 PyPI、Steam Workshop 或 GitHub Release 发布，也不会把忽略的安装包强行加入源码提交。
