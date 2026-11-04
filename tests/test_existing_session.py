import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from stellaris_autopilot import existing_session
from stellaris_autopilot.assistant_prompts import render_prompt
from stellaris_autopilot.campaign import CampaignError
from stellaris_autopilot.direct_mcp import build_direct_server
from stellaris_autopilot.existing_session import ExistingGameSession


class _Process:
    def __init__(self):
        self.exit_code = None

    def poll(self):
        return self.exit_code


class _Connection:
    def observe(self):
        return {"status": "observed"}


class _Runtime:
    def __init__(self, root: Path):
        self.closed = False
        self.process = _Process()
        self.config_path = root / "direct.json"
        self.native = object()
        self.connection = _Connection()
        self.initial_summary = {
            "country_id": "0",
            "date": "2200.01.01",
            "campaign_id": "campaign-a",
            "snapshot": "digest-a",
            "paused": True,
        }
        self.disconnect_calls = []

    def checkpoint_game(self):
        return {"status": "checkpoint_saved"}

    def disconnect_game(self, *, save=True):
        self.disconnect_calls.append(save)
        return {"status": "disconnected", "saved": save, "game_closed": False}

    def close(self):
        self.closed = True


def _session(tmp_path, **kwargs):
    return ExistingGameSession(tmp_path / "game", tmp_path / "state", **kwargs)


def _connector(runtime, calls=None, *, fail_once=False, progress_phase=None):
    state = {"fail": fail_once}

    def connect(executable, state_root, *, user_dir, pid, extended, timeout_seconds, progress):
        calls.append(
            {
                "executable": executable,
                "state_root": state_root,
                "user_dir": user_dir,
                "pid": pid,
                "extended": extended,
                "timeout_seconds": timeout_seconds,
            }
        )
        if state["fail"]:
            state["fail"] = False
            raise CampaignError("no paused save")
        if progress_phase is not None:
            progress(progress_phase)
        return runtime

    return connect


