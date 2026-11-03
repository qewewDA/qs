# Stellaris AI Autopilot · 群星 AI 自动驾驶

> 一次接入，之后直接中文指挥：先生成离线入口，把 `stellaris_agent` 提示词发到同一个Agent对话一次，之后直接说中文命令。详见 [离线 AI 入口](docs/ai-entry.md)。

用 MCP 精确指挥单人《群星》：模型只能从新观察到的状态里选择具体星球、舰队、科技和已授权动作，不能提交控制台脚本、坐标、原生地址或文件路径。推荐入口是附加已运行模式 `scripts/serve_existing_game.py`：你先正常打开群星、读档并暂停，MCP 只连接当前局，不启动也不关闭游戏。需要由启动器创建进程并从最近不可变检查点接续时，使用保留的持久战役启动器 `scripts/serve_campaign.py`。

Python 3.11+，Windows 10/11 为实机目标；MIT 开源。本文描述的 MCP 入口不发送键盘或鼠标输入；旧的 Windows 输入路径另述。仓库只包含源码、Mod 模板和测试，不附带游戏本体、知识 JSON、真实存档或本机 MCP 配置。

## 当前能力与边界

**已验证构建：`Cygnus v4.5.2 (9776)`。** 原生适配器固定校验 `stellaris.exe` 的 SHA-256（`400df27c82ddc845aa9dce79bd468d81f18f060d299cd263e3afa93aef4f7a83`），Frida 固定为 `17.9.4`；构建或哈希不符时拒绝原生控制。当前原生后端只支持玩家国家 `0`、单人、非铁人。这是固定版本、固定样本的实机结果，不能外推为全部 DLC、全部起源/特殊建筑/舰种或未来版本均已支持。

已经实机通过的范围包括：

- 暂停/恢复、暂停状态下观察，以及指定科研领域的精确科技选择；
- 指定舰队的等待、环绕，以及跨系统移动/停止的订单入列；
- 城市区划、指定分区建筑的正常付费入队，以及升级、拆除、转区和取消；
- 普通与殖民船建造入队、指定天体勘察、采矿/科研/前哨站订单、普通殖民命令与殖民 landing。

必须区分三种证据：**入队/下单成功**（新状态出现正确项目或订单）、**自然完成**（游戏推进到项目或舰队动作实际结束）、**landing/殖民发展**（殖民船真正落地、殖民地开始发展）。本仓库不会用前一种证据代替后一种：`queue_colony_ship` 只证明殖民船建造入队，`colonize_planet` 与 landing 另行验证，完整殖民发展仍未验证；造舰、勘察、建站和移动的多数结果只到精确入队/订单。内阁、传统和飞升选择保留给用户决定；战斗、外交和全部建设变更没有完整接口。

完整接口与逐项证据边界见 [MCP 逐项指挥](docs/direct-control.md)，持久战役的目录、保存与恢复见 [战役接续](docs/campaign.md)。旧的 Windows 键鼠输入和只读 MCP 仍在仓库中，但不是本入口的控制方式，说明见下文及 [无键鼠直连](docs/direct-connection.md)。

## 安装

在仓库根目录，Windows PowerShell：

```powershell
uv sync --frozen --extra dev --extra mcp --extra native
uv run python scripts/serve_existing_game.py --help
uv run python scripts/serve_campaign.py --help
```

`native` 额外依赖安装 Frida（仅 Windows 生效）；`mcp` 提供 MCP 服务端。没有原生依赖时可以运行离线解析、模拟和只读 MCP，但不能建立原生控制。Linux/macOS 可运行观察、规划、模拟和测试，真实游戏控制只支持 Windows：

```bash
uv sync --frozen --extra dev --extra mcp
uv run stellaris-autopilot demo
uv run pytest -q
```

`demo` 使用独立临时目录和 replay adapter，执行解析 → 授权 → 派单 → 新观察 → 后置条件 → SQLite 记录，输出 `SIMULATION` 和 `verified`。它不连接游戏，也不调用付费模型，不能当作实机验证。

## 快速开始：附加已运行（推荐）

先正常启动群星、手动读入要接续的存档并暂停；不要用本项目启动器启动这个实例。然后在仓库根目录运行：

```powershell
uv run python scripts/serve_existing_game.py `
  --state-dir .\attached-state `
  --game-dir "<Steam>\steamapps\common\Stellaris"
```

