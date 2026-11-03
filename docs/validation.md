# 本次交付的验证记录

## 2026-11-01 Windows 无键鼠直连实机补充

在本机 Cygnus v4.5.2 (9776)、独立用户目录和“地球联合国”存档副本中完成真实引擎测试。此节更新直连能力；下文的 Linux/Windows 历史轮次保留当时的验证范围。

- 暂停读档自动触发隐藏 Mod 事件；日志明确确认 `PAUSED_ON_LOAD`。
- 通过 `igc` 启动批处理，无键鼠解除暂停；未使用 `overnight` 性能测试模式作为正式入口。
- 定时 `run` 在 2200.01.02 读取第一版 mailbox；外部替换后在 2200.01.03 读取第二版，确认热读取。
- 真实 `debug_dumpgamestate` 产生约 19 MB 完整明文状态。观察器复制阶段检查稳定性，之后解析不可变数据。
- 同一普通舰队事件调用两次，日志只记录一次；新状态包含军事舰队 `actions.wait.time=30`，证明真实订单而非仅日志 ACK。
- **真实 stdio MCP → 指令文件 → 游戏引擎 → 新状态验证**通过：`probe_connection` 返回 `connection_verified`（2200.01.21），`execute_action("hold_military_fleets")` 返回 `action_verified`（2200.02.06）。停止工具成功清空队列，journal 无未决项。
- 实机发现全局舰队表含数字 ID 的 `none` 删除槽。已提炼 11 项最小回归；只有完整玩家 manager 下未引用的明确删除槽可忽略，引用缺失/冲突/重复/未知继续阻断。
- 实机否定 `country:0` 效果 scope；采用显式国家事件初始化唯一 session，再由 console 当前玩家根 scope 检查该 flag。
- 真实状态导出会重写 `continue_game.json`；每次隔离启动前重新固定真实 `.sav`，测试完成移除临时 `userdir.txt`。

直连当前仅支持固定探针和军事舰队等待；暂停唤醒、通用科技选择、付费建设/造舰及外交没有因此获得验证。细节与生命周期限制见 [direct-connection.md](direct-connection.md)。原 `mcp` 仍只读，实验性写接口是单独的 `direct-mcp`。

日期：2026-10-31。主机：Linux / Python 3.12.14。目标：最新稳定版 Stellaris，中文，2560×1440；没有本机游戏客户端或 Windows 桌面。

## 第一轮交付

- `uv sync --frozen --extra dev --extra mcp`：成功，使用工作区的 uv.lock。
- 完整 `pytest -q`：**209 passed**。包括 loopback HTTP OpenAI-compatible function calling、真实 MCP stdio 子进程、解析/预算/战争状态、Windows 输入的 fake transport、故障与重试测试。
- 最终单次规划退出码微调后，engine/CLI/save 的相关 **94 项**回归再次全部通过。
- `ruff check src tests`：通过。
- `uv build`：源码包与 wheel 构建成功，包含 Mod 事件、on_actions、scripted_effects 和中英本地化。
- wheel 安装至独立 `/tmp/saa-final-wheelcheck`，从 `/tmp` 运行 `demo`、`inspect` 和 `install-mod`：成功。未通过源码 checkout 导入；Mod 从 wheel 的 data-files 安装。
- `demo` 完成模拟选择科技→新文本存档→后置条件验证→SQLite verified，明确不是游戏执行证据。
- 指定四个 GitHub 项目的 README、目录及相关源码和许可证审查完成；没有复制项目代码。
- 指定 Wiki 和 Steam 官方最新版本 API 通过正常代理请求得到 403，尚未读到正文/最新补丁。网络域名需求已保存至环境配置草稿，不等于已生效。

**未执行**：Windows 实际 Win32/PyAutoGUI 输入、PDXScript 在游戏引擎中加载与舰队移动、真实玩家存档解析回归、真实 LLM 提供商凭据/模型、Windows CI。CI 已配置 Ubuntu/Windows × Python 3.11/3.12/3.13，但尚未在远端运行。

因此交付的是可安装和测试的自动驾驶基础首版，不是已经在最新游戏中验收通过的通用帝国自治系统。实机经济/科研仍需校准宏，舰队事件仍需对真实订单验证；尚无全自动 UI 识别、通用造舰/建筑队列或全面外交执行。

## 第二轮检查优化与交接

同日完成二次检查，最终完整套件：**262 passed in 8.97s**；`ruff check src tests` 与 `git diff --check` 均通过。代码优化及后续接手步骤见 [HANDOFF.md](HANDOFF.md)。

本轮增加并验证：

