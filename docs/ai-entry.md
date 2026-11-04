# 离线 AI 入口：一次接入，持续中文指挥

这是一个给新用户的双击入口：导出器把**一份**一次性接入提示词、当前 MCP 配置和使用说明写到新目录，浏览器打开其中的 群星AI入口.html 即可预览、复制和下载。页面是纯离线单文件，**不会连接 MCP、不会启动或操作游戏、不会注册 MCP、不会读写存档，也不会修改全局配置**。

使用方式是：在**同一个Agent对话**里先把提示词发一次；之后直接说中文任务，不需要重复选任务卡或复制模板。**新对话没有保留上下文，需要重新发一次。**

真正的游戏控制由 MCP 客户端完成：按本机配置接入服务后，Agent 才能调用游戏工具。推荐使用 [附加已运行模式](attach-existing.md)：你先正常打开群星、读入存档并暂停；MCP 服务启动后立即握手并暴露工具，不因没有游戏而阻塞，随后 Agent 才调用 `connect_game()` 连接当前局一次。连接成功返回精简初始快照，断开后游戏保持运行。入口生成器会按配置中的 `serve_existing_game.py` 导出这套提示词；持久战役启动器的配置则保留自动启动与检查点接续流程。

附加模式的重连语义以 [附加已运行模式](attach-existing.md) 为准：未连接时 `attached_status()` 不访问游戏，已连接时只做快速存活检查；游戏退出后状态变为 `connected=false`、`state=stale`，用户重开并暂停后用 `connect_game(retry=true)` 重连；用户已切换存档时先 `disconnect_game(save=false)` 再连接，不需要重启 MCP 服务。MCP 客户端取消连接时状态可显示 `state=cancelling`，清理完成后需重新调用 `connect_game(retry=true)`，不会留下后台连接。

## 生成入口

在仓库根目录，Windows PowerShell：

    .venv\Scripts\python.exe scripts\export_ai_entry.py --mcp-config <本机配置.json> --output-dir <新目录>

示例：

    .venv\Scripts\python.exe scripts\export_ai_entry.py --mcp-config .\mcp.local.json --output-dir .\artifacts\ai-entry

配置里有多个 MCP 服务器时，用 --server-name 选择要导出哪一个：

    .venv\Scripts\python.exe scripts\export_ai_entry.py --mcp-config <本机配置.json> --output-dir <新目录> --server-name stellaris-campaign

.\mcp.local.json 是本机配置占位路径，不要提交；.\artifacts\ai-entry 是已有忽略目录下的输出示例。

只有一个服务器时可以省略 --server-name；有多个服务器而未指定时，导出器应拒绝猜测并提示选择。--output-dir 不存在时应新建；不要写入已有文件的目录，避免覆盖用户内容。

导出目录通常包含：

- 群星AI入口.html：双击打开的入口页，单文件内联 CSS/JS，不依赖网络。
- mcp-connection.json：页面“复制 MCP 配置”使用的文本，默认文件名可由 payload 覆盖。
- 先看这里.md：同目录使用说明，页面底部有链接。
- 给Agent的一次性提示词.txt：页面“下载提示词 txt”和同目录链接使用的文本；文件名由 payload 的 filename 决定。

## 页面内容

页面只保留一个接入提示词，不显示任务选择卡，也不要求每次填写任务：

- 顶部说明：首次在客户端接入 MCP，向同一个 Agent 发一次提示词；之后直接中文指挥。新对话没有保留上下文时再发一次。
- 一次性接入提示词：显示 title、summary 和完整纯文本预览；主按钮是“复制一次性接入提示词”。
- 打法 / 偏好（可选）：一个输入框；内容会附加到提示词末尾，并标注为“我的持续偏好（仅此对话，之后直接下命令时也请遵守）”。
- 预览：只读文本域，显示最终要发送的纯文本；改动打法/偏好会立即更新。
- 下载：把当前预览（含打法/偏好）下载为 payload 指定的 txt 文件名。
- 同目录文本：可打开当前提示词的 txt 和使用说明 先看这里.md。
- MCP 连接配置：收在 details 里，默认折叠；显示服务器名、可选战役目录、配置文件名和配置原文，并提供次要的“复制 MCP 配置”按钮。
- 自然命令示例：页面列出 3–4 个“之后可以直接这样说”的句子，仅作对话示例，不会执行。
- 本地记忆：只把“打法 / 偏好”存入 localStorage，异常会容错；页面明确说明只保存在此浏览器，清空浏览器数据会丢失。
- 无障碍：输入有 label，复制结果通过 aria-live 状态区播报，按钮/输入框/链接有 focus-visible，prefers-reduced-motion 下关闭过渡和动画。

示例句子（页面同样只作为对话示例展示）：

