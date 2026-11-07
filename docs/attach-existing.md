# 附加已运行群星

附加模式是推荐的日常入口：连接用户自己启动、已经读档并暂停的群星进程。它只取得进程的查询/等待句柄和现有原生命令通道，不启动游戏、不关闭游戏、不自动载入旧检查点，也没有终止游戏的接口。

## 与持久战役模式的区别

| 项目 | 持久战役模式 | 附加已运行模式 |
| --- | --- | --- |
| 游戏由谁启动 | 启动器通过 WMI 创建并持有 | 用户自己正常启动 |
| 接续来源 | manifest 指向的最近完成检查点 | 游戏当前内存中的存档 |
| 连接后的第一件事 | 核对并载入检查点身份 | 核对当前局身份和暂停状态 |
| 结束方式 | 显式保存检查点后可关闭游戏 | 保存检查点或直接断开；游戏保持运行 |
| 进程权利 | 自有进程句柄 | 仅查询/同步句柄，没有终止权 |
| 换档 | 由启动器载入检查点 | 用户手动读档后重新连接 |

附加模式不会把“断开连接”当成“保存退出”，也不会在连接时自动载入上一次的检查点。用户在游戏里切换到另一份存档后，旧的附加连接身份已经变化，必须断开并重新连接当前局：先调用 `disconnect_game(save=false)` 断开旧连接，再调用 `connect_game()` 连接当前存档；不需要重启 MCP 服务，也不要用旧会话执行保存。

## 前置条件

- Windows 10/11；非 Windows 平台直接拒绝附加。
- 游戏由用户正常启动，版本与项目固定校验的构建一致；`stellaris.exe` 的 SHA-256 不匹配时拒绝附加。
- 单人、非铁人存档；多人或铁人存档不适用于现有直连/原生命令边界。
- 游戏已经进入存档并保持暂停。附加模式不会替用户点“继续”、读档或恢复时间。
- 同一台机器上只运行一个准备连接的游戏。若确实有多个候选进程，必须在本机明确指定 PID；模型不能自行选择或猜测 PID。
- 该进程不能带有另一个启动器的 `-qsowned=` 标记。带此标记的游戏由对应持久战役启动器管理，应改用它的连接，而不是再次附加。

## 启动与 MCP 配置

在仓库根目录（Windows PowerShell）：

```powershell
uv run python scripts/serve_existing_game.py `
  --state-dir .\attached-state `
  --game-dir "<Steam>\steamapps\common\Stellaris"
```

- `--state-dir` 必填：保存附加会话、`direct.json`、`before-attachment.sav` 和检查点记录；不要把游戏本体或用户目录指向这里。
- `--user-dir` 可选：不填时从 `game-dir\userdir.txt` 或默认 Documents 目录解析；必须与游戏当前实际使用的用户目录一致。
- `--pid` 可选：本机有多个 `stellaris.exe` 时明确选择；模型不能填写或猜测。
- `--extended` 与直连模式相同；`--knowledge-bundle` 可选。
- `--connect-timeout` 可选，默认 45 秒（范围 5–120 秒），是 `connect_game()` 的初始化预算，不是严格的墙钟取消期限。进程检查和原生 RPC 等待会按剩余预算收紧；文件解析、同步清理等本地步骤仍可能额外耗时，因此不承诺到点强制中断。MCP 握手和工具暴露不等待游戏。

MCP 客户端配置示例（路径按本机替换，`cwd` 指向仓库根目录）：

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

入口启动后立即完成 MCP 握手并暴露工具，不先附加游戏；即使当前没有运行中的游戏，`attached_status()` 也能返回缓存状态，知识包缺失也不会阻塞握手。首次连接由模型调用 `connect_game()`，只有成功后才发布 runtime；返回值包含已核验的精简初始快照（国家、日期、战役、快照摘要和暂停状态），不要为接入再请求完整 dump。连接失败会报告阶段和原因，模型应停止并让用户修正；用户纠正前置条件后才用 `connect_game(retry=true)` 再试，不会自动重试。服务从一开始就提供 `attached_status()`、`connect_game()`；连接成功后使用 `checkpoint_game()` 和 `disconnect_game(save=true)`；`checkpoint_game` 把新存档写到 `save games/qs_mcp`，`disconnect_game` 只断开 AI 接管，不关闭游戏。

## 连接状态与重连

`attached_status()` 只报告宿主状态：未连接时不访问游戏，不枚举或注入进程，也不读取存档或请求 dump；已连接时只通过已持有的进程句柄做一次快速存活检查，不重新执行 WMI 枚举、不注入、不请求 dump。

### 一次失败提供完整诊断