- 未变存档缓存、调用方状态隔离、尺寸限制、同大小改写后恢复 mtime 的识别、parse/hash 竞态拒绝、非普通文件提前拒绝。Windows 使用原生 ChangeTime 的路径已实现；这部分 API 尚未在 Windows 实测。
- SQLite 双子进程抢占、写入失败回滚、终态 CAS、限量历史摘要及派单前再次检查 freshness/STOP。
- MCP 对活跃 WAL 的读取不会修改原库/WAL/SHM，也不会在原目录生成 sidecar；持续变动、超限和不稳定快照会拒绝预览。
- Windows 模拟输入的 STOP、失焦、固定 PID、可中断等待和部分控制台输入保留现场。
- UTF-8 BOM 配置、非法字段类型与超大数值、保留动作名、`doctor` 失败退出码、错误 live 初始化不创建 journal、安装期间保护并发出现的 Launcher 文件。

已重建 wheel 和源码包，并在独立目录 `/tmp/saa-review-5gvn9hm7` 创建新虚拟环境，仅安装 wheel，从 checkout 外运行：

| 验证 | 结果 |
| --- | --- |
| `demo` | 成功，模拟结果 `verified`，不是游戏验证 |
| `inspect` 合成示例 | 成功，识别玩家 country 0 |
| `install-mod` | 成功，从 wheel 数据目录安装事件及本地化 |
| 中文本地化 BOM | 保留 UTF-8 BOM |
| 源码包内容 | 包含 `docs/HANDOFF.md`、`AGENTS.md`、`uv.lock` 与示例配置 |

CI 增加了源码包交接文件、wheel 中英本地化和独立 Mod 安装检查；远端 CI 依然未执行。本轮没有实机游戏验证，也没有改变第一轮记录的官方文档/最新版本联网限制。没有自动提交、推送或发布。

## 第三轮：Windows 本机接续测试

日期：2026-10-31。从 GitHub `main` 的 `9b497b7` 拉取源码，使用 Windows / Python 3.12.14 / uv 0.12.19，并通过锁文件安装 `dev`、`mcp`、`windows` 可选依赖。

首次原样复跑为 **261 passed, 1 skipped in 15.91s**。唯一跳过项是 Linux 平台拒绝测试，在 Windows 上按设计跳过；MCP、HTTP 集成测试均已运行。Ruff、构建和模拟演示通过。

本机继续验证发现并修复了三项问题：

1. **真实存档漏读玩家舰队**：支持 `country[id].fleets_manager.owned_fleets` 所有权关联和 `movement_manager.coordinate.origin` 位置；冲突、未知所属、缺失引用、重复 ID 保留未知语义，不再把无法识别的舰队表报告为已知空集。
2. **PyAutoGUI 紧急停止异常未受控退出**：将真实异常继承关系对应的 `FailSafeException` 转换为 `DriverError`，CLI 返回 2；不发后续输入或清理按键，不拦截 `KeyboardInterrupt`。
3. **Windows uv 独立 wheel 找不到 Mod**：当 `sys.prefix` 与缓存中的实际 wheel 安装位置分离时，使用安装元数据的 RECORD 定位 Mod data-files。

修复后的完整测试为 **285 passed, 1 skipped in 13.14s**；`ruff check src tests` 和 `git diff --check` 通过。

### 真实旧存档，只读验证

| 存档版本 | 游戏日期 | 玩家舰队 | 可读位置 | 玩家行星 | 战争状态 |
| --- | --- | ---: | ---: | ---: | --- |
| Corvus v4.2.4 | 2210.01.01 | 32 | 32 | 4 | 已知参战 |
| Pegasus v4.4.6 | 2230.07.19 | 6 | 6 | 1 | 未知，`war_status_known=false` |

行星计数已在第四轮按真实 owner 规则重新读取并更正；此前 21 / 7 的结果混入了仅有 controller 的天体。

两份存档均识别到唯一玩家、战役身份、资源和三个科研领域。第一份还保留了 10 支舰队的当前订单。第二份的 `missing_fields` 包含 `at_war`，不能将其布尔占位值解释为和平。原始存档未修改，也未把个人存档或真实身份加入源码；回归测试使用少量脱敏构造数据。

### Windows 与分发验证

- 实际 `WindowsDriver()` 初始化、Win32 函数绑定、DPI awareness 与 PyAutoGUI 导入成功；非游戏前台被正确拒绝。没有发送键盘/鼠标输入。
- 完整套件在本机实际覆盖了 Windows 文件 ChangeTime 的缓存失效和读写竞态路径。
- wheel 和源码包重新构建成功。独立 uv 环境在 checkout 外运行 `demo`、示例 `inspect`、`install-mod` 均成功，确认模块来自 wheel 缓存而非源码目录。
- 六项 Mod 资源与源码逐字节一致，中英本地化 UTF-8 BOM 保留；源码包包含交接文档、AGENTS、锁文件及配置示例。Mod 安装仅写入测试目录。