用户目录默认从 `game-dir\userdir.txt` 或 Documents 解析，也可以用 `--user-dir` 明确指定。同机有多个 `stellaris.exe` 时在本机加 `--pid`，不要让模型猜。`--extended` 与直连模式相同，`--knowledge-bundle` 可选。

MCP 客户端配置示例（路径替换为本机实际值；`cwd` 指向仓库根目录）：

```json
{
  "mcpServers": {
    "stellaris-existing": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": [
        "<repo>\\scripts\\serve_existing_game.py",
        "--state-dir", "<attached-state>",
        "--game-dir", "<Steam>\\steamapps\\common\\Stellaris"
      ],
      "cwd": "<repo>"
    }
  }
}
```

该入口只附加当前已运行的游戏，不启动、不关闭、不自动载入旧检查点。连接成功后提供 `attached_status()`、`checkpoint_game()` 和 `disconnect_game(save=true)`：检查点写到游戏的 `save games/qs_mcp`，断开只释放 AI 接管，游戏继续运行。完整流程、身份校验、换档重连和常见问题见 [附加已运行群星](docs/attach-existing.md)。需要启动器自动加载最近检查点时，继续使用下面的持久战役模式。

## 快速开始：持久战役 MCP

启动器自己创建并持有游戏进程；启动前必须没有其他 `stellaris.exe` 在运行，并先在 Steam 客户端登录。它不会接管或关闭手动打开的游戏。默认安装目录可用 `--game-dir` 覆盖。

首次导入一个**单人、非铁人**存档（原档只读，导入后保留副本）：

```powershell
uv run python scripts/serve_campaign.py `
  --campaign-dir .\campaign `
  --initialize "<你的存档>.sav" --country 0 --single-player --non-ironman `
  --game-dir "<Steam>\steamapps\common\Stellaris"
```

首次成功后去掉 `--initialize`，之后每次连接同一个 `--campaign-dir`：

```powershell
uv run python scripts/serve_campaign.py `
  --campaign-dir .\campaign `
  --game-dir "<Steam>\steamapps\common\Stellaris"
