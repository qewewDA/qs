# Stellaris AI Autopilot · 群星 AI 自动驾驶

让 LLM 从玩家帝国的存档中制定计划，通过 **Windows 游戏输入或固定 Mod 事件**执行获准操作，再读取新存档验证结果。Python 3.11+，Windows 10/11 为实机目标；MIT 开源。核心无第三方运行依赖，LLM 支持 OpenAI-compatible / Ollama，另有可选 MCP 接口。

接手开发请先读 [交接文档](docs/HANDOFF.md)；完整测试与安装验证范围见 [验证记录](docs/validation.md)。

**这是可安装、可运行、带测试的首版，不是已完成全部游戏操作的通用帝国 AI。** 云环境没有《群星》，Windows 输入与 PDXScript 尚未实机验证。经济、科研操作需要按本机版本/分辨率配置 UI 宏；舰队集结有游戏脚本，但仍需本机验证。未实现自动探索 UI、全自动外交、智能战线和任意建筑/舰船的通用队列接口。不会用免费资源、刷建筑或改 AI 权重冒充玩家操作。

## 安装与离线运行

Windows PowerShell，在仓库根目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[windows,mcp,dev]"
.\.venv\Scripts\stellaris-autopilot.exe demo
.\.venv\Scripts\stellaris-autopilot.exe inspect examples\empire.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Linux/macOS 可运行观察、规划、MCP、模拟及测试；真实输入只支持 Windows：

```bash
uv sync --frozen --extra dev --extra mcp
uv run stellaris-autopilot demo
uv run pytest -q
```

`demo` 使用独立临时目录和 **replay adapter**，实际运行解析 → 授权 → 派单 → 新观察 → 后置条件 → SQLite 记录，输出 `SIMULATION` 和 `verified`。它不连接游戏，也不调用付费模型。

## 连接自己的单人帝国

本轮目标：**当前最新稳定版、中文、2560×1440**。不固定 4.5.2，也不把参考仓库的版本号当作当前发行版。运行时从实际存档读取完整版本，并由本机完成兼容验证后锁定；云端尚未核实官方最新版本号。UI Mod 名称及缩放比例尚未确定，点击坐标保留占位。启用 UI Mod 时另存 `config.ui-mod.local.json` 并重新校准；原版用 `config.local.json`，不要混用两套宏。填版本号及匹配分辨率只能通过运行前检查，不能证明 UI 布局或 Mod 兼容；需要本机测试后再授权。

1. 使用**单人、非铁人**游戏，保留自己的存档备份。存档通常在“文档/Paradox Interactive/Stellaris/save games/”，也可能在 OneDrive。先手动保存，运行 `inspect <实际.sav路径>`，确认 `country_id`、版本、科技、库存、舰队与月净收入。不支持的字段会留空并标记，不会补成 0。
2. 复制 `examples/config.json` 为根目录的 `config.local.json`。路径相对于配置文件；修改 `save_path` 为实际存档、`country_id` 为自己的国家 ID、`campaign_id` 为该存档的 `metadata.campaign_id`、`state_dir` 为 `./.autopilot`（示例位于子目录，复制到根目录后需调整）。缺少可识别战役身份的存档只能观察，不能实时执行。`verified_game_version` 填入**本机核对过的完整** `metadata.game_version`，不是随意填写最新版。铁人或多人标志为 yes 时永远拒绝操作；仅在存档没有相应字段且你已核实模式时才将两个 `*_confirmed` 设为 true。
3. 配置 UI 操作。`calibrate --delay 5` 后切回游戏，把鼠标停在目标控件；输出客户区尺寸及 0–1 相对坐标。将 `resolution`、宏步骤、前置条件和**具体**后置条件改为本机值。示例坐标、科技 ID、舰队 ID 和首都星系 ID **都是占位，不可直接授权**。每种宏先从已知 UI 状态手动核对；不自动判断面板/弹窗、语言或 UI 缩放。
4. 将愿意交给 AI 的动作 ID 加入 `allowed_actions`。模型只能选这些 ID，不能提供坐标、控制台代码或权限。经济动作可用预先校准的付费建设宏，后置条件应检查对应星球的新队列/建筑；科研应检查具体科技 ID，不能用“合金随时间增加”代替动作验证。
5. 先运行 dry-run，再 `doctor`，最后显式使用 `--live`。不加 `--live` 从不发送游戏输入。

```powershell
$saa = ".\.venv\Scripts\stellaris-autopilot.exe"
& $saa calibrate --delay 5
& $saa doctor --config config.local.json
& $saa run --config config.local.json --planner rules --goal "科技优先，保持合金盈余"
```

### 使用 LLM 长期管理

本地 Ollama 示例：自行安装 Ollama，运行支持 tools 的模型（例如 `ollama pull qwen3:8b`）。模型是否已安装、是否支持强制工具调用由实际响应决定；项目不会自动下载模型。

```powershell
$env:AUTOPILOT_LLM_BASE_URL = "http://localhost:11434/v1"
$env:AUTOPILOT_LLM_MODEL = "qwen3:8b"
# 使用远程模型时改为提供商的 HTTPS /v1 地址，按需在本机安全设置 API key：
# $env:AUTOPILOT_LLM_API_KEY = ...
& $saa run --config config.local.json --planner llm --live --continuous --years 10 --goal "未来十年重点发展科技，保持合金盈余，尽量避免战争。遭到进攻时自动调集舰队防守。"
```