本机安装元数据显示 **Cygnus v4.5.2 (9776)**，但没有运行中的游戏，也没有该版本的新存档。保存的显示设置为中文、2560×1600、UI 缩放 1.0；这不是客户区实测，不能沿用示例的 2560×1440 坐标。仍未验证当前安装版本的 PDXScript 加载、实际输入、舰队订单或 UI 宏，也未调用真实 LLM 提供商或运行远端 CI。模拟演示的 `verified` 仍仅代表回放成功。

## 第四轮：4.5.2 新局存档

同日用户提供了新局信息，本机找到对应的 2200.01.01 存档。存档版本为 **Cygnus v4.5.2**；通过 `player.country` 识别玩家国家 0，`galaxy.ironman=no`，用户另行确认单人、无 Mod。原档未修改，个人存档和本地配置没有加入源码。

只读解析及结构核对确认：8 类库存资源、9 个自有舰队对象（含非军事对象）、明确为空的战争表、当前和平。三类科研队列均不存在，备选科技已存在且自动科研关闭，符合新局尚未选择科技；规范化结果保留 `research={}` 和 `missing_fields=["research"]`，不伪造已选科技。

发现并修复一项真实结构问题：4.5.2 的恒星和未殖民天体可能只有 `controller=0`，原先的 controller 回退会把它们当作自有行星。现在行星仅依据实际 `owner`，正确识别 **1 颗自有殖民行星**；同时保留 `planet_class`、`planet_size`、`colony` 和 `build_queue` 实际字段。殖民地 ID 与行星 ID 分别处理，`build_queue` 仍是引用，不代表已支持其队列详情。两份旧档已用新规则重读，自有行星分别为 4 / 1，第三轮表格已更正。

修复后完整套件：**286 passed, 1 skipped in 19.22s**；唯一跳过项仍为 Linux 平台拒绝测试。Ruff 和 `git diff --check` 通过，增加了归属与新旧字段保留的脱敏回归。

本地观察配置已关联该战役和存档，并记录用户确认的单人模式；`allowed_actions=[]`、未填已验证版本及客户区尺寸。离线规则的 dry-run 返回 `waiting`，不发送游戏输入。`doctor` 在单人确认前正确拒绝未知模式；确认后正确停在未校准版本检查。后续仍需在运行中的游戏完成客户区、宏及实际动作验证，不能把本次读取成功称为游戏操作成功。

## 第五轮：真实游戏窗口与首次科研验证边界

用户打开游戏后，识别到实际 `stellaris.exe` 进程和唯一的 Stellaris 窗口。真实 `calibrate --delay 0` 通过前台标题、exe、PID、DPI、客户区及坐标检查，读取到 **2560×1600**，本地配置已保存这一实测尺寸。鼠标当时的位置没有作为控件坐标保存，也未下达游戏操作。

Windows Computer Use 的窗口截图连续两次失败，准确错误为 `SetIsBorderRequired failed: 不支持此接口 (0x80004002)`；无截图的可访问性读取只返回整个游戏窗格，不能定位游戏控件。已请求当前游戏截图，具体宏坐标及暂停状态仍待画面核对。

同时确认并修复了新局首次选择科技的验证边界：默认规则要求 expected 路径在派单前存在，会使 `research={}` 的新局永久拒绝首次选择。新增本地动作字段 `allow_new_expected_paths`，默认空列表；仅能显式指定本动作精确 `eq` 后置路径及明确非空目标。前置条件、`changed` 及未申请的路径保持旧规则，执行后仍要求新快照和精确目标值。配置拒绝重复、非法路径、空值、容器及非有限数值；值不符、路径仍缺失、同 digest 或旧 mtime 均保留 `unverified`，阻止重试。

新增回归后完整套件为 **320 passed, 1 skipped in 18.63s**；Ruff 和 `git diff --check` 通过。当前本地动作授权仍为空，已验证版本仍未填写；截图失败没有被当成宏校准成功。

## 第六轮：Steam 本地截图和点击遮挡防护

用户提供当前游戏截图后，确认 2200.01.01、太阳系视图及暂停状态。Steam 默认 F12 已在本机实际成功生成本地游戏截图，可继续核对界面。物理学候选项为改良偏射盾、零重力实验室、量子计算，与实际存档中的科技 ID 对应；“量子计算”候选宏的像素坐标和精确 `tech_physics_1` 后置条件已准备，尚未授权派单。

