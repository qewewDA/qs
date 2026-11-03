# 持久战役接续（scripts/serve_campaign.py）

`campaign.py` 管理一个持久的本地战役目录，`scripts/serve_campaign.py` 是日常生产入口。它保存首次导入的副本、不可覆盖的检查点、每次连接的独立会话和命令日志。再次接入使用最近一次完成发布的检查点，不会重新从最初的 2200 年存档开始。

本入口与一次性 probe 或普通 `direct-mcp` 不同：它自己创建并持有游戏进程，在已有精确 MCP 工具之上增加持久检查点、`campaign_status`、`checkpoint_campaign` 和 `save_and_close`。

## 适用构建与范围

- 实机验收构建：`Cygnus v4.5.2 (9776)`，Windows，Python 3.12，Frida 17.9.4。
- 原生适配器固定校验 `stellaris.exe` 的 SHA-256 `400df27c82ddc845aa9dce79bd468d81f18f060d299cd263e3afa93aef4f7a83`；不匹配就拒绝原生调用。
- 当前只支持玩家国家 `0`、单人、非铁人。明确多人或铁人存档会被拒绝；模式未知时需要本地显式确认。
- 内阁、传统和飞升选择继续由用户决定。固定版本、固定样本的结果不能外推为全部 DLC、全部起源/特殊建筑/舰种或未来版本均已支持。

2026-11-02 的完整回归为 **1628 passed, 3 skipped**（Windows JUnit），`ruff check src tests`、`git diff --check` 与 wheel/sdist 核对通过。验证记录不随源码分发。

## 安装与启动

在仓库根目录（Windows）：

```powershell
uv sync --frozen --extra mcp --extra native
uv run python scripts/serve_campaign.py --help
```

`mcp` 提供 MCP 服务端；`native` 安装固定版本 Frida（仅 Windows）。没有原生依赖时无法建立原生控制，但可运行离线解析和模拟。启动前先登录 Steam 客户端并正常退出手动打开的群星；启动器发现其他 `stellaris.exe` 会直接拒绝，不会接管或关闭它。

首次导入（`--initialize` 只用于第一次；原档只读）：

```powershell
uv run python scripts/serve_campaign.py `
  --campaign-dir .\campaign `
  --initialize "<你的存档>.sav" --country 0 --single-player --non-ironman `
  --game-dir "<Steam>\steamapps\common\Stellaris"
```

首次成功后删除 `--initialize`。后续每次 MCP 连接使用同一个 `--campaign-dir`，读取最近检查点：

```powershell
uv run python scripts/serve_campaign.py `
  --campaign-dir .\campaign `
  --game-dir "<Steam>\steamapps\common\Stellaris"
