# 高效的日常任务流程

本页描述已实现的回执复用、精确短计划，以及显式授权时间的有限自然完成 runner。接口与离线验收不等于当前构建的实机提速测量。

## 每项任务按需准备

1. 同一连接和授权未变化时，不为每条指令重复 `attached_status`、`status`、`list_actions` 或 probe。
2. 已有相关的新鲜观察与暂停证明时直接选择实际对象；缺对象、状态过期、用户手动操作或时间推进后再 `request_observation`。
3. 写任务在暂停未知或正在运行时调用 `set_paused(true)`；已核验暂停则不重复发送。只读任务不为了优化流程改变游戏时间。
4. 同一数据版本已解析的建筑/设计定义复用。已知精确 ID 时直接读取必要定义，不重复“搜索名称→再查同一条定义→再读百科”。动态修正和实际成本仍依据当前游戏。
5. 多项精确操作仍串行执行；每项都经过现有的身份、授权、对象、付款、journal、真实后置条件及邮箱清理核验。

## 多项操作一次提交

单动作保持直接调用原工具的快路径。多个目标和约束明确时，Agent 可一次调用：

```json
{
  "request_id": "earth-two-labs-001",
  "steps": [
    {"kind": "queue_building", "planet_id": "3", "zone_id": "4", "building_id": "building_research_lab_1"},
    {"kind": "queue_building", "planet_id": "3", "zone_id": "4", "building_id": "building_research_lab_1"}
  ]
}
```

上述 ID 仅说明参数结构，实际必须来自当前局的新鲜观察与同版本定义。1–20 步，每步使用 `kind` 和原工具的精确参数；每次造一个项目，数量用重复步骤明确表示。支持科研选择、星球建设/升级/拆除/转换/取消、造舰、勘察/建站/殖民及既有舰队精确订单。`probe_connection` 与整体军事舰队 hold 不作为计划步骤。

- 已有已核验的暂停证明即可提交；需要暂停时可以显式把 `set_paused(true)` 作为首步。计划不自动暂停或解除暂停，不接受 `set_paused(false)`。
- 可选 `expected_snapshot` 绑定已有快照。后续步骤继续绑定上一项已核验快照；任何整份 digest 改变都保守停止，返回当前相关目标、队列和资源，供重新决策，不偷偷忽略变化。整 digest 不是完整的换档检测器。
- 服务端持有同一个 writer 排他权，逐步走原执行器。第二个写请求立即 `busy`，不把过时意图排队。同一舰队的未完成动作不能连发；用户明确允许的 `fleet_stop → 一个后续动作` 可用，但不自动插入 stop。
- `execute_plan` 正常完成直接返回最终记录；不强制先 prepare 或事后再 result。每步回执仍区分订单核验和自然完成。整体 `completed` 的 `completion_scope` 是 `dispatch_verification_only`，不能报告舰船、建筑已建成或船已抵达。
- 第一项拒绝则 `blocked/stale/busy`，已有成功部分则 `partial`；任何未能确认的当前步保留 `unverified`，后续 `skipped`。没有自动回滚。

### 断线、重试和取消

为一项计划使用稳定且唯一的 `request_id`。规范化 hash 包含步骤顺序、全部参数和可选快照条件；只忽略字典字段顺序。同 ID、同计划返回原来的 running/最终记录，不重复下单；同 ID 改计划则 `conflict`。丢回复时查 `task_result(request_id)`，不要换新 ID 重发。对于明确未提交的 busy/stale，需要重新决策并形成新任务，不能自动循环换 ID 重试。

任务库独立于存档解析和游戏连接。附加模式保存在固定 `state-dir/tasks.sqlite`，托管战役保存在固定 `campaign-dir/tasks.sqlite`，跨 session 重连仍可读取。普通 direct 模式默认与配置同目录；代码调用者使用轮换配置目录时须传固定 `task_store_path`。去重作用域是同一任务库，换机器或另建 state/campaign 目录不会自动共享。