- 看看现在帝国的情况，先别推进时间。
- 在地球补一个研究实验室，预算 600 矿物。
- 让第 3 舰队到目标星系待命，到了告诉我。
- 保存检查点，然后退出。

## 复制与降级

- 复制优先 navigator.clipboard.writeText；失败回退 document.execCommand('copy')；两者都失败时选中预览文本并提示“已选中文本，请按 Ctrl+C 手动复制”，不会假报成功。
- 页面文件名和链接按 basename 清洗，避免 javascript: 等 scheme。
- 下载使用 Blob + object URL，文件名取当前提示词的 filename；下载的是当前预览内容。

## 安全与边界

- 单文件、无外部字体、无 CDN、无图片、无统计脚本；断网可用。
- payload 以 application/json 嵌入并转义 <、>、&、U+2028、U+2029；用户输入只通过 textContent / value 写入，不用 innerHTML。
- 配置文本可能含本机绝对路径；导出目录是本地文件，不要连同配置一起分享到公开位置。
- 页面不读取存档、不向游戏发命令；它不能替代 MCP 工具，也不能证明任何游戏操作成功。
- 页面不注册 MCP；首次接线必须由 AI 客户端实际读取或配置 mcp-connection.json。

## 与 MCP 原生 prompt 的关系

配套的 MCP 服务通过 assistant_prompts.py 暴露**唯一**的 stellaris_agent 提示词（标题“一次性接入提示词”）。它的内容与页面导出的接入提示词一致：包含完整持续操作约定，要求先调用 `attached_status()`、再调用 `connect_game()` 一次，成功返回值已含精简初始快照；失败时报告 phase/reason 后停止，用户纠正后才用 `connect_game(retry=true)`。之后接受同一对话里的自然中文命令；内阁、传统、飞升由用户选择，AI 只可检索资料理解打法。

MCP 原生 prompt 只是服务端提供的用户消息模板：客户端连接后出现在提示词列表，选中后仍要由模型调用真实工具；它不会自动读档、下单或保存。客户端不支持 prompts/list 时，就使用导出的 HTML 复制入口。prompt 注册后需要重启服务并重新连接客户端才能看到；这只是 prompt 注册刷新，游戏换档或退出后的重连不需要重启 MCP 服务。

附加模式的 `state=stale`、连接取消、`connect_game(retry=true)` 和换档前 `disconnect_game(save=false)` 细节见 [attach-existing.md](attach-existing.md)；这些细节不要求重启 MCP 宿主。

## 主程序接口

提示词渲染入口固定为：

    render_prompt(*, playstyle="", campaign=True, existing_game=False, server_name="当前群星 MCP 服务", connection_note="") -> str

它只返回文字，不读快照、不下单；当前没有 playbooks 函数。existing_game=True 时使用附加模式的 attached_status → connect_game 一次性连接、checkpoint_game 和 disconnect_game，并说明连接成功已有精简初始快照、失败报告阶段后停止、用户纠正后才 retry=true、断开后游戏保持运行；否则 campaign=True 时使用持久战役的 checkpoint_campaign / save_and_close，campaign=False 时说明普通直连的保存能力边界。

## 不在本页范围

- 不安装依赖、不启动服务、不启动游戏、不注册 MCP。
- 页面负责整理提示词和配置。MCP 服务由 `serve_existing_game.py` 启动后先完成握手；连接当前手动打开的游戏由 Agent 调用 `connect_game()` 完成。自动启动模式由 `serve_campaign.py` 完成。
- 不宣称全局已安装、已游戏实测或所有 DLC/版本可用；实际使用仍受 Cygnus v4.5.2、国家 0、单人非铁人、EXE 哈希和本地授权的限制。
- 不替代 docs/campaign.md 和 docs/direct-control.md 中的检查点、暂停、后置条件和保存说明。

## 开发者接口

渲染器位于 src/stellaris_autopilot/assistant_portal.py，接口固定为：

    render_portal(payload: dict) -> str

它是纯函数：不做文件、网络、进程或游戏访问，不依赖第三方库。payload 字段：

- title：页面标题。
- server_name：MCP 服务器名。
- campaign_dir：可选，战役目录；为空时不显示。
- config_file：配置文本文件名，默认 mcp-connection.json。
- config_text：MCP 配置原文，页面只展示/复制。
- guide_file：同目录说明文件名，默认 先看这里.md。
- prompts：列表；本入口只使用第一项，通常只有一项，字段为 name、title、summary、text、filename。name 固定为 stellaris_agent，title 为“一次性接入提示词”，filename 为“给Agent的一次性提示词.txt”。

导出器和 MCP prompt 注册由主程序的 assistant_entry.py、assistant_prompts.py 与 scripts/export_ai_entry.py 负责；本渲染器不读配置、不推断路径、不启动任何东西。
