"""Tests for the one-time onboarding prompt and offline entry exporter."""

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from stellaris_autopilot import direct_mcp
from stellaris_autopilot.assistant_entry import export_entry
from stellaris_autopilot.assistant_portal import render_portal
from stellaris_autopilot.assistant_prompts import render_prompt


SESSION = "a" * 32


@pytest.fixture
def direct_config(tmp_path):
    user_dir = tmp_path / "game"
    user_dir.mkdir()
    path = tmp_path / "direct.json"
    path.write_text(
        json.dumps(
            {
                "user_dir": str(user_dir),
                "state_dir": str(tmp_path / "state"),
                "country_id": "0",
                "campaign_id": "campaign",
                "verified_game_version": "4.test",
                "session_token": SESSION,
                "allowed_actions": ["probe_connection", "hold_military_fleets"],
                "allowed_orders": ["fleet_wait", "fleet_orbit"],
                "allowed_native_actions": [
                    "select_research",
                    "queue_district",
                    "queue_building",
                    "set_paused",
                ],
                "single_player_confirmed": True,
                "non_ironman_confirmed": True,
                "verification_timeout_seconds": 0.02,
                "poll_seconds": 0.001,
            }
        ),
        encoding="utf-8",
    )
    return path


def prompt_text(server, arguments=None):
    result = asyncio.run(server.get_prompt("stellaris_agent", arguments or {}))
    return "\n".join(message.content.text for message in result.messages)


def write_config(tmp_path, servers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False), encoding="utf-8")
    return path


def valid_server(command="python"):
    return {"command": command, "args": ["server.py", "--campaign-dir", "C:/战役 目录"]}


def test_native_prompts_only_expose_one_agent_prompt_and_do_not_read_or_order(direct_config, monkeypatch):
    monkeypatch.setattr(
        direct_mcp, "DirectObserver", lambda *args, **kwargs: pytest.fail("prompt rendering read a snapshot")
    )
    monkeypatch.setattr(
        direct_mcp,
        "publish_order",
        lambda *args, **kwargs: pytest.fail("prompt rendering published an order"),
    )
    monkeypatch.setattr(
        direct_mcp,
        "atomic_publish",
        lambda *args, **kwargs: pytest.fail("prompt rendering published a command"),
    )

    server = direct_mcp.build_direct_server(direct_config, persistent_campaign=True)
    listed = asyncio.run(server.list_prompts())
    assert [item.name for item in listed] == ["stellaris_agent"]
    assert listed[0].title == "一次性接入提示词"

    text = prompt_text(server, {"playstyle": "种田 800 矿物，不主动开战"})
    assert "种田 800 矿物，不主动开战" in text
    assert "checkpoint_campaign" in text
    assert "save_and_close" in text
    assert "从最近完成的检查点继续" in text
    assert not (Path(direct_config).parent / "state").exists()


def test_plain_direct_prompt_does_not_promise_saving(direct_config):
    server = direct_mcp.build_direct_server(direct_config, persistent_campaign=False)
    listed = asyncio.run(server.list_prompts())
    assert [item.name for item in listed] == ["stellaris_agent"]
    text = prompt_text(server)
    assert "没有 checkpoint_campaign / save_and_close" in text
    assert "不能承诺持久保存或自动退出" in text
    assert "保存退出" in text


def test_render_prompt_campaign_flag_changes_only_save_claims():
    campaign = render_prompt(campaign=True)
    plain = render_prompt(campaign=False)
    assert "checkpoint_campaign" in campaign and "save_and_close" in campaign
    assert "从最近完成的检查点继续" in campaign
    assert "没有 checkpoint_campaign / save_and_close" in plain
    assert "不能承诺持久保存或自动退出" in plain


def test_existing_prompt_routes_sdk_wrapped_tools_without_reflection_or_extra_rounds():
    text = render_prompt(existing_game=True, server_name="stellaris-campaign")
    assert 'tools["mcp__stellaris-campaign__attached_status"]({})' in text
    assert 'tools["mcp__stellaris-campaign__connect_game"]({})' in text
    assert 's?.state === "disconnected"' in text
    assert "ToolArgsMap" in text
    assert "直接 return 回执" in text
    assert "不能代替 SDK 声明" in text
    assert "不读状态目录或 SQLite" in text
    assert "重建知识库不能恢复原生调用" in text
    assert "run_code" not in render_prompt(existing_game=False)