`task_result` 和 `cancel_task` 无需游戏活着，也不等待游戏生命周期锁。取消只阻止未来步骤；当前提交先完成核验与清理，再结束。`cancel_requested` 不等于已经撤回游戏订单。MCP 客户端直接取消调用时同样保留当前执行器，清理结束前不释放 writer。

计划步骤在可能产生副作用之前先持久化 `executing`。原进程已死亡时，遗留 running 只返回 `interrupted`，该步未知、未开始项跳过，绝不自动接管或重放；进程身份无法核实时保持未知。原动作 journal 仍是核验与人工排查依据。任务返回已核验但整体回执尚未落盘的崩溃窗口也采取这一保守规则。

## 成功回执可直接使用

经过真实后置条件核验的写入结果现在附带：

- `dispatch_state=verified`。
- `goal_state=completed` 或 `pending`：暂停、清空订单、拆除/取消等已核验的立即效果，与需要自然时间的建造/抵达/研究分别表示。
- `context`：同一次已核验 after 的 snapshot/date、相关目标/分区、可用的精确队列、资源余额与变化。
- `paused` / `pause_known`：只有实际关联回执给出严格布尔值时才声称已知暂停。
- `resource_change_known` / `resource_change_unknown`：缺失、非数与不可表示的差值保持未知，不填零；余额变化不是实时价格报价。

这些字段由执行器已经取得的 before/after 生成，不额外请求 dump。拿到成功回执后，直接报告或继续下一项；不要为证明同一项成功再完整观察一次。需要新决策数据、实际自然完成或进一步检查时再观察。

`context` 是执行证据与下一步候选信息，不是长期有效的授权或内存状态租约。后续每项写入仍重新执行现有前置与后置条件核验；用户手动操作、换档、连接变化或时间推进后不能永远信任旧上下文。

## 不把未知当空

区分 `unknown`、`known_empty`、`known_value`。`known=false`、`queue_known=false`、`buildings_known=false` 或 null 不证明科研槽、队列或建筑槽为空；不能因观察缺失而推断可以下单。

## 下单和自然完成分开

- 建筑/舰船进入付费队列，不能报告已经建成。
- 勘察/移动/建站订单已核验，不能报告已经勘察完、抵达或建好。
- 殖民订单和殖民地引用出现，不能直接报告殖民发展全部完成。
- 队列为空，不证明此前每个项目都完成；也可能被取消或改变。

只有用户任务明确允许推进时间、给出上限并要求受支持的自然终点时，才使用下述有限 runner。

## 宿主等待自然终点

`submit_completion(request_id, intent, time_authorization, expected_snapshot=None)` 持久化任务并迅速返回。单个后台 worker 持有同一游戏 writer 排他权，复用现有 typed 执行器和付费、日志、后置条件、邮箱清理校验。等待和下一项固定选择留在宿主；竞争写请求立即 busy，不排队。

第一版 `intent` 是封闭的判别类型：

| kind | 必填目标 | 成功含义 |
| --- | --- | --- |
| `survey_body` | `fleet_id`, `planet_id`, `system_id` | 当前国家完整勘察台账证明该天体已勘察 |
| `survey_system` | `fleet_id`, `system_id`, `planet_ids` | 完整观察系统与冻结天体集合仍一致，集合内每个天体都已勘察；按提交顺序接续，不声称最短路线 |
| `enter_system` | `fleet_id`, `planet_id`, `system_id` | 目标天体锚定已访问系统；舰队进入该系统，授权的停止已核验；不保证抵达系统中心 |

目标必须来自当前局，ID 不接受数字强转、脚本、坐标或任意条件表达式。`survey_system` 冻结列表为 1–100 个唯一 ID，不能只传一部分后称“全系统”。已有已勘察项跳过；一艘船只在前一项已证明完成且订单确实空闲后接续，防止 replace 语义覆盖工作。起步需已经暂停、无保护暂停、目标舰队订单完整可知且为空；不暗中清除用户旧任务。

时间授权全部必填，无默认放行：

```json
{
  "allow_time_advance": true,
  "max_game_days": 180,
  "max_wall_seconds": 180,
  "final_pause": true
}
```

