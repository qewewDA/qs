# 资料研究、许可证及版本边界

研究日期：2026-10-31。以下是本次实际检查的公开仓库快照和可访问性记录。仓库 README 中的“支持版本”是上游声明，不是本项目对当前 Stellaris 的实机验证。本项目独立实现自己的解析器、执行器和 Mod；未直接复制以下四个项目的应用源码、脚本或资源。

## 指定文档的实际访问结果

| 地址 | 本次结果 |
| --- | --- |
| https://stellaris.paradoxwikis.com/Modding | 代理 CONNECT 返回 403 Forbidden，正文不可读 |
| https://stellaris.paradoxwikis.com/Modding_tutorial | 同上 |
| https://stellaris.paradoxwikis.com/Event_modding | 同上 |
| https://stellaris.paradoxwikis.com/Effects | 同上 |
| https://stellaris.paradoxwikis.com/Scopes | 同上 |
| https://main--pdxdoc-next.netlify.app | 同上 |
| https://qunxing.huijiwiki.com/wiki/Mod | 同上 |

三站域名不在当前环境的受限访问规则中。已尝试通过当前平台代理正常读取，没有绕过策略。尚不能声称读过这些页面的正文。需要在环境设置中允许 `stellaris.paradoxwikis.com`、`main--pdxdoc-next.netlify.app`、`qunxing.huijiwiki.com` 后重新请求；即使域名获准，站点仍可能另有反爬或访问限制。

为继续开发，使用下列允许访问的 GitHub 仓库和 CWTools 的脚本定义交叉核查。它们不能替代在用户游戏版本上的 `error.log` 检查与行为验收。

## 四个指定项目