```

MCP 客户端配置示例（路径替换为本机实际值；`cwd` 指向仓库根目录）：

```json
{
  "mcpServers": {
    "stellaris-campaign": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": [
        "<repo>\\scripts\\serve_campaign.py",
        "--campaign-dir", "<campaign-dir>",
        "--game-dir", "<Steam>\\steamapps\\common\\Stellaris"
      ],
      "cwd": "<repo>"
    }
  }
}
```

`--status` 只读检查 manifest、检查点和历史命令，不启动游戏。`--recover-last-checkpoint` 只在明确接受“从最近完成检查点恢复、丢弃上次未保存进度”时使用；存在未决命令时仍然拒绝。

### 日常 MCP 顺序

1. `campaign_status()` 查看当前检查点、存档日期、会话历史和未决命令。
2. `request_observation()` 读取本次会话的新状态。第一个成功的新观察会确认确实载入了当前检查点；身份、日期或玩法状态不符时拒绝后续写入。
3. 用精确工具操作具体对象；每项都核对新状态中的真实后置条件，不把日志 ACK 当作成功。
4. 中途 `checkpoint_campaign()` 保存新检查点并继续保持暂停。
5. 结束前显式调用 `save_and_close()`，确认返回 `saved_and_closed` 且 `owned_process_closed:true`，再断开 MCP。
6. 下次连接同一目录，加载 manifest 指向的最新检查点。

断开 MCP 时的自动保存只是尽力而为；客户端可能在数秒内强杀服务，不能保证断开本身完成保存。保存失败会保留正在运行的游戏并记录 `save_failed`，由用户通过游戏自身界面人工挽救；程序不会自动重试或猜测进程。

### `--reload-proof` 与生产退出

`--reload-proof` 是可选参数，但生产退出需要它：报告必须是针对**当前游戏构建、当前战役**的真实两轮重载实验，包含 EXE hash、版本、国家、两轮保存/关闭/重载状态 hash 和清理结果。没有报告时仍可创建并保留解析检查点，但 `save_and_close()` 会拒绝关闭游戏（游戏保持暂停）。仓库不附带该报告或生成它的研究实验脚本；干净 clone 首次使用通常只能保存检查点后人工退出，或自行按相同口径完成两轮重载实验。指定无效报告会在创建会话和启动游戏之前直接拒绝。

### 可选知识库

`--knowledge-bundle` 指向本地 `GameKnowledge` JSON（默认尝试仓库相对路径 `artifacts/qs-game-knowledge.json`，不存在则不启用知识工具）。知识库包含本机游戏定义与本地化编译结果，体积较大，**不随仓库分发**；请放在 git 忽略的位置或 `artifacts/` 下且不要提交。资料只用于检索建筑、区划、科技、飞升、传统、内阁、舰船、经济、外交和危机等定义，不会授予任何游戏操作权限。官方 Wiki 与灰机 Wiki 正文当前未导入（0 条），不能宣称已抓取网站全部正文。

## 精确控制接口

默认授权（不加 `--extended`）只有原有实机证据的四个原生操作，以及两种固定舰队订单：

- `select_research`、`queue_district`、`queue_building`、`set_paused`；
- `issue_order` 的 `fleet_wait` 与 `fleet_orbit`。

加 `--extended` 后额外开放升级、拆除、转区、取消建设、普通/殖民造舰、勘察、建站、殖民、跨系统移动和停止订单。接口存在不等于全部实机通过；逐项范围见 [MCP 逐项指挥](docs/direct-control.md)。所有工具都执行本地授权、所有权、可观察性、暂停和真实后置条件检查；模型不能选择进程、DLL、函数地址、路径、原始脚本或执行代码。

原只读 MCP 入口仍可用，只暴露 `observe_empire`、`list_actions`、`plan_action` 和 `status`，全部只读：

```json
{"mcpServers":{"stellaris":{"command":"<repo>\\.venv\\Scripts\\stellaris-autopilot.exe","args":["mcp","--config","<repo>\\config.local.json"]}}}
```

旧的 Windows 输入路径（`calibrate`/`run --live`）使用 `PyAutoGUI` 和本机宏坐标，需要单独安装 `windows` 额外依赖并按本机分辨率校准；示例坐标一律是占位。它和精确 MCP 是两套入口，不能把一套的验证结果套到另一套。

## 其他常用命令

```powershell
uv run stellaris-autopilot inspect "<你的存档>.sav"
uv run stellaris-autopilot install-mod --mod-dir "$env:USERPROFILE\Documents\Paradox Interactive\Stellaris\mod"
uv run pytest -q
uv run --all-extras ruff check src tests scripts
```

Mod 安装命令不会覆盖已有 Mod；安装后需在 Paradox Launcher 播放集中启用。动作完成需要新存档；日志 ACK、文件存在和发送按键成功都不算验证。状态区分 `dry_run`、`waiting`、`blocked`、`dispatched`、`verified`、`unverified`，未决操作保留到人工核对，不自动重发。

## 目录结构

```text
scripts/serve_existing_game.py  附加已运行游戏 MCP 启动器（推荐日常入口）
scripts/serve_campaign.py   持久战役 MCP 启动器（保留）
src/stellaris_autopilot/
  attached_process.py / attached_runtime.py  只读附加、会话初始化、检查点与断开
  direct.py / direct_mcp.py     精确 MCP 工具与本地授权
  campaign.py / campaign_runtime.py  不可变检查点、会话、保存与重载门禁
  native.py / native_*.js       固定构建的原生科研、建设、舰队命令适配
  pdx.py / saves.py             文本/ZIP 存档解析与玩家视角
  observe.py / planner.py       快照、规则/LLM 规划
  config.py / engine.py / journal.py  授权、执行闭环与持久日志
  windows.py / mcp_server.py / cli.py  Windows 输入、只读 MCP、命令行
mod/                          PDXScript 模板
tests/                        解析、故障注入、模拟输入、MCP/CLI 测试
```

## 验证与限制

2026-11-02 的 Windows 实机验收构建为 `Cygnus v4.5.2 (9776)`；完整回归为 **1628 passed, 3 skipped**，`ruff check src tests`、`git diff --check`、wheel/sdist 构建与包内文件核对通过。实机证据来自隔离测试副本：`keyboard_mouse_input=false`、`native_ai_takeover=false`，不是对用户持久战役的操作。验收报告和证据记录不随源码分发（测试源码随 sdist 提供）；详见 [验证记录](docs/validation.md)。

当前明确未覆盖：全部 DLC/起源/特殊建筑与舰种、完整殖民发展、完整战斗与外交、任意版本二进制存档格式、客户端强杀/断电下的保存保证、远端 CI。固定版本或 EXE 哈希不匹配时，原生控制会被拒绝，只保留观察/规划等离线能力。研究结论、许可证与参考项目审查见 [docs/research.md](docs/research.md)。