```

MCP 客户端配置示例（路径按本机替换，`cwd` 指向仓库根目录）：

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

需要额外精确接口时在同一命令加 `--extended`。`--status` 只检查 manifest、检查点及历史命令，不启动游戏。`--recover-last-checkpoint` 表示接受从最近完成检查点恢复，可能丢失上次未保存的内存进度；存在未决命令时仍然拒绝。

## 知识库（可选）

`--knowledge-bundle` 指向本地 `GameKnowledge` JSON。默认尝试仓库相对路径 `artifacts/qs-game-knowledge.json`，不存在则不启用知识工具；显式指定的路径不存在会直接报错。知识 JSON 体积较大，**不随仓库分发**，不要提交到 git。它包含本机游戏定义与本地化编译结果，只用于检索建筑、区划、科技、飞升、传统、内阁、舰船、经济、外交和危机等定义，不会授予任何操作权限。官方 Wiki 与灰机 Wiki 正文未导入（0 条），不能宣称已抓取全部正文。

## 日常 MCP 工具

启动器在已有精确控制工具上增加：

- `campaign_status()`：检查当前存档日期、检查点 hash、会话历史、未决命令与重载证据。
- `checkpoint_campaign()`：暂停、验证并保存新检查点，游戏继续保持暂停。
- `save_and_close()`：完成新检查点后，关闭本启动器创建的那个游戏进程。

这三个工具属于持久战役启动器。普通 `direct-mcp` 和一次性 probe 服务不会因为源码存在就具备保存工具。`campaign_status` 是只读；两个保存工具都没有模型可填写的路径、存档名或脚本参数。

推荐顺序：

1. 连接同一个 `campaign-dir`，调用 `campaign_status()` 查看当前检查点。
2. 调用 `request_observation()` 获取本次会话的新状态。首个成功的新观察会确认本次会话确实载入了当前检查点，记录 `reload_verified` 与玩法状态 hash；首次写入前也有相同门禁。身份、日期、同日玩法状态或会话标记不符时拒绝写入。
3. 执行具体操作，检查每项返回的真实后置条件与未决记录。
4. 中途调用 `checkpoint_campaign()` 留新检查点并保持暂停。
5. 结束调用 `save_and_close()`，确认返回 `saved_and_closed` 和 `owned_process_closed:true`。
6. 断开本次 MCP 连接；下次启动同一服务，加载 manifest 指向的新检查点。`save_and_close()` 关闭游戏后，本次服务不会自行再次启动游戏。

**日常结束先显式调用 `save_and_close()`，确认成功后再断开 MCP。** 断开时的自动保存只是尽力尝试：有些客户端会在数秒内终止或强杀服务，而真实保存可能更久，不能保证断开本身完成保存。

控制器有机会执行错误处理时会保留正在运行的游戏、记录 `save_failed`，并在 stderr 给出原因；若已通过暂停阶段，游戏保持暂停，连接或暂停本身失败则不保证暂停。强杀可能来不及写失败记录，下一次接入应按非正常结束检查。用户可通过游戏自身保存界面人工挽救。

`stop_bridge` 只停止接收控制命令，不保存也不退出。如果先设置 STOP，后续保存所需的暂停命令可能被挡住，启动器会如实报告失败并保留游戏。

## 生产退出与 `--reload-proof`

`save_and_close()` 的关闭分支要求当前格式/构建有真实重载证明。`--reload-proof` 在创建会话和启动游戏之前读取本地报告，校验 EXE hash、版本、战役、国家、两轮实际状态 hash、前一轮检查点与后一轮加载来源的关联，以及保存退出和清理结果。指定无效报告会直接拒绝启动；没有指定报告时可以生成解析检查点，但 `save_and_close()` 会拒绝关闭游戏（已保存的检查点保留，游戏保持暂停）。仓库不附带该报告或生成它的研究实验脚本；干净 clone 首次使用通常只能保存检查点后人工退出，或自行按相同口径完成两轮重载实验。

## 目录与信任边界

```text
campaign/
  manifest.json                  # 当前指针、身份、历史会话
  original.sav                   # 只首次导入，之后保留原样
  controller.lock                # 操作系统文件锁；进程崩溃后释放
  checkpoints/<随机ID>.sav        # 新建后不覆盖
  sessions/<会话ID>/
    direct.json                  # 此次连接的本地授权
    journal.sqlite               # 本次命令的持久状态
    native-worker-stderr.log
  userdir/                       # 独立游戏配置、固定桥接 Mod、dump
    save games/qs_campaign/<检查点ID>.sav
