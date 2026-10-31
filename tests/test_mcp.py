import asyncio
import json
import sys

import pytest

pytest.importorskip("mcp")

from stellaris_autopilot.journal import Journal
from stellaris_autopilot.mcp_server import _PreviewJournal, build_server
from stellaris_autopilot.windows import WindowsDriver


@pytest.fixture
def config_path(tmp_path):
    save_path = tmp_path / "private empire.sav"
    save_path.write_text('''date="2200.01.01" version="4.test"
player={country=0} country={0={name="My Empire" resources={alloys=50}
research={physics="tech_old"}}}''', encoding="utf-8")
    action = {
        "id": "select_physics", "description": "Select calibrated physics research", "category": "research",
        "steps": [{"kind": "key", "key": "f4"}],
        "expected": [{"path": "research.physics", "op": "eq", "value": "tech_new"}],
    }
    unapproved = {**action, "id": "select_other"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"save_path": save_path.name, "actions": [action, unapproved],
                                "allowed_actions": ["select_physics"], "state_dir": "private-state"}))
    return path


def invoke(server, name, arguments=None):
    return asyncio.run(server._tool_manager.call_tool(name, arguments or {}))


def test_tools_have_readonly_annotations_and_no_execute_tool(config_path):
    server = build_server(config_path)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {"observe_empire", "list_actions", "plan_action", "status"}
    for tool in tools:
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
    plan_schema = next(tool for tool in tools if tool.name == "plan_action").inputSchema
    assert set(plan_schema["properties"]) == {"action_id", "reason"}


def test_observe_does_not_return_source_path(config_path):
    result = invoke(build_server(config_path), "observe_empire")
    assert result["state"]["name"] == "My Empire"
    assert len(result["snapshot"]) == 64
    assert "source" not in result["state"]["metadata"]
    assert str(config_path.parent) not in json.dumps(result)


def test_only_locally_authorized_actions_are_exposed_and_config_reloads(config_path):
    server = build_server(config_path)
    assert [action["id"] for action in invoke(server, "list_actions")] == ["select_physics"]
    raw = json.loads(config_path.read_text())
    raw["allowed_actions"] = []
    config_path.write_text(json.dumps(raw))
    assert invoke(server, "list_actions") == []


def test_plan_is_dry_run_without_windows_or_journal_write(config_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("MCP must not create a Windows execution driver")
    monkeypatch.setattr(WindowsDriver, "__init__", forbidden)
    server = build_server(config_path)
    result = invoke(server, "plan_action", {"action_id": "select_physics", "reason": "Prioritize science"})
    assert result["status"] == "dry_run"
    assert result["execution"] == "No game input sent"
    assert not (config_path.parent / "private-state").exists()
    assert invoke(server, "plan_action", {"action_id": "select_other", "reason": "No authorization"})["status"] == "blocked"


def test_status_reads_existing_journal_without_private_snapshot(config_path):
    state_dir = config_path.parent / "private-state"
    journal = Journal(state_dir / "journal.sqlite")
    journal.claim("digest", "select_physics", {"country_id": "0", "metadata": {"source": "C:/private/save.sav"}})
    journal.close()
    result = invoke(build_server(config_path), "status")
    assert result["unresolved"][0]["status"] == "pending"
    assert "before_json" not in result["operations"][0]
    assert "private/save" not in json.dumps(result)


def test_unresolved_dispatch_blocks_even_preview(config_path):
    state_dir = config_path.parent / "private-state"
    journal = Journal(state_dir / "journal.sqlite")
    journal.claim("digest", "select_physics", {"country_id": "0"})
    journal.close()
    result = invoke(build_server(config_path), "plan_action", {"action_id": "select_physics", "reason": "Science"})
    assert result["status"] == "blocked"
    assert "Unresolved" in result["reason"]


def test_missing_save_error_has_no_absolute_path(config_path):
    server = build_server(config_path)
    (config_path.parent / "private empire.sav").unlink()
    with pytest.raises(Exception) as caught:
        invoke(server, "observe_empire")
    assert str(config_path.parent) not in str(caught.value)


def test_status_missing_journal_does_not_create_files(config_path):
    result = invoke(build_server(config_path), "status")
    assert result["operations"] == result["unresolved"] == []
    assert not (config_path.parent / "private-state").exists()


def test_closed_wal_journal_read_does_not_create_original_sidecars(config_path):
    state_dir = config_path.parent / "private-state"
    path = state_dir / "journal.sqlite"
    journal = Journal(path)
    journal.claim("digest", "select_physics", {"country_id": "0"})
    journal.close()
    before = {entry.name: entry.read_bytes() for entry in state_dir.iterdir()}
    result = invoke(build_server(config_path), "status")
    assert result["unresolved"][0]["status"] == "pending"
    assert {entry.name: entry.read_bytes() for entry in state_dir.iterdir()} == before


def test_active_wal_is_included_without_changing_original_files(config_path):
    state_dir = config_path.parent / "private-state"
    journal = Journal(state_dir / "journal.sqlite")
    try:
        journal.claim("digest", "select_physics", {"country_id": "0"})
        before = {entry.name: entry.read_bytes() for entry in state_dir.iterdir()}
        result = invoke(build_server(config_path), "status")
        assert result["unresolved"][0]["status"] == "pending"
        assert {entry.name: entry.read_bytes() for entry in state_dir.iterdir()} == before
    finally:
        journal.close()


def test_changing_journal_is_rejected_instead_of_reading_mixed_copy(config_path, monkeypatch):
    state_dir = config_path.parent / "private-state"
    journal = Journal(state_dir / "journal.sqlite")
    try:
        journal.claim("digest", "select_physics", {"country_id": "0"})
        original_copy = _PreviewJournal._copy
        writes = 0
        def copy_then_change(self, destination, signatures):
            nonlocal writes
            original_copy(self, destination, signatures)
            writes += 1
            journal.update(1, "unverified", f"concurrent write {writes}")
        monkeypatch.setattr(_PreviewJournal, "_copy", copy_then_change)
        with pytest.raises(ValueError, match="changed during each snapshot"):
            _PreviewJournal(state_dir / "journal.sqlite").records()
        assert writes == 3
    finally:
        journal.close()


def test_oversized_journal_is_rejected_without_sqlite_open(config_path, monkeypatch):
    state_dir = config_path.parent / "private-state"
    journal = Journal(state_dir / "journal.sqlite")
    journal.close()
    monkeypatch.setattr(_PreviewJournal, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="64 MiB"):
        _PreviewJournal(state_dir / "journal.sqlite").records()


def test_actual_stdio_session_initializes_and_calls_tools(config_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def session():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "stellaris_autopilot.cli", "mcp", "--config", str(config_path)],
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                initialization = await client.initialize()
                assert initialization.serverInfo.name == "Stellaris AI Autopilot"
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "observe_empire", "list_actions", "plan_action", "status"
                }
                result = await client.call_tool("plan_action", {
                    "action_id": "select_physics", "reason": "Read-only stdio preview"
                })
                assert not result.isError
                payload = result.structuredContent or json.loads(result.content[0].text)
                assert payload["status"] == "dry_run"

    async def bounded_session():
        await asyncio.wait_for(session(), timeout=15)

    asyncio.run(bounded_session())
    assert not (config_path.parent / "private-state").exists()
