# 本次交付的验证记录

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