@pytest.mark.parametrize(("payload", "connects"), [
    ({"state": "disconnected", "mcp_ready": True}, 1),
    ({"structuredContent": {"state": "disconnected", "mcp_ready": True},
      "content": [{"type": "text", "text": '{"state":"failed"}'}]}, 1),
    ({"content": [{"type": "text", "text": '{"state":"disconnected","mcp_ready":true}'}]}, 1),
    ({"structuredContent": {"state": "failed", "mcp_ready": True}}, 0),
    ({"structuredContent": {"state": "stale", "mcp_ready": True}}, 0),
    ({"structuredContent": {"state": "connecting", "mcp_ready": True}}, 0),
    ({"structuredContent": {"state": "connected", "mcp_ready": True}}, 0),
    ({"state": "disconnected", "mcp_ready": False}, 0),
    ({"content": [{"type": "text", "text": "unparseable"}]}, 0),
    ({"isError": True, "structuredContent": {"state": "disconnected", "mcp_ready": True}}, 0),
    (None, 0),
], ids=["direct", "structured", "json-text", "failed", "stale", "connecting", "connected",
        "not-ready", "bad-text", "error-envelope", "null"])
def test_sdk_connect_program_executes_against_realistic_mcp_envelopes(payload, connects):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the client-side onboarding example")
    text = render_prompt(existing_game=True, server_name="stellaris-campaign")
    program = re.search(r"```javascript\n(.*?)\n```", text, re.DOTALL).group(1)
    script = (
        f"const payload = {json.dumps(payload)};\n"
        "const calls = [];\n"
        "const tools = {\n"
        '  "mcp__stellaris-campaign__attached_status": async args => { calls.push(["status", args]); return payload; },\n'
        '  "mcp__stellaris-campaign__connect_game": async args => { calls.push(["connect", args]); '
        'return {status:"connected"}; }\n'
        "};\n"
        "const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;\n"
        f"const fn = new AsyncFunction('tools', {json.dumps(program)});\n"
        "await fn(tools);\n"
        "console.log(JSON.stringify(calls));\n"
    )
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                               text=True, encoding="utf-8", timeout=10, check=True)
    calls = json.loads(completed.stdout)
    assert calls == [["status", {}]] + [["connect", {}]] * connects


def test_export_entry_exports_only_selected_server_and_does_not_modify_config(tmp_path):
    servers = {
        "stellaris-campaign": valid_server(),
        "other-server": {"command": "other", "args": ["other.py"]},
    }
    config_path = write_config(tmp_path, servers)
    before = config_path.read_bytes()
    output = tmp_path / "中文 路径" / "导出 目录"

    result = export_entry(config_path, output, server_name="stellaris-campaign")

    assert result == output.resolve() / "群星AI入口.html"
    assert result.is_absolute()
    assert result.is_file()
    assert config_path.read_bytes() == before
    assert {path.name for path in output.iterdir()} == {
        "群星AI入口.html",
        "mcp-connection.json",
        "先看这里.md",
        "给Agent的一次性提示词.txt",
    }
    exported = json.loads((output / "mcp-connection.json").read_text(encoding="utf-8"))
    assert set(exported["mcpServers"]) == {"stellaris-campaign"}
    assert exported["mcpServers"]["stellaris-campaign"] == servers["stellaris-campaign"]
    html = result.read_text(encoding="utf-8")
    assert "stellaris-campaign" in html
    assert "other-server" not in html
    assert "群星 AI 指挥台" in html
    assert "stellaris_agent" in html
    assert "给Agent的一次性提示词.txt" in html


def test_plain_direct_export_guide_does_not_promise_automatic_save(tmp_path):
    config_path = write_config(tmp_path, {"stellaris-campaign": {"command": "python", "args": ["server.py"]}})
    output = tmp_path / "plain-bundle"
    result = export_entry(config_path, output)
    guide = (output / "先看这里.md").read_text(encoding="utf-8")
    prompt = (output / "给Agent的一次性提示词.txt").read_text(encoding="utf-8")
    assert result.is_absolute()
    assert "没有持久战役的保存退出工具" in guide
    assert "自动关闭" not in guide
    assert "没有 checkpoint_campaign / save_and_close" in prompt
    assert "不能承诺持久保存或自动退出" in prompt