两个上限都为严格整数 1–3600。数字是结构示例，不是程序默认值或用户既有授权。程序不修改游戏速度，使用群星 360 天历计算日期差。采样和现有命令核验/清理会有延迟，上限不是精确 tick 闸门或硬实时中断。

完整请求示例（对象 ID 仅说明格式）：

```json
{
  "request_id": "survey-known-system-001",
  "intent": {"kind": "survey_system", "fleet_id": "1", "system_id": "247", "planet_ids": ["104", "105"]},
  "time_authorization": {"allow_time_advance": true, "max_game_days": 180, "max_wall_seconds": 180, "final_pause": true}
}
```

### 结果、取消与关闭

- `task_result(request_id, wait_seconds=0)` 同时读短计划和自然完成任务；可选 0–30 秒只读等待，不读取游戏、不抢 writer。取消这个等待只结束本次查询，不取消已授权的后台任务。
- 同一稳定 ID 的规范化 hash 包含目标、冻结顺序、时间授权和可选初始 snapshot。丢回复用同 ID 查原记录，换 ID 重发不安全；P1/P2 共用同一固定任务库，异任务同 ID 冲突。
- `cancel_task` 停止未来步骤和等待；当前提交仍先核验、disarm，再按授权核验最终暂停。已提交订单保留，不承诺撤销或退款。
- 正常关闭 MCP 时通知 worker 取消，抵抗 AnyIO 与显式 asyncio 取消并等待清理结束；强杀、崩溃和掉电无法保证暂停。原进程死亡后旧 ID 只读显示 interrupted，不接管、不重放。
- 记录包含 `phase`、`goal_state`、`last_evidence`、每步核验和 `final_pause.status`。`completed` 的 scope 为 `observed_goal_and_final_pause`；下单回执仍是 `pending`，只有明确状态终点才是 `satisfied`。不承诺由指定船导致结果的唯一因果归属。
- `cancelled/deadline/interrupted/needs_observation/unverified` 都须查看 `final_pause`，不能只看终态词就说“游戏已暂停”。无安全会话、日期倒退或 fresh observation 失败时不对可能的新世界发清理命令，暂停保持未知。

等待期间每次使用本次相关窗口消息返回的严格布尔 runtime，不使用缓存 pause 代替 fresh proof。未知勘察台账、冻结成员变化、战斗/归属变化、订单被替换或消失但目标未完成时停止；发现玩家/保护暂停不再次解暂停。移动只接受已经证明的动作结构和其在目标系统内剩余的合法 wait；陌生底层 order 结构保守停止，不推断其来源。

宿主后台任务不等于客户端自动通知或自动唤醒模型。短任务可用一次有界 `task_result` 收到终态；较长任务继续在宿主运行，可稍后查同 ID。不要让无事时的模型不断 dump/查百科，也不要承诺所有任务固定只有两次调用。

人工同进程换档必须断开后重新连接。日期倒退、配置/会话改变可检测；同一天、同战役回读仍不具备完整世代证据。该能力没有补齐矿站类型/归属、造舰 job→ship、正式殖民完成等观察链，这些任务继续只保证精确下单。

## 同舰队订单不能当作追加队列

现有固定订单可能清理并替换该舰队的已有订单。不能连续发 `move(A)`、`move(B)` 就称路线已排队；单天体勘察同理。旧脚本队列确需清理、且用户任务允许替换时，才显式 stop，不为流程简短而自动插入 stop。

外系统目标若普通勘察/建站命令已经支持寻路，不额外拆出多余的移动、等待抵达和停止步骤。科研船、工程船和殖民船应引用各自正确角色的舰队。

## 异常与接续

`blocked`、`unverified`、超时或未决出现时停止相关步骤，保留已完成部分并说明未知项，不自动重放或伪装回滚。客户端取消不证明已经提交的游戏订单被撤回。

其他自然终点须先补齐相应完整观察证据；短计划不能代替它。prepare 按需，result 仅用于恢复或进行中的任务，不能增加所有命令的固定仪式。