`connect_game()` 在原生 worker 启动前，同时检查本机 exe 指纹、历史未决 journal 和控制器锁。
这些文件检查不会附加游戏、执行原生命令或读取游戏存档。失败回执直接包含：

- `code`、`phase`、`reason`、`next_step` 和 `retry_automatically=false`。
- `diagnostics.executable`：实测 SHA-256、相邻 launcher 文件中的版本提示、受支持版本和指纹。
  launcher 版本只是标签，不能授权原生调用；无法读取 exe 时 `supported=null`，返回 `executable_unreadable`。
- `diagnostics.unresolved_operations`：未决操作的 session、operation ID、已知动作种类和未知结果。
  检查所有 session，公开最多 10 条，`count_at_least` 是数量下界；不会改写、解决或重放操作。
  记录无法读取时返回 `journal_unreadable`，不能据此声称确有未决记录或 journal 为空。
- `diagnostics.controller_lock`：锁是否存在、可识别的控制器 PID 和 session；仅凭锁文件不能判断 owner 存活，
  `owner_liveness=unknown` 时保持锁，由本机核实归属。

出现多项阻塞时使用同一回执里的诊断，不逐项修正后反复连接才能发现下一项。
不得通过 SQL、删锁、修改版本文字或放宽 hash pin 来绕过检查。
`unsupported_game_build` 需要验证这个构建的原生函数地址、对象布局和运行结果；
**重建机制知识包不能让原生桥兼容新版本**。
当前精确 hash 表保留 Cygnus v4.5.2；新增 Cygnus v4.5.3 的受限观察接入：
56 个函数/全局/vtable 地址按当前代码重新定位，注入脚本在创建原生函数或 hook 之前，
检查实际加载镜像中 32 个函数的前 32 字节，防止磁盘已更新而运行进程仍是旧版。
profile、地址和能力由宿主选择，不是 MCP 参数。源码地址库存变化会拒绝生成脚本。

4.5.3 当前只允许观察、验证暂停为 true、连接探针和保存检查点，摘要标记
`control_scope=observation_pause_checkpoint_only`。舰队命令、建设、造舰、研究选择和推进时间均拒绝；
DirectConnection、原生 Python submit 和脚本 RPC 都有能力检查。
连接先检查窗口绑定回执中的暂停状态；未暂停或无法证明暂停时，不请求 dump 或加载会话事件。
静态定位和只读内存核验不等于实机控制通过，玩法命令需逐类取得真实后置条件证据后再开放。

连接成功、失败、取消完成、检测到 stale、断开和服务关闭都会更新 `connection-progress.json`。
普通 `attached_status()` 使用服务内存状态；进度文件用于本机诊断，不能把上次服务遗留的文件当作当前已连接。
如果进度文件不可写，回执标明 `progress_persistence_error`，已验证的连接不因此被撤销。

### DSH 等通过 SDK 包装工具的客户端

外层只有 `run_code` 并不表示 MCP 工具缺失。以客户端完整 SDK 的 `ToolArgsMap` 声明为准，
已声明的 MCP 方法通过 `tools["mcp__stellaris-campaign__attached_status"]` 等名称调用。
不要靠 `Object.keys(tools)`、函数反射或猜工具名排查。

一次性提示词提供了可执行示例：一次程序检查状态，先解包 `structuredContent`，
缺失时解析 JSON text，再仅在 `state=disconnected` 且 `mcp_ready=true` 时调用一次 `connect_game({})`。
这样首次状态检查和连接只需一次模型往返；两个依赖的服务调用仍顺序执行。
`failed`、`stale`、`connecting`、错误回执或无法解析的内容都会停止，示例不会自动填 `retry=true`。

游戏退出或句柄失效后，`attached_status()` 返回 `connected=false`、`state=stale`，清除初始摘要，并给出重新打开游戏、读档、暂停后重连的引导；不会自动重连。

MCP 客户端取消连接请求时，活动尝试会在清理期间显示 `state=cancelling`（`connected=false`）。取消只作用于本次 attempt：在进程发现、原生启动和每次 RPC 前检查；已开始的步骤会等待完成并同步清理，清理完成后取消请求才结束；不会把取消的连接保留为可用 runtime（若 runtime 已建立但结果交付前被取消，也会关闭并标记失败）。取消一个 busy 请求不会取消另一个正在进行的 attempt。取消清理完成后如需再连，使用 `connect_game(retry=true)` 发起新的尝试。

重连按情况处理：