def test_export_entry_refuses_to_overwrite_existing_directory(tmp_path):
    config_path = write_config(tmp_path, {"stellaris-campaign": valid_server()})
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        export_entry(config_path, output)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert {path.name for path in output.iterdir()} == {"keep.txt"}


@pytest.mark.parametrize(
    ("servers", "server_name", "message"),
    [
        ({"one": valid_server(), "two": valid_server()}, None, "Choose one server"),
        ({"one": valid_server()}, "missing", "does not exist"),
        (
            {"one": {"command": "python", "args": ["server.py"], "env": {"TOKEN": "x"}}},
            None,
            "Export supports only",
        ),
        ({"one": {"command": "python", "args": ["--initialize", "save.sav"]}}, None, "must not initialize"),
        (
            {"one": {"command": "python", "args": ["--recover-last-checkpoint"]}},
            None,
            "must not initialize or recover",
        ),
        (
            {"one": {"command": "python", "args": ["--campaign-dir"]}},
            None,
            "campaign directory argument needs a value",
        ),
        ({"one": {"command": "python", "args": "server.py"}}, None, "Invalid local MCP command"),
    ],
)
def test_export_entry_rejects_invalid_configs_without_partial_directory(
    tmp_path, servers, server_name, message
):
    config_path = write_config(tmp_path, servers)
    output = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match=message):
        export_entry(config_path, output, server_name=server_name)

    assert not output.exists()
    assert list(tmp_path.glob(".qs-entry-*")) == []


def test_render_portal_keeps_user_text_inside_escaped_json(tmp_path):
    evil = "</script><img src=x onerror=alert(1)>"
    html_text = "<b>不应成为标签</b>"
    separators = chr(0x2028) + "中间" + chr(0x2029)
    payload = {
        "title": "标题 " + html_text,
        "server_name": "stellaris-campaign",
        "config_text": "cfg " + evil,
        "prompts": [
            {
                "name": "stellaris_agent",
                "title": "一次性接入提示词",
                "summary": "摘要 " + html_text,
                "text": "前 " + evil + separators + " 后",
                "filename": "给Agent的一次性提示词.txt",
            }
        ],
    }

    html = render_portal(payload)
    assert "</script><img" not in html
    assert "<img" not in html
    assert "<b>" not in html
    assert html.count("</script>") == 2
    assert "首次在客户端接入 MCP，向同一个 Agent 发一次提示词" in html
    assert html.index('id="copy-prompt"') < html.index('id="playstyle"')
    assert html.index('id="playstyle"') < html.index('id="preview"')
    assert "离线提示词入口，游戏操作由已连接的 MCP 执行" in html

    match = re.search(r'<script type="application/json" id="portal-data">(.*?)</script>', html, re.S)
    assert match is not None
    raw = match.group(1)
    assert "</script>" not in raw
    assert "<img" not in raw
    assert chr(0x2028) not in raw
    assert chr(0x2029) not in raw
    assert "\\u003c" in raw

    parsed = json.loads(raw)
    assert parsed["config_text"] == payload["config_text"]
    assert parsed["prompts"][0]["text"] == payload["prompts"][0]["text"]
    assert parsed["prompts"][0]["summary"] == payload["prompts"][0]["summary"]
    assert parsed["title"] == payload["title"]


def test_exported_prompt_text_matches_html_payload(tmp_path):
    config_path = write_config(tmp_path, {"stellaris-campaign": valid_server()})
    output = tmp_path / "bundle"
    result = export_entry(config_path, output)
    prompt_file = (output / "给Agent的一次性提示词.txt").read_text(encoding="utf-8")
    html = result.read_text(encoding="utf-8")
    match = re.search(r'<script type="application/json" id="portal-data">(.*?)</script>', html, re.S)
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["prompts"][0]["text"] == prompt_file