```

`manifest.json` 的 `campaign_id`、`country_id`、`game_version` 必须与每个检查点匹配。首次导入和后续检查点均要求选定国家是存档中唯一可识别的人类玩家；未知游戏模式依赖用户已提供的单人、非铁人确认。

模型不能选择检查点路径、控制台命令或脚本；路径只通过本地启动参数配置，MCP 保存工具没有路径参数。

## 保存流程

1. 正常暂停命令必须得到实际暂停验证。
2. 取得当前用户目录的命令锁，阻止其他 MCP 写入动作与保存交错。
3. 检查所有历史会话的日志；存在 `pending`、`dispatched` 或 `unverified` 命令时拒绝保存和退出。
4. 在游戏线程通过固定原生回调请求新的 dump；验证会话、战役、国家、版本、时间戳和暂停状态。
5. 复制同一代文件的不可变字节，并核对刚刚观察到的 SHA256；文件被游戏改写时拒绝保存。
6. 将原始 `gamestate` 字节和本次快照的 `meta` 打包成新的 ZIP `.sav`。dump 根级缺少游戏必需的玩家头像和旗帜；程序从选定国家的实际创始物种引用、物种头像和国家旗帜恢复，未知时拒绝建包。不重新序列化 `gamestate`。
7. 完整写入、刷新磁盘、重新打开包，确认解析、身份、日期、内容 hash；新检查点不可覆盖已有文件。
8. 原子替换 manifest 的当前检查点指针。发布失败时旧指针继续有效，未被引用的新文件不会在下次启动时被自动采纳。

检查点结果区分：

| 字段 | 证明的内容 |
| --- | --- |
| `parse_verified` | 完整字节、hash、存档结构和战役身份通过本地验证 |
| `reload_verified` | 后续另一个游戏进程实际载入此检查点，并观察到相同战役、国家、版本和日期以及新的会话标记 |
| `reload_gameplay_sha256` | 后续重载时，标准化玩法状态与被加载检查点一致；只忽略来源路径、桥接会话回执，以及 4.5.2 首次 `imported_save` 重载记录的 `reload_initialization` 空队列缓存 |

本地解析成功不等于游戏已实际重载。bootstrap 会增加会话标记，所以重载后的整份 dump hash 通常不同；原检查点的字节 hash 始终单独保留。

### 4.5.2 首次 imported_save 的初始化队列例外

普通 `packaged_dump` 检查点的重载仍按标准化 `gameplay_state` 严格比较，打包时要求 `gamestate` 字节精确一致；没有把所有观察字段视为逐字相等的豁免。

只有固定版本 `Cygnus v4.5.2` 的首次 `imported_save` 检查点第一次重载存在受控例外：原始 2200.01.01 存档可为未殖民天体省略 `build_queue` 引用，游戏首次载入后会补齐这些引用并新增空 planet 队列。验收样本为 56 个 body 的 `build_queue` 引用和 5 个空 planet 队列；这些队列是 `type=planet`、`items=[]`、`item_count=0`，且 `queue.disabled` 为 `None`（不是 `true`）。

`campaign.py` 的 `imported_queue_initialization` 只接受同时满足以下条件的差异：

- 变化的 body 在旧状态中无 `owner`、`colony` 或 `build_queue`，新状态只增加一个未占用的 `build_queue` 引用，其它字段不变；
- 新增队列的 owner 是当前国家、位置对应一个由当前国家控制且有轨道站的 body，并且队列形状与空队列完全一致；
- 所有新增队列的 `disabled` 必须严格为 `None`；未知或其它 `disabled` 值保留原样并导致例外不成立，不会被默认成零或 `true`；
- 从新状态移除这些新增引用/队列后，`gameplay_state(expected) == gameplay_state(observed)`；已有队列、已殖民星球队列引用和其它玩法字段仍严格比较。

接受例外时，检查点 manifest 记录 `reload_initialization`：`uncolonized_body_queues`（`planet_id`/`queue_id` 列表）和 `selected_empty_queue_ids`。后续 `packaged_dump` 检查点、其它游戏版本或非首次导入重载都不使用该例外。整份 dump 仍会因 bootstrap 会话标记而 hash 不同，因此不能表述为“所有观察字段逐字相等”；等价的只是移除已记录初始化队列后的标准化玩法状态。

## 游戏进程所有权

Windows 的 MCP stdio 客户端可以为整个子进程树设置关闭即终止的 Job Object。启动器用 WMI 的 `Win32_Process.Create` 创建游戏，随后取得并持有一个精确内核进程句柄，避免游戏继承 MCP 客户端的任务。

所有权成立需要 WMI 返回的映像、完整固定命令行、私有会话标记与创建时间一致；内核句柄再独立核对映像和创建时间。游戏还必须不属于 Windows Job Object，否则本次已验证身份的新进程会被关闭并拒绝使用。启动器不会修改客户端的 Job Object。

后续 `poll`、等待和关闭只使用这个最初取得的句柄。磁盘上的 PID 只用于记录，不用于重新寻找或关闭进程；PID 被系统复用时也不能转移所有权。释放句柄不会终止游戏。

如果 WMI 已创建游戏、但后续身份查询失败，程序报告可能有游戏仍在运行，并保留必要的目录重定向供本地处理；它不会猜测 PID 并关闭未经验证的进程。

## 崩溃恢复

操作系统锁随控制器进程结束释放，磁盘上的旧 lock 文件不会永久阻塞新进程；历史会话记录会保留。

- 非正常结束且没有未决命令：默认拒绝直接重新启动，要求本地显式添加 `--recover-last-checkpoint`，表示接受从最近完成检查点恢复；上次没有存盘的内存进度可能丢失。
- 存在未决命令：即使加恢复参数也拒绝启动。先检查对应存档、结果和命令日志，再通过既有 reconciliation 流程明确处置；程序不会重发这些命令。
- 发现 hash、国家、战役、版本不一致，或日期倒退：拒绝继续。
- 检查点落盘成功、manifest 更新失败：旧检查点仍是唯一启动来源，遗留文件只供人工检查。
- 崩溃后仍存活的旧游戏：新启动器拒绝启动，不会根据日志 PID 自动杀进程。只有本次创建并验证身份后持有的精确内核进程句柄可以被关闭。
- 崩溃发生在直接命令写入锁持有期间时，用户目录可能留下 `.qs_direct_controller.lock`。程序会阻止后续写入，要求本地检查锁及相关日志；`--recover-last-checkpoint` 不会擅自打破仍可能属于另一个写入者的锁。

## 验证范围与能力边界

**验证分层：**入队/下单成功只证明新状态出现正确项目或订单；自然完成需要另一个游戏日期的新状态；landing/殖民发展需要独立证据。默认授权只有 `select_research`、`queue_district`、`queue_building`、`set_paused` 和 `fleet_wait`/`fleet_orbit`。`--extended` 额外开放升级、拆除、转区、取消建设、普通/殖民造舰、勘察、建站、殖民、跨系统移动和停止；这些接口仍分别执行所有权、可观察性、暂停和真实后置条件检查。

本轮研究验收记录（不随仓库分发）中：

- 默认工具、扩展付费建设/拆除/转区/取消、科研与舰队精确订单有独立新状态证据；
- `queue_colony_ship` 只证明殖民船建造入队；`colonize_planet` 另有普通命令与 landing 证据，完整殖民发展 `full_colony_development_verified=false`；
- 舰队自然完成、前哨站和 landing 采用累计验证口径：原 partial trial 保持 `verified:false`，只有后续 resume 报告全部条件通过时才聚合；
- 检查点格式的两轮游戏重载、日常 MCP 两轮接续有独立报告；日常报告是隔离测试副本，不是用户持久战役；
- 断开时保存失败后保留游戏的故障路径、客户端强杀/断电保证仍需要单独证据；
- 生物舰、平台、运输船、新建设计、修改配装、完整战斗和外交没有在本轮直接开放。

`tests/test_campaign.py` 覆盖不可变检查点、字节与元数据一致性、恢复当前指针、未决命令拒绝重放、manifest 发布故障、身份错误、日期倒退、检查点篡改、独立进程文件锁，以及区分解析与重载证明。`tests/test_campaign_runtime.py` 覆盖首次写入前的重载门禁、同日错误状态拒绝、共享 MCP 连接、保存先于进程关闭、保存失败保留游戏和重定向、无格式证明拒绝关闭、未暂停快照拒绝保存，以及错误或不关联的格式报告拒绝。单元测试中的进程和原生快照替身不是实际游戏生命周期证据。

`owned_process.py` 的进程生命周期有 42 项确定性单元测试，覆盖命令/映像/时间身份、只释放句柄时不终止进程、拒绝继承任务、隐藏启动、超时和 Steam 未登录拒绝；固定 PowerShell 脚本通过语法解析。只读 Windows 探针核对 WMI 与内核 API 查询自身进程、规范映像一致和创建时间差在精度范围内。这些检查没有启动或附加游戏。
