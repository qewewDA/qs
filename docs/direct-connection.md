# 实验性无键鼠直连

当前的精确舰队订单、正常科研、付费区划与暂停/恢复见 [MCP 逐项指挥](direct-control.md)。该文包含真实 MCP 结果、原生后端的精确版本限制和授权方式；[全局资料库](game-knowledge.md)涵盖飞升/传统等知识与数值检索。下文说明文件通道的底层机制。

本机在 **Cygnus v4.5.2 (9776)**、单人、非铁人、独立用户目录和存档副本中验证了以下引擎入口。游戏内 Mod 与 Python/MCP 之间通过引擎自带的命令文件通信；这不是官方网络 API。

```text
MCP 固定动作 → 原子替换 qs_direct_inbox.txt
                   ↓
commands_at_date.txt 每游戏日 run 该文件
                   ↓
受 session/operation 标记保护的固定效果 → 状态导出 → 身份与真实订单验证
```

## 已验证的底层行为

1. `on_single_player_save_game_load` 可以在暂停读档时自动执行隐藏事件。实测日志分别记录读档回调、人类国家和暂停状态。
2. `-igc=run qs_direct_bootstrap.txt` 可以在进入游戏后执行预设批处理；批处理内的 `game_speed 0`、状态导出和 `game_paused false` 已运行。无需无人值守性能测试模式。
3. 安装目录 `tools/commands_at_date.txt` 是厂商提供的说明和示例。复制至独立用户目录后，调度器按游戏日期执行命令；实测游戏暂停时不消费该队列。
4. 调度器中的 `run qs_direct_inbox.txt` 使用 VFS 相对文件名。本机实测 2200.01.02 读取第一版文件，外部原子替换后在 2200.01.03 读取第二版，证明运行中会重新读取内容。
5. `debug_dumpgamestate qs_direct` 产生完整 UTF-8 文本状态，实际路径为 `<user_dir>/save games/dump_qs_direct.sav.txt`。本机样本约 19 MB。
6. `event <固定国家事件ID> 0` 可以准确绑定玩家国家；该事件为选中国家设置唯一 session flag。之后 console `effect` 的当前玩家根 scope 必须包含该 flag 才能执行请求。`country:0` 在此版本被实际拒绝，不能用作效果 scope。
7. 普通舰队脚本 `clear_orders = yes`、`queue_actions = { wait = 30 }` 已实际执行。新状态中的军事舰队包含 `actions = { wait = { time = 30 } }`，重复调用的操作守卫只执行一次。

指定 Modding Wiki 的直接请求与 API 请求本轮均返回客户端挑战页面，未读到正文。以上行为依据本地厂商文件、安装脚本及实机结果，不能推广为其他游戏版本的兼容承诺。

## MCP 服务与配置

原 `mcp --config config.local.json` 仍是只读服务。新入口使用独立配置：

```powershell
.\.venv\Scripts\python.exe -m stellaris_autopilot direct-mcp --config C:\path\config.direct.local.json
```

参考配置见 [examples/direct-config.json](../examples/direct-config.json)。必须替换用户目录、状态目录、从实际状态读到的国家/战役/版本，以及本次启动唯一的 32 位小写十六进制 session token。配置文件本身不会启动游戏、安装 Mod 或建立 session；先准备并验证隔离启动环境。

| 工具 | 行为 |
| --- | --- |
| `observe_empire` | 只读最近已有快照，返回 freshness；不发布命令 |
| `request_observation` | 原生模式可在暂停时导出新状态；文件模式等待预定日期 |
| `list_actions` | 列出本地明确授权的固定动作 |
| `probe_connection` | 写入本次操作专有回执，验证新状态；没有玩法订单 |
| `execute_action` | 只接收固定 `action_id`，目前支持连接探针和军事舰队等待 |
| `issue_order` | 指定观察到的舰队/天体 ID，执行等待、环绕、跨系统移动或停止，按本地授权开放 |
| `select_research` | 使用已验证的原生后端选择当前候选科技，保持正常研究过程 |
| `queue_district` | 通过正常原生队列付费建造指定区划，验证实际付款与未完成项目 |
| `queue_building` | 在指定的自有星球分区正常付费新建建筑，核对殖民地与物理星球的关联 |
| `set_paused` | 通过正常暂停命令设置并独立验证状态，尊重游戏的保护暂停 |
| `search_game_knowledge` / `get_game_definition` | 只读全局机制、条件与基础数值，支持中文检索 |
| `status` | 读取授权、STOP 和未决操作摘要 |
| `stop_bridge` | 创建 STOP 并清空本服务的指令文件；游戏时间继续 |