打开 Stellaris 后运行命令，默认有 5 秒切回游戏（可用 `--arm-delay` 调整）；每步输入都检查前台进程是否为 `stellaris.exe`。不会替你激活游戏窗口。`--continuous` 在新存档出现时重新规划，起始游戏日期加 `--years` 为结束点；暂停游戏不会推进期限。`rules` 是离线优先级规则，**不理解自然语言**，只有 `llm` 使用上述目标和四领域策略。远程 LLM 会收到规范化的玩家状态与目标，不上传原始存档、API key 或本地路径。

动作完成后需要**新存档**。可在 `save_steps` 配置本机已核对的快速保存宏，或依靠频繁自动保存/手动保存。确保观察路径是同一局最新存档；本版不自动挑选未知目录中的“最新一局”。默认 60 秒验证超时，可调至 600 秒；长距离舰队到达可能超过该时间，建议以本机实际可观察的移动订单作后置条件。日志 ACK、文件存在和发送按键成功都不算验证。`verified` 只表示配置的后置条件在新存档中成立，因果强度取决于你配置的条件。

### 可选的游戏内 Mod

```powershell
& $saa install-mod --mod-dir "$env:USERPROFILE\Documents\Paradox Interactive\Stellaris\mod"
```

将 Mod 加入 Paradox Launcher 的播放集。按实际文档位置修改路径；安装命令不会覆盖已有 Mod。先在游戏控制台手动执行 `event saa.1 <玩家国家ID>`，检查 `logs/game.log` 的 `SAA|bridge_enabled` 和 `logs/error.log`。月脉冲输出玩家观察日志；Python 日志监听器支持半行、截断和轮转。配置 `log_path` 为实际 `game.log` 可启用监听。

`defend_capital` 是固定事件 `saa.20`：清除所有未交战的自有移动军事舰队旧命令，排入移向首都的命令，不改资源或属性。它没有智能前线选择，也不保证安全路径。设置守卫和具体舰队/订单后置条件后才授权。`saa.2` 停用桥接；只停桥接不会撤销已下达的舰队命令。详见 [Mod 使用说明](mod/README.md)。

## 停止、恢复和结果

```powershell
& $saa stop --config config.local.json
& $saa journal --config config.local.json
# 在游戏内核实未验证操作后记录结论，而不是盲目重发：
& $saa reconcile --config config.local.json --id 1 --note "已核实游戏状态及队列，无需重复发送"
```

`stop` 创建 `state_dir/STOP`；恢复前由你删除该文件。宏等待期间约每 50ms 检查 STOP 和失焦，单次系统输入调用不能撤销。Ctrl+C 结束进程，PyAutoGUI 鼠标移至屏幕角落可紧急停止输入。事件输入中断后可能留下控制台和部分文字，必须人工核对。驱动绑定首次检查的游戏 PID，游戏重启后须重启自动驾驶。SQLite 在发送前持久登记，超时/异常/中断保留 `pending` 或 `unverified`，阻止重复派单，直到人工核对。不要同时运行多个使用不同 state_dir 的控制器。游戏焦点、菜单、当前存档发生变化时应停止；前台检查不能完全消除系统切换焦点的瞬间竞态。

状态区分：`dry_run` 未输入、`waiting` 无操作、`blocked` 拒绝、`dispatched` 仅已发送、`verified` 新存档满足条件、`unverified` 未确认。决策在 `decisions.jsonl`，操作在 `journal.sqlite`。本版只自动处理新观察后的重新规划；不对不明结果自动重试，也不会绕过失败的前置条件。

## MCP 与结构

```json
{"mcpServers":{"stellaris":{"command":"C:/path/qs/.venv/Scripts/stellaris-autopilot.exe","args":["mcp","--config","C:/path/qs/config.local.json"]}}}
```

MCP 暴露 `observe_empire`、`list_actions`、`plan_action` 和 `status`，全部只读；`plan_action` 是预览。真正执行由本机 CLI 管理，MCP 请求不能升级为 live。

MCP 查询稳定的数据库/WAL 私有副本，不在原状态目录创建 SQLite sidecar。主库与 WAL 总计上限 64MiB；三次快照均被并发写入改变时拒绝预览，不把未读到的未决动作视为空。归档数据库前先停止控制器并保留未决操作，勿在运行中单独删除 WAL。`doctor` 的 policy 检查失败和 `run` 的 blocked/unverified 返回退出码 2，便于脚本识别失败。

```text
src/stellaris_autopilot/
  pdx.py / saves.py       文本/ZIP 解析、玩家视角与版本差异
  observe.py             稳定快照、日志增量监听
  planner.py             离线规则 / OpenAI-compatible 工具调用
  config.py / predicates.py  本机授权和可观察断言
  engine.py / journal.py  执行闭环、持久去重与故障恢复
  windows.py             Windows 前台/DPI/输入桥接
  mcp_server.py / cli.py  MCP 与命令行
mod/                     PDXScript on_actions/events/scripted_effects
tests/                   解析、故障注入、模拟输入、HTTP/MCP/CLI测试
```

研究结论、许可证与精确提交在 [docs/research.md](docs/research.md)。四个参考项目均已审阅 README、结构及相关源码；本项目独立实现，无复制其代码。三个 Wiki 域名因云网络策略 403 尚未读到正文，不能宣称完成全部文档研究；需要在环境设置保存已添加的域名后继续核对。CWTools 类型定义提供了脚本交叉核查依据，不能替代实机测试。不宣称兼容尚未测试的游戏版本。