现场还发现右下角的外部悬浮窗会遮挡游戏主菜单，即使游戏仍拥有前台。UI 输入工具明确拒绝该非游戏目标。项目 WindowsDriver 因此增加 `WindowFromPoint` 与 `GetAncestor(GA_ROOT)` 检查，在移动前及点击前确认实际命中同一游戏根窗口；允许游戏子窗口，拒绝外部窗、查询失败及移动后出现的遮挡，原有前台、PID、客户区和 fail-safe 检查保留。用户移开悬浮窗后，实际无输入命中检查确认物理学通知与右下菜单均属于游戏窗口。

新增回归后完整测试为 **333 passed, 1 skipped in 13.48s**；Ruff、`git diff --check` 和构建通过。游戏菜单、保存对话框及关闭云存档选项已核对，仍保持暂停。界面工具的键盘输入没有写入保存文件名，已请用户手动填写独立测试档名；原始存档另有本地备份。当前仍无项目 Engine 的真实科技派单与新存档 `verified` 证据，保存宏及该闭环继续校准。

## 第七轮：改为精确 MCP、原生队列与全局知识

2026-11-01 用户明确要求整体不使用键鼠，由 MCP 指定具体对象和动作。本机采用原始地球联合国存档的隔离副本、独立 userdir、固定 Mod 模板和精确构建的原生命令适配。原 EXE SHA-256 固定为 `400df27c82ddc845aa9dce79bd468d81f18f060d299cd263e3afa93aef4f7a83`，Frida 固定 17.9.4，注入器运行在单独子进程；只管理启动器自己创建的游戏进程。

真实 stdio MCP 已通过指定舰队/星球环绕、替换为 45 天等待，以及三个科研领域的具体候选选择。科研保持正常未完成过程，已完成科技数保持 31。旧脚本队列必须先使用 `clear_fleet_actions` 清理，再清普通订单，防止等待被追加。

原生私有注册消息通道可在暂停时进入已固定的游戏线程。没有每日邮箱调度的实测中，暂停、观察、科研和两次精确舰队命令均保持在 2200.01.13，恢复后日期推进到 2200.01.20。暂停成功需要游戏内 `is_paused` 条件写出的唯一回执及独立 live runtime；命令 ACK 不够。保护暂停来源和模式保留，暂停指令禁止由每日文件回退执行。

### 真实建设目标错误与修复

实测普通建筑被正常校验拒绝，进一步定位到 ID 空间混淆：物理地球 ID=3、殖民地 ID=0；建设项目中历史字段 `planet` 指殖民地，队列 location type2 指物理星球。此前城市区划 500 矿物试验只证明了付费及排队，不能视为正确目标通过。旧记录保留，新的验收必须完整关联项目殖民地、`colony.carrier`、物理星球 colony/build_queue、付款国和正常队列。

修复后的真实 MCP 在地球分区0排入研究实验室，成本400矿物、progress0/360，原有建筑不变。原生层还核对完整殖民地 handle、队列与 owner0。旧验证器留下的未决建筑操作经过新解析器和原始 dump 本地检查后记 reconciled，没有重发。区划以修正后的新独立 MCP 试验作为目标证据，历史的数值直接相等检查已移除。

### 全局机制参考

用户要求知识包括飞升、传统、内阁等所有流派内容，实际控制优先星球建设与飞船。新增本机 4.5.2 资料编译器及 MCP 搜索/查询工具，输出 22,539 条、92 类定义：包括49飞升天赋、234传统、77起源、281国民理念、1,126特质及科技/经济/外交/危机/defines等。重复ID与条件保持显式，文件来源有哈希，变量和 inline_script 可追溯，44条歧义不当作可用条目。

真实 MCP 查询通过中文研究实验室、精确建筑成本/工期/维护费、舰船组件等。观察增加只读 `empire_build`，缺失的传统/飞升列表保持未知。官方与灰机 Wiki 页面/API 均遭挑战或401/403，正文导入数为0；没有把错误页面当正文。可读导出的导入结构保留 URL、修订、许可与适用版本。

最终完整 pytest 数量、Ruff、diff检查、wheel/.js字节核对和补丁验证由输出目录的最新 `qs-direct-test-report.md` 与 `pytest-windows-direct.xml` 记录。测试全过程未向游戏发送键鼠输入，原始存档哈希未变，原用户目录未启用 Mod；结束后清理临时 userdir.txt 与自己的游戏进程。仍未接入造舰、跨系统勘察/建站/殖民、完整战斗和全部建设变更。