def test_constructor_and_status_do_not_connect_or_load_knowledge(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(existing_session, "resolve_user_dir", lambda *args: calls.append("resolve"))
    monkeypatch.setattr(existing_session, "connect_existing", lambda *args, **kwargs: calls.append("connect"))
    monkeypatch.setattr(
        existing_session.GameKnowledge, "load", staticmethod(lambda path: calls.append("knowledge"))
    )

    session = _session(tmp_path, knowledge_bundle=tmp_path / "knowledge.json")
    status = session.status()

    assert calls == []
    assert status["state"] == "disconnected"
    assert status["mcp_ready"] is True
    assert status["connected"] is False
    assert status["knowledge_loaded"] is False


def test_failed_connect_is_cached_and_retry_requires_explicit_flag(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    calls = []
    monkeypatch.setattr(existing_session, "resolve_user_dir", lambda game_dir, user_dir: tmp_path / "user")
    monkeypatch.setattr(existing_session, "connect_existing", _connector(runtime, calls, fail_once=True))
    session = _session(tmp_path)

    first = session.connect_game()

    assert first["status"] == "blocked"
    assert first["state"] == "failed"
    assert first["last_error"]["reason"] == "no paused save"
    assert session._runtime is None
    assert len(calls) == 1

    second = session.connect_game()

    assert second["status"] == "blocked"
    assert second["cached_failure"] is True
    assert len(calls) == 1

    third = session.connect_game(retry=True)

    assert third["status"] == "connected"
    assert third["summary"] == runtime.initial_summary
    assert third["initial_observation_verified"] is True
    assert len(calls) == 2


def test_successful_connect_passes_timeout_and_progress_and_publishes_summary(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    seen = []
    monkeypatch.setattr(
        existing_session, "resolve_user_dir", lambda game_dir, user_dir: tmp_path / "resolved"
    )
    monkeypatch.setattr(
        existing_session,
        "connect_existing",
        _connector(runtime, seen, progress_phase="verify_running_process"),
    )
    session = _session(tmp_path, user_dir=tmp_path / "explicit", pid=123, extended=True, timeout_seconds=30)

    result = session.connect_game()

    assert result["status"] == "connected"
    assert result["summary"] == runtime.initial_summary
    assert result["initial_observation_verified"] is True
    assert session._runtime is runtime
    assert len(seen) == 1
    assert seen[0]["user_dir"] == tmp_path / "resolved"
    assert seen[0]["pid"] == 123
    assert seen[0]["extended"] is True
    assert seen[0]["timeout_seconds"] == 30


def test_repeated_connect_does_not_reattach(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    calls = []
    monkeypatch.setattr(existing_session, "resolve_user_dir", lambda game_dir, user_dir: tmp_path / "user")
    monkeypatch.setattr(existing_session, "connect_existing", _connector(runtime, calls))
    session = _session(tmp_path)

    assert session.connect_game()["status"] == "connected"
    second = session.connect_game()

    assert second["status"] == "connected"
    assert second["already_connected"] is True
    assert session._runtime is runtime
    assert len(calls) == 1


def test_concurrent_connect_returns_busy_without_blocking_status(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def connector(executable, state_root, *, user_dir, pid, extended, timeout_seconds, progress):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return runtime

    monkeypatch.setattr(existing_session, "resolve_user_dir", lambda game_dir, user_dir: tmp_path / "user")
    monkeypatch.setattr(existing_session, "connect_existing", connector)
    session = _session(tmp_path)
    first = []

    def run_first():
        first.append(session.connect_game())

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(5)

    busy = session.connect_game()
    assert busy["status"] == "busy"
    assert busy["state"] == "connecting"
    assert busy["retry_automatically"] is False

    status_result = []
    status_thread = threading.Thread(target=lambda: status_result.append(session.status()))
    status_thread.start()
    status_thread.join(timeout=0.5)
    assert not status_thread.is_alive()
    assert status_result[0]["state"] == "connecting"
    assert status_result[0]["connected"] is False

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first[0]["status"] == "connected"
    assert len(calls) == 1


def test_forward_proxy_holds_operation_lock_until_disconnect_can_run(tmp_path):
    runtime = _Runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    disconnect_started = threading.Event()
    order = []

    def observe():
        entered.set()
        assert release.wait(5)
        order.append("observe")
        return {"status": "observed"}

    def disconnect_game(*, save=True):
        disconnect_started.set()
        order.append("disconnect")
        return {"status": "disconnected", "saved": save, "game_closed": False}

    runtime.connection.observe = observe
    runtime.disconnect_game = disconnect_game
    session = _session(tmp_path)
    session._runtime = runtime
    session._state = "connected"
    observed = []
    disconnected = []

    forward_thread = threading.Thread(target=lambda: observed.append(session.observe()))
    forward_thread.start()
    assert entered.wait(5)

    disconnect_thread = threading.Thread(
        target=lambda: disconnected.append(session.disconnect_game(save=False))
    )
    disconnect_thread.start()
    time.sleep(0.2)
    assert not disconnect_started.is_set()
    assert disconnect_thread.is_alive()

    release.set()
    forward_thread.join(timeout=5)
    disconnect_thread.join(timeout=5)
    assert not forward_thread.is_alive()
    assert not disconnect_thread.is_alive()
    assert order == ["observe", "disconnect"]
    assert observed[0]["status"] == "observed"
    assert disconnected[0]["status"] == "disconnected"


def test_context_returns_current_config_and_native(tmp_path, monkeypatch):
    config = object()
    runtime = _Runtime(tmp_path)
    monkeypatch.setattr(existing_session, "load_direct_config", lambda path: config)
    session = _session(tmp_path)
    session._runtime = runtime

    assert session.context() == (config, runtime.native)


def test_knowledge_is_loaded_lazily_once(tmp_path, monkeypatch):
    calls = []
    knowledge = object()
    monkeypatch.setattr(
        existing_session.GameKnowledge,
        "load",
        staticmethod(lambda path: calls.append(Path(path)) or knowledge),
    )
    session = _session(tmp_path, knowledge_bundle=tmp_path / "knowledge.json")

    assert session.status()["knowledge_loaded"] is False
    assert session.knowledge() is knowledge
    assert session.knowledge() is knowledge
    assert calls == [tmp_path / "knowledge.json"]
    assert session.status()["knowledge_loaded"] is True


def test_disconnect_save_failure_retains_runtime(tmp_path):
    runtime = _Runtime(tmp_path)

    def fail_disconnect(*, save=True):
        raise OSError("disk full")

    runtime.disconnect_game = fail_disconnect
    session = _session(tmp_path)
    session._runtime = runtime
    session._state = "connected"

    with pytest.raises(OSError, match="disk full"):
        session.disconnect_game(save=True)

    assert session._runtime is runtime
    assert session.status()["connected"] is True


def test_provider_configured_server_keeps_existing_prompt(tmp_path):
    session = _session(tmp_path)
    server = build_direct_server(
        tmp_path / "direct.json",
        connection=session,
        context_provider=session.context,
        knowledge_provider=session.knowledge,
        existing_game=True,
    )

    result = asyncio.run(server.get_prompt("stellaris_agent"))
    text = result.messages[0].content.text
    assert text == render_prompt(campaign=False, existing_game=True)
    tools = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"search_game_knowledge", "get_game_definition", "get_wiki_reference", "knowledge_status"} <= tools


def test_stdio_handshake_lists_connect_game_without_game_or_knowledge(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "serve_existing_game.py"
    state_dir = tmp_path / "state"
    missing_game = tmp_path / "missing-game"
    missing_knowledge = tmp_path / "missing-knowledge.json"

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                str(script),
                "--state-dir",
                str(state_dir),
                "--game-dir",
                str(missing_game),
                "--knowledge-bundle",
                str(missing_knowledge),
            ],
            cwd=str(repo),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "attached_status" in names
                assert "connect_game" in names
                result = await client.call_tool("attached_status", {})
                assert not result.isError
                payload = result.structuredContent or json.loads(result.content[0].text)
                assert payload["state"] == "disconnected"
                assert payload["mcp_ready"] is True
                assert payload["connected"] is False
                assert payload["next_tool"] == "connect_game"

    started = time.perf_counter()
    asyncio.run(asyncio.wait_for(run(), timeout=20))
    elapsed = time.perf_counter() - started
    print(f"stdio handshake seconds: {elapsed:.3f}")
    assert elapsed < 10
