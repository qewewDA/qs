"""Export a local, offline AI entry without launching MCP or changing a campaign."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile

from .assistant_portal import render_portal
from .assistant_prompts import AGENT_PLAYBOOK, render_prompt


def _select_server(config_path: Path, server_name: str | None) -> tuple[str, dict]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("Cannot read a local MCP JSON configuration") from None
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise ValueError("Configuration must contain a nonempty mcpServers object")
    if server_name is None:
        if len(servers) != 1:
            raise ValueError("Choose one server with --server-name")
        server_name = next(iter(servers))
    if not isinstance(server_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", server_name):
        raise ValueError("Server name must be a simple MCP identifier")
    entry = servers.get(server_name)
    if not isinstance(entry, dict):
        raise ValueError("The selected MCP server does not exist")
    # This local bridge needs no credentials. Do not copy unrelated servers,
    # environments or authentication material into a shareable prompt bundle.
    if set(entry) - {"command", "args", "cwd"}:
        raise ValueError("Export supports only a local command, args and optional cwd; use a dedicated config")
    if (not isinstance(entry.get("command"), str) or not entry["command"].strip()
            or not isinstance(entry.get("args"), list)
            or not all(isinstance(arg, str) for arg in entry["args"])
            or ("cwd" in entry and (not isinstance(entry["cwd"], str) or not entry["cwd"].strip()))):
        raise ValueError("Invalid local MCP command configuration")
    if any(arg.split("=", 1)[0] in {"--initialize", "--recover-last-checkpoint"} for arg in entry["args"]):
        raise ValueError("Daily entry must not initialize or recover a campaign; use its ordinary reconnect config")
    return server_name, entry


def _campaign_directory(args: list[str]) -> str:
    for index, arg in enumerate(args):
        if arg.startswith("--campaign-dir="):
            value = arg.split("=", 1)[1]
        elif arg == "--campaign-dir":
            value = args[index + 1] if index + 1 < len(args) else ""
        else:
            continue
        if not value or value.startswith("--"):
            raise ValueError("The campaign directory argument needs a value")
        return value
    return ""


def export_entry(config_path: Path, output_dir: Path, *, server_name: str | None = None) -> Path:
    """Create a new bundle directory; never overwrite existing or user-edited files."""
    config_path, output_dir = Path(config_path).resolve(), Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("Output directory already exists; choose a new directory to preserve your edits")
    output_dir = output_dir.resolve()
    name, entry = _select_server(config_path, server_name)
    campaign_dir = _campaign_directory(entry["args"])
    existing_game = any(arg.replace("\\", "/").rsplit("/", 1)[-1] == "serve_existing_game.py"
                        for arg in entry["args"])
    connection_description = ("当前已经载入的游戏存档" if existing_game else
                              campaign_dir or "普通直连，无持久战役保存工具")
    config_text = json.dumps({"mcpServers": {name: entry}}, ensure_ascii=False, indent=2) + "\n"
    note = (
        f"本机连接配置：{output_dir / 'mcp-connection.json'}。\n"
        "这份配置只用于当前电脑；若客户端支持本机文件，可读取它完成接入。"
        "客户端不支持动态接入时，请说明它需要的配置步骤，不声称已经连接。"
    )
    if campaign_dir:
        note += f"\n这次接续的持久战役目录：{campaign_dir}；从最近完成的检查点继续。"
    if existing_game:
        note += "\n连接模式：我先正常打开群星、读档并暂停；MCP 直接连接当前这一局。"
    book = AGENT_PLAYBOOK
    cards = [{"name": book.name, "title": book.title, "summary": book.summary, "filename": book.filename,
              "text": render_prompt(server_name=name, campaign=bool(campaign_dir), existing_game=existing_game,
                                    connection_note=note)}]
    guide = (
        "# 群星 AI 入口\n\n"
        "1. 双击 `群星AI入口.html`。首次使用，把“连接配置”交给支持本地 stdio MCP 的 AI 客户端。\n"
        "2. 点“复制一次性接入提示词”，发给同一个 Agent 对话一次。之后直接用中文下令。\n"
        "3. 比如说“地球补两个研究所”“科研船去勘察”“保存退出”，由 Agent 选择工具并核验结果。\n\n"
        "页面完全离线，只整理和复制文字，不会连接、启动或控制游戏。"
        "当前客户端必须实际提供这些 MCP 工具；仅粘贴提示词不能建立连接。\n\n"
        + ("先按平常的方式启动群星，读入存档并保持暂停，再连接 MCP。"
           "这次连接读取当前已经打开的局，断开后游戏保持运行。" if existing_game else
           "启动前先在 Steam 登录，并正常保存退出手动打开的群星。"
           "连接配置中的启动器将自己启动受管理的游戏，读取同一战役的最新检查点。")
        + "同一局同时只接一个控制客户端。\n\n"
        + ("MCP 服务先完成握手并显示工具，再由 Agent 调用 attached_status 和 connect_game 各一次。"
           "接入成功已含精简初始状态，无需重复完整存档；失败就报告阶段与原因，停止重试。"
           "游戏重开并暂停后，旧连接为 stale 时调用 connect_game(retry=true) 重新核对当前局。"
           "在同一进程内换档时先 disconnect_game(save=false)，再连接，无需重启 MCP。"
           "工具不可见时请重连客户端，不要让 Agent 翻源码、编写探针或循环启动服务。\n\n"
           if existing_game else "")
        +
        "## 连接与使用\n\n"
        f"- MCP 名称：`{name}`\n"
        f"- 本机配置：`{output_dir / 'mcp-connection.json'}`\n"
        f"- 来源配置：`{config_path}`\n"
        f"- 连接对象：`{connection_description}`\n\n"
        "支持 MCP prompts 的客户端可以选用一次 `stellaris_agent`；没有提示词菜单时，复制页面或 txt。\n"
        "同一个对话后续不用再贴。换成没有保留这些约定的新对话或新 Agent 时，再发一次。\n\n"
        + "\n".join(f"- `{card['name']}`：{card['title']}" for card in cards)
        + "\n\n内阁、传统、飞升继续由你选择；AI 通过已有选择和你的打法理解建设与舰船任务。"
        "首次接入先读状态、保持暂停、等待下一条命令。"
        "请在本次任务里写清目标、数量、预算和是否允许时间推进；可以只用中文。\n\n"
        "示例：检查地球岗位和收支，列出科技发展需要的建设，先保持暂停。\n\n"
        "## 有限等待与接续\n\n"
        "只下单用原工具；多项明确下单可一次 execute_plan。要求自然完成时，"
        "目前支持单天体勘察、完整已观察系统的冻结天体列表勘察，以及进入指定系统后停船。"
        "例如：让这艘空闲科学船把这个已知系统勘察完，允许推进最多180个游戏日、"
        "最多180秒现实时间，结束暂停。这里的上限仅是示例，请按当前任务确定。\n\n"
        "Agent使用 submit_completion 一次提交，宿主等待并接续，"
        "task_result 可只读等待最多30秒，cancel_task 停止后续步骤并核验结束暂停；"
        "已下达的游戏订单不撤销。返回running时不用反复观察存档或换ID重发。"
        "后台任务完成不保证客户端自动唤醒AI；之后可直接说‘查一下刚才的任务’。"
        "玩家暂停、换档或订单改变时流程会停止。矿站、造舰、完整殖民的最终完成证据尚未开放给该runner。\n\n"
        "## 保存\n\n"
        + ("说“保存”时调用 checkpoint_game，在游戏的 save games/qs_mcp 中另存新检查点。"
           "说“保存退出”或“断开”时调用 disconnect_game(save=true)，确认 saved=true、"
           "game_closed=false；AI 连接结束后游戏仍可继续手动玩。直接关闭客户端只会释放连接，"
           "请先显式保存。切换存档后重新连接。\n\n" if existing_game else
           "中途保存创建新的检查点并保留游戏；保存退出需确认 `saved_and_closed` 和 "
           "`owned_process_closed: true`。缺少当前构建/战役的有效重载证明时，自动关闭会被拒绝；"
           "可保留检查点后人工退出。强制断开 AI 客户端不保证保存。\n\n" if campaign_dir else
           "当前是普通直连入口，没有持久战役的保存退出工具；请通过游戏自身正常保存退出。\n\n")
        + "这份入口不扩大本地授权，不添加游戏能力，不会修改原始存档或全局 MCP 设置。"
        "包含本机路径，适合本机自用；换电脑请重新生成，分享前检查路径。\n"
    )
    payload = {"title": "群星 AI 指挥台", "server_name": name, "campaign_dir": campaign_dir,
               "config_file": "mcp-connection.json", "config_text": config_text,
               "guide_file": "先看这里.md", "prompts": cards}
    files = {"群星AI入口.html": render_portal(payload), "mcp-connection.json": config_text,
             "先看这里.md": guide, **{card["filename"]: card["text"] for card in cards}}
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # Stage all generated files together. A failure never leaves a partial entry.
    with tempfile.TemporaryDirectory(prefix=".qs-entry-", dir=output_dir.parent) as staging:
        staged = Path(staging) / "bundle"
        staged.mkdir()
        for filename, content in files.items():
            (staged / filename).write_text(content, encoding="utf-8")
        staged.rename(output_dir)
    return output_dir / "群星AI入口.html"