| 项目及检查的 HEAD | 许可证证据 | 复用结论 |
| --- | --- | --- |
| [megidragon/stellaris-mcp-interface](https://github.com/megidragon/stellaris-mcp-interface/tree/13de108638fc52ac37837e5785789ff28f7b82d6) `13de108638fc52ac37837e5785789ff28f7b82d6` | `package.json` 声明 MIT；检查的根目录未见独立 LICENSE 正文 | 仅参考接口与结构；不复制代码，也不把 npm 元数据当成已完成全部授权审查 |
| [3v4d3/GalacticConclave](https://github.com/3v4d3/GalacticConclave/tree/9a9717fafe4838ef5a895f0282a7e3a8586a0aa6) `9a9717fafe4838ef5a895f0282a7e3a8586a0aa6` | 根 `LICENSE`：MIT，Copyright (c) 2026 dave | MIT 可与本项目 MIT 兼容；若以后复制实质性内容须保留上游版权和许可。当前独立实现 |
| [Dfintz/Stellaris_Overmind](https://github.com/Dfintz/Stellaris_Overmind/tree/7b28c585202e8c25c8fb3e9b3e7bd53e73028c00) `7b28c585202e8c25c8fb3e9b3e7bd53e73028c00` | 根 `LICENSE`：MIT，Copyright (c) 2026 Stellaris Overmind contributors | MIT 兼容，同样需在实质性复用时保留声明；当前独立实现 |
| [gitmaan/stellaris-companion](https://github.com/gitmaan/stellaris-companion/tree/4aada5dc33264f9293ebdc7cd90dd40d29987b40) `4aada5dc33264f9293ebdc7cd90dd40d29987b40` | 根 `LICENSE`：MIT，Copyright (c) 2026 Stellaris Companion Contributors | MIT 兼容；其嵌套组件及依赖需要分别按相应许可证审查，根许可不覆盖所有第三方内容。当前独立实现 |

### stellaris-mcp-interface

实际阅读 `README.md`、`package.json`、`src/index.ts`、`src/commands/command-writer.ts` 及 `mod/` 相关文件。README 声明设计目标为 **Cetus 4.3.1**。目录包括 TypeScript stdio MCP 服务、`src/parser/`（jomini/ZIP 存档解析）、日志监听、命令文件写入，以及 `mod/common/on_actions`、`mod/common/scripted_effects`、`mod/events`。

它以月脉冲国家事件写 `game.log`，MCP 暴露状态查询和命令排队工具。`CommandWriter.flush()` 的实质是写出 `ai_commands.txt`，返回提示玩家在控制台执行 `run ai_commands.txt`。**文件写入不代表游戏执行，更不代表操作生效。** 在源码里看到的资源/研究指令及任意效果串不符合本项目的普通游戏操作边界，因此不直接暴露给模型。

`queueBuildOnPlanet()` 依赖动态命名的 event target；`queueMoveFleet()` 中的数字 ID 和作用域表达式也不能凭 README 就认定当前版本可用。本项目不直接照搬这些未经实机验证的操作模板。

### GalacticConclave

实际阅读 `README.md`、`LICENSE` 以及 `save_parser.py`、`game_io.py`、`llm_client.py`、`main.py`。这是 Python 桌面外交交互应用，README 标识 **v0.6.0**；`save_parser.py` 自述测试对象为 Phoenix v4.x。其顶层主要是这些 Python 模块，无 TypeScript MCP 服务。

存档路径是 ZIP 中的 `gamestate`，通过文本/括号解析提取国家数据。游戏执行路径是 LLM/外交响应 → APPLY → 命令文件 → 控制台 `run` 注入。Windows 窗口激活、控制台键和非铁人条件会影响注入。它示范了真实输入传输，但不是完整玩家帝国的经济、科技、军事自治系统；控制台注入成功也仍需新状态验证。借鉴点是明确区分读取、提案、执行与 UI 配置；不直接复用可自由拼接的模型命令。

### Stellaris_Overmind

实际阅读 `README.md`、`LICENSE`、`docs/EXECUTOR_SPEC.md`、`engine/bridge.py`、自动执行脚本及 Mod 的 events/scripted_effects/personalities/static_modifiers。执行器规格文档标识 **4.4.6**，而部分 personality 注释仍引用 **4.3/4.3.4**，说明版本声明本身也有漂移。

`docs/EXECUTOR_SPEC.md` 明确规定：**玩家模式只是建议，绝不执行玩家操作**；AI 模式通过 `event overmind.101`–`.111 <country_id>` 控制默认 AI 国家。这些事件守卫是 `is_ai = yes`，并非玩家帝国控制器。

例如 BUILD_FLEET、IMPROVE_ECONOMY 和 DEFEND 的宏效果主要是设 flag、切换 personality、应用临时属性 modifier；不是直接验证过的造舰队列、建设排队或舰队移动。设研究速度/舰队容量加成也会改变游戏平衡。本项目既不把它们当作普通玩家建设动作，也不把调整原生 AI 权重称为已经控制人类玩家。可借鉴的是固定动作目录、每国家作用域、执行文件白名单和审计；这不包含复制代码。

### stellaris-companion

实际检查 `README.md`、`LICENSE`、目录及玩家状态提取/MCP 相关源码。目录包括 `stellaris_save_extractor/`、`stellaris_companion/`、Rust `stellaris-parser/`、Electron UI、`docs/local-mcp.md` 和 `scripts/smoke_mcp_stdio.py`。

它提供较丰富的存档领域提取、玩家上下文和本地 MCP 接口，属于观察与顾问系统，可借鉴玩家国家定位、版本化状态提取、MCP stdio 与 UI/后端分离。Rust/jomini 输出存在字符串数值转换及重复键语义问题，不能将 PDX 文本简单当作 JSON 或用正则全局抓取一个 `country` 字段。本项目不假定它自带玩家帝国的执行闭环。

## PDXScript 接口的补充核查

参考 [cwtools/cwtools-stellaris-config](https://github.com/cwtools/cwtools-stellaris-config/tree/99147efe8072b331005b50536762dcc0b8573224)，HEAD `99147efe8072b331005b50536762dcc0b8573224`，提交日期 2026-06-11。只阅读定义作为语法参考，未复制其配置文件。

- `config/Stellaris on_actions.csv`：`on_monthly_pulse_country`、`on_game_start_country` 的 root/this 是 country。
- `config/effects.cwt`：`set_country_flag`、`remove_country_flag`、`log`、`save_event_target_as`、`clear_orders`；`set_policy = { policy = ... option = ... cooldown = yes }`。
- `config/triggers.cwt`：`can_set_policy = { policy = ... option = ... }`、`is_multiplayer`、`is_ironman`、`is_mobile`、`is_in_combat`、`is_ship_class`。
- `config/links.cwt`：`capital_scope` 将 country 切到 planet。
- `config/common/fleet_actions.cwt`：`queue_actions`、`move_to`（目标为天体坐标作用域）。

据此独立编写的 `saa.20` 将未在战斗的玩家移动军事舰队的命令清空并排入到首都的移动命令。它是**待实机验证**的真实动作模板，不是原生 AI 权重开关。`saa_DEFEND_DISPATCHED` 只说明分发代码运行；新存档中的订单/位置才是检查结果的依据。没有匹配舰队也可能产生日志。

指定项目和本次检查的 CWTools 文件均没有提供可验证的 `human_ai` 接口证据，因此本项目不自动执行它，也不承诺它可以把玩家帝国交给原生 AI 管理。

## 对本项目的实现约束

1. `.sav` 是观察输入，按 ZIP/PDX 实际格式解析，不覆盖存档。玩家身份必须由可信配置与存档中的玩家映射核对。
2. 日志单向输出；Mod 无通用 HTTP/文件读取桥。日志文本、国家名、存档文本都不是模型可执行指令。
3. LLM 输出固定 schema 的动作 ID 和参数；本地验证所有权、授权、前提和状态新鲜度，再交给固定执行器或用户校准的 UI 宏。
4. 脚本事件写日志、输入发送成功、端口/PID 存在，都不是游戏操作完成。等待操作后的新存档并验证领域状态；缺失字段和超时应返回未验证。
5. 建设、科技、外交各类 UI 会随语言、缩放、分辨率和版本变化。未经校准和新状态验证的宏不能称为开箱即用。
6. 云端测试能证明解析、授权、工具调用和执行器控制流；只有真实 Windows 客户端才能验收前台窗口、控制台键、PDXScript 加载、舰队排队与抵达。