- **游戏已退出或句柄失效（`state=stale`）。** 用户重新启动群星、读入目标存档并暂停后，调用 `connect_game(retry=true)`；它先清理旧会话，再核对当前局身份和暂停状态，不沿用旧 PID、窗口 token 或快照。`state=stale` 时 `disconnect_game(save=true)` 会拒绝保存；要释放旧连接可用 `disconnect_game(save=false)`。
- **游戏仍在运行，但用户已经切换存档。** 先调用 `disconnect_game(save=false)` 断开旧连接，明确不保存旧会话；再调用 `connect_game()` 连接当前存档。不要直接调用 `disconnect_game(save=true)`，否则旧会话的保存请求可能发往新存档。
- **同一个 MCP 服务进程可以连续重连。** 换档或游戏重启不需要重启 MCP 宿主；只有工具列表不可见、客户端配置变化或服务进程已退出时，才需要重连客户端或重启服务。

## 基本流程

1. **正常打开游戏。** 从 Steam 或用户惯用的方式启动群星，手动读入要接续的存档，进入游戏后暂停。不要用本项目的启动器启动附加模式的目标进程。
2. **保持游戏运行并暂停。** 附加模式不会自动关闭游戏；暂停只是为了让第一次观察和后续精确指令有稳定起点。
3. **完成 MCP 握手。** 按上面的命令启动 `serve_existing_game.py`，指向当前游戏实际使用的用户目录和本地配置。MCP 客户端先完成 initialize 和 `tools/list`；此时工具已经可见，且不会访问游戏。
4. **连接当前局。** 先调用 `attached_status()` 查看缓存状态。`state=stale` 时按上面的重连步骤处理；首次连接或已修正失败原因后，调用 `connect_game()`（必要时 `retry=true`）一次。成功返回值已经包含核验过的精简初始快照，直接报告帝国、日期和可用操作，保持暂停；不要为了接入再请求完整 dump。若返回 blocked、busy 或 failed，报告 phase、reason 和用户需要修正的一步后停止；用户修正后明确要求再连时，才调用 `connect_game(retry=true)`。
5. **精确指挥。** 使用与直连模式相同的固定工具执行明确动作。每次写入前先取得新观察；只使用本次状态中真实存在的对象 ID。模型文本不能变成控制台命令、脚本或键鼠坐标。
6. **保存检查点或断开。** 需要保留进度时调用 `checkpoint_game()`，核对返回的 `checkpoint_saved` 和新存档路径；它只保存当前局，不会关闭游戏。结束时调用 `disconnect_game(save=true)`，确认 `saved=true`、`game_closed=false` 后再断开 MCP；不需要保存时可用 `disconnect_game(save=false)`。断开只释放附加句柄，游戏继续运行。
7. **换档后重新连接。** 用户在游戏内读取另一份存档后，先调用 `disconnect_game(save=false)` 断开旧连接（不保存旧会话），再调用 `connect_game()` 连接当前局；同一个 MCP 服务进程可以继续使用，不需要重启宿主。若游戏已经退出，则重新打开、读档、暂停后调用 `connect_game(retry=true)`。不要复用旧连接的 PID、窗口 token 或状态快照。

## 附加模式不会做什么

- 不会在 MCP 启动时自动附加游戏；`connect_game()` 之前只提供握手和缓存状态，不枚举或注入游戏进程。
- 不会在已连接状态反复枚举进程、注入或请求 dump；`attached_status()` 只对已持有的进程句柄做快速存活检查。
- 不会在找不到运行中的游戏时自动启动一个游戏；没有候选进程就报错，由用户自己启动。
- 不会因为 MCP 断开、Python 对象被回收、检查点失败或异常而关闭游戏。`close()` 和垃圾回收只关闭持有的句柄。
- 不会调用 `TerminateProcess`，也不会暴露 `terminate`/`kill` 方法。
- 不会自动载入上一次检查点，不会自动切换到另一份存档，也不会在换档后沿用旧会话状态。
- 不会自动重连；`state=stale` 只提供重连引导，必须由用户重开游戏、读档、暂停后显式调用 `connect_game(retry=true)`。
- 不会附加到带 `-qsowned=` 标记的进程，也不会接管另一个启动器已经管理的游戏。
- 不会把 PID 当作长期身份。PID 只在本次连接内用于定位，真正的身份是映像路径和创建时间；PID 被系统复用后会因创建时间不符而拒绝。

## 身份校验顺序