`allowed_actions`、`allowed_orders` 和 `allowed_native_actions` 默认空。`hold_military_fleets` 必须单独授权：它清除自有、可移动、未交战军事舰队的旧脚本动作和普通命令，并排入 30 游戏日的等待。精确舰队、普通付费建设和造舰等扩展见 [逐项指挥](direct-control.md)，日常保存和重连见 [战役指南](campaign.md)。全面外交仍未提供直接接口。

舰队等待的验证需要完整的自有舰队表、可辨识舰种与移动状态，以及本机已观测的 inactive combat 结构；未知或活动战斗状态会在发布前阻断。新快照必须包含相同目标集合、操作回执，并且每支目标舰队的动作队列仅有有效的等待订单。日志 ACK 或 receipt 本身不构成舰队动作成功。

## 启动环境与生命周期

本机提供隔离测试启动器，准备独立用户目录、原始存档副本、最小 Mod 和 bootstrap 文件，再用 `--continuelastsave -igc=run qs_direct_bootstrap.txt` 启动。原生模式对启动器自己创建的游戏进程接入私有注册消息通道，已经实测在暂停时执行。文件模式的日期调度需预先覆盖需要运行的时间范围。

日常接续使用单独的持久战役启动器与 `stellaris-campaign-extended.mcp.json`；一次性测试配置每次新建副本，不能代替持久配置。日常启动器每次载入最近成功保存的检查点，第一次观察或操作前核对完整标准化玩法状态。

- `userdir.txt` 可将游戏导向独立目录。测试文件是无 BOM、无引号的 ASCII 绝对路径；原用户目录未安装桥接 Mod。测试结束要关闭测试进程并移除临时重定向文件，之后才恢复原版启动。
- 本机测试使用完整的原始 Windows 命令行。引擎启动参数对连字符的解析有历史限制；不要把带连字符的长路径直接拼进 `userdir=` 或 `igc=`。文件名使用固定下划线形式。
- **状态导出会改写 `continue_game.json`**，将继续目标变成文本 dump。后续自动启动前必须重新固定到真正的 `.sav`；测试已实际遇到并修复这个启动问题。
- `igc` 完成首次启动和国家绑定。原生模式可在暂停时读取、派单并恢复；纯日期文件模式无法在暂停时自行唤醒。原生命令必须在本次有效的窗口消息回调中派发暂停请求，禁止用日期回退将游戏暂停。
- 重新连接、读旧存档、切换战役或更换 session 前必须先停用并清空旧文件，重新建立唯一国家绑定。游戏内去重 flag 会随旧存档回滚，不能将它宣称为跨回滚的 exactly-once 保证。
- 共享用户目录的写入由独占锁串行化；进程崩溃留下的锁和未决 SQLite 记录要求本地核查，不会自动重发。现有文件的 ownership header 同时绑定 session 和国家，拒绝覆盖未知文件或另一 session 的队列。
- `stop_bridge` 清空队列不会撤销已在游戏中排入的舰队订单。游戏内活动的指令读取与文件替换之间仍存在一个消费竞态，必须保留未决结果。

快照读取在复制字节阶段检查文件身份、大小、mtime 与 Windows ChangeTime；随后解析这份不可变字节。解析期间下一天的 dump 更新不会污染这次快照，也不会把混合写入当作有效状态。