1. 检查平台为 Windows，并校验用户给出的 PID 形状（若给出）。
2. 解析并检查选中的 `stellaris.exe`，校验固定 SHA-256。
3. 通过只读 WMI 调用枚举 `Win32_Process`，记录 `pid`、`executable`、`creation_filetime` 和 `command_line`。该查询只读取，不创建进程。
4. 没有指定 PID 时要求恰好一个候选；指定 PID 时要求该 PID 恰好对应一个候选。
5. 拒绝命令行含 `-qsowned=` 的候选。
6. 只用 `PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE`（`0x1000 | 0x100000`）打开句柄，不申请终止权。
7. 用持有的句柄重新读取映像路径和创建时间，要求与 WMI 记录及用户选择的文件一致；创建时间不一致视为 PID 复用。
8. 用 `WaitForSingleObject(handle, 0)` 确认进程仍然存活；已退出的候选不返回附加对象。

上述 WMI 枚举、句柄打开和窗口绑定只发生在 `connect_game()` 建立连接时；连接后的 `attached_status()` 不重复 WMI 枚举、注入或 dump，只检查已持有的进程句柄是否仍存活。

任何一步失败都只报告本地可处理的原因，不猜测 PID、不重开进程、不终止进程。校验失败时已打开的句柄会关闭。

## 私有窗口绑定

附加模式需要一个由本地宿主生成的绑定 token，用于让原生命令通道绑定当前游戏的窗口消息入口。该 token 是宿主内部参数，不是 MCP 工具参数，模型不能填写或选择。绑定失败时应重新观察当前局并重新连接，不要用键盘/鼠标输入或控制台命令兜底。

## 常见问题

- **提示有多个候选进程。** 关闭不用的群星实例，或在本机启动参数中明确指定 PID；不要让模型猜。
- **提示已由另一个启动器管理。** 该游戏的命令行含 `-qsowned=`。改用创建它的持久战役连接；若要使用附加模式，先由用户正常退出该实例，再从 Steam 正常启动并读档。
- **提示路径或创建时间不匹配。** 可能是 PID 被复用、游戏已重启，或选错了安装目录。重新确认当前 `stellaris.exe` 路径，重启游戏后重新连接；不要复用旧 PID。
- **提示进程已退出 / `attached_status()` 显示 `state=stale`。** 附加对象和摘要已失效；重新启动游戏、读档、暂停，再调用 `connect_game(retry=true)`。不需要重启 MCP 服务。
- **连接返回 blocked、busy 或 failed。** 按返回的 phase 和 reason 修正当前前置条件；不要循环重试或启动探针。用户明确要求再连后才调用 `connect_game(retry=true)`。
- **连接后状态日期不对。** 先确认用户手动读的是哪份存档。附加模式不会自动载入检查点；如果用户已经换档，先调用 `disconnect_game(save=false)`（不要保存旧会话），再调用 `connect_game()` 连接当前局，不需要重启 MCP。
- **换档或游戏重启后需要重启 MCP 吗？** 不需要。同一个服务进程保留工具；按上面的 `disconnect_game(save=false)` → `connect_game()`（游戏已退出则 `connect_game(retry=true)`）完成重连。只有工具不可见、客户端配置变化或服务进程退出时才重连客户端或重启服务。
- **提示 `.qs_existing_controller.lock` 已存在。** 这是之前控制器遗留的控制器锁。先核实文件中的 `controller_pid`、`session` 和对应进程，确认没有活跃 owner 后由本机人工处理；不要删除不确定归属的锁。
- **非 Windows 平台。** 附加模式不支持；现有直连/原生路径只在 Windows 上验证。

## 安全边界

连接先核对进程身份和当前游戏状态，保留连接前的状态副本，再在当前用户目录创建本次会话的固定事件文件，并通过正常事件加载器加载。模型只能调用已有的精确工具；底层复用固定命令文件和普通原生命令，模型不能提供任意控制台脚本。连接过程不改 Mod 启用列表或游戏本体文件；保存时创建新的检查点，不覆盖原始存档。程序不发送键鼠输入，也不终止手动打开的游戏。当前局是否可写仍由连接校验、未决命令检查和真实后置条件决定，连接成功不代替玩法动作验证。

本机历史实机验收已在同一个手动启动的游戏进程完成两次独立 MCP 连接：读取 2200.01.01 的当前局、保存检查点、断开，再次连接；两轮日期、国家、战役身份与资源一致，游戏持续运行。验收只执行观察、暂停核验与保存，没有在用户当前局发出建设或舰队玩法命令；这些精确操作复用既有工具，其逐项证据边界见 [direct-control.md](direct-control.md)。

上述证据来自较早的附加连接流程。最新快速握手、未连接/已连接状态分流、`state=stale` 重连引导、连接取消清理和换档 `disconnect_game(save=false)` 流程目前只完成离线验证，尚未在用户重开群星后做实机复验；等用户重开群星、读档并暂停后，再按本文流程验证连接、换档、取消清理和退出后的 `state=stale` 行为。
