"""Offline tests for attached-existing-game runtime lifecycle and prompts.

The fake process/native/connection objects never touch a real game.  File
operations are confined to pytest temporary directories.
"""
import asyncio
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

import stellaris_autopilot.attached_runtime as module
from stellaris_autopilot.assistant_prompts import render_prompt
from stellaris_autopilot.attached_runtime import AttachedRuntime, connect_existing
from stellaris_autopilot.campaign import CampaignError
from stellaris_autopilot.direct import DirectConfig, load_direct_config, validate_direct_config
from stellaris_autopilot.direct_mcp import build_direct_server
from stellaris_autopilot.direct_orders import render_target_events
from stellaris_autopilot.observe import Observation


TOKEN = "a" * 32


class Process:
    """A game process whose Python close only releases our observation handle."""

    def __init__(self):
        self.pid = 8240
        self.live = True
        self.closes = 0

    def poll(self):
        return None if self.live else 1

    def close(self):
        self.closes += 1


class Native:
    def __init__(self):
        self.calls = []
        self.closed = 0
        self.event_token = None
        self.inbox_token = None

    def bind_existing_window(self, token):
        self.calls.append(("bind", token))

    def load_attached_events(self, token):
        self.event_token = token
        self.calls.append(("load_events", token))

    def run_inbox(self, token):
        self.inbox_token = token
        self.calls.append(("run_inbox", token))

    def request_dump(self, token):
        self.calls.append(("request_dump", token))

    def result(self, token):
        self.calls.append(("result", token))
        if token == self.event_token:
            return {"status": "events_load_returned", "kind": "load_attached_events",
                    "token": token, "country_id": "0", "execution_source": "window_message"}
        if token == self.inbox_token:
            return {"status": "inbox_executed", "kind": "run_inbox",
                    "token": token, "country_id": "0", "execution_source": "window_message"}
        return {"status": "dump_requested", "kind": "request_observation",
                "token": token, "country_id": "0", "execution_source": "window_message"}

    def close(self):
        self.closed += 1
        self.calls.append(("close",))


class Connection:
    def __init__(self, observation="observed", pause="pause_verified"):
        self.observation_status = observation
        self.pause_status = pause
        self.calls = []

    def request_observation(self):
        self.calls.append(("request_observation",))
        return {"status": self.observation_status}

    def set_paused(self, paused):
        self.calls.append(("set_paused", paused))
        return {"status": self.pause_status}


class Journal:
    def __init__(self, unresolved=None):
        self.items = list(unresolved or [])

    def unresolved(self):
        return list(self.items)


def make_runtime(tmp_path, *, event_namespace="qs_direct_targets", with_connection=True):
    user_dir = tmp_path / "user_dir"
    (user_dir / "save games").mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    process, native = Process(), Native()
    runtime = AttachedRuntime(process, native, user_dir, state_dir, TOKEN)
    config = DirectConfig(
        user_dir=user_dir, state_dir=state_dir, country_id="0", campaign_id="campaign-a",
        verified_game_version="Cygnus v4.5.2", session_token=TOKEN,
        allowed_actions=("probe_connection",), allowed_orders=("fleet_wait", "fleet_orbit"),
        allowed_native_actions=("select_research", "queue_district", "queue_building", "set_paused"),
        single_player_confirmed=True, non_ironman_confirmed=True,
        verification_timeout_seconds=120, poll_seconds=0.25, event_namespace=event_namespace,
    )
    raw = {key: str(value) if isinstance(value, Path) else value for key, value in vars(config).items()}
    runtime.config_path.write_text(json.dumps(raw), encoding="utf-8")
    if with_connection:
        runtime.connection = Connection()
    return SimpleNamespace(runtime=runtime, process=process, native=native, user_dir=user_dir,
                           state_dir=state_dir, config=config)


def observation(state, digest="d" * 64):
    return Observation(state=state, digest=digest, modified_at=time.time())


def base_state():
    return {
        "country_id": "0", "date": "2200.01.01",
        "metadata": {"player_country_ids": ["0"], "ironman": False, "multiplayer": False,
                     "campaign_id": "campaign-a", "game_version": "Cygnus v4.5.2"},
    }


def verified_state(token=TOKEN):
    state = base_state()
    state["direct_bridge"] = {
        "session_flags": [f"qs_direct_session_{token}"],
        "target_flags": [f"qs_direct_targets_{token}"],
        "done_flags": [],
    }
    return state


def patch_package(monkeypatch, calls, *, fail=None, payload=None):
    def package(dump_path, destination, country_id, *, expected_digest):
        calls.append(SimpleNamespace(dump_path=Path(dump_path), destination=Path(destination),
                                     country_id=country_id, expected_digest=expected_digest))
        if fail is not None:
            raise fail
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"packaged checkpoint")
        return payload or {"date": "2200.01.01", "sha256": "c" * 64, "parse_verified": True}

    monkeypatch.setattr(module, "package_dump", package)


def set_path(state, path, value):
    target = state
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def test_checkpoint_saves_a_new_artifact_without_closing_the_game(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    journal = Journal()
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: journal)
    observed = observation(verified_state(), "a" * 64)
    monkeypatch.setattr(env.runtime, "_fresh", lambda: (observed, {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages)

    result = env.runtime.checkpoint_game()

    assert result["status"] == "checkpoint_saved"
    assert result["game_still_running"] is True
    assert result["game_closed"] is False
    assert len(packages) == 1
    destination = packages[0].destination
    assert destination.parent == env.user_dir / "save games" / "qs_mcp"
    assert re.fullmatch(r"2200\.01\.01-[0-9a-f]{12}\.sav", destination.name)
    assert packages[0].dump_path == env.user_dir / "save games" / "dump_qs_direct.sav.txt"
    assert packages[0].country_id == "0"
    assert packages[0].expected_digest == "a" * 64
    assert result["checkpoint"]["path"] == str(destination)
    assert env.runtime.latest_checkpoint == result["checkpoint"]
    assert json.loads((env.state_dir / "last-checkpoint.json").read_text(encoding="utf-8")) == result["checkpoint"]
    assert env.process.closes == 0 and env.native.closed == 0
    assert env.runtime.connection.calls == [("request_observation",), ("set_paused", True)]


def test_checkpoint_requires_a_successful_fresh_observation(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal())
    env.runtime.connection.observation_status = "unavailable"
    fresh_calls = []
    monkeypatch.setattr(env.runtime, "_fresh", lambda: fresh_calls.append(True))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="refresh"):
        env.runtime.checkpoint_game()

    assert fresh_calls == [] and packages == []
    assert env.runtime.connection.calls == [("request_observation",)]
    assert env.process.closes == 0


def test_checkpoint_requires_verified_pause(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal())
    env.runtime.connection.pause_status = "blocked"
    fresh_calls = []
    monkeypatch.setattr(env.runtime, "_fresh", lambda: fresh_calls.append(True))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="pause"):
        env.runtime.checkpoint_game()

    assert fresh_calls == [] and packages == []
    assert env.runtime.connection.calls == [("request_observation",), ("set_paused", True)]


def test_checkpoint_blocks_on_unresolved_operation_before_requesting_a_dump(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal([{"id": 7}]))
    fresh_calls = []
    monkeypatch.setattr(env.runtime, "_fresh", lambda: fresh_calls.append(True))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="Unresolved"):
        env.runtime.checkpoint_game()

    assert fresh_calls == [] and packages == []
    assert env.process.closes == 0 and env.native.closed == 0


def test_checkpoint_rejects_an_unpaused_native_receipt(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal())
    observed = observation(verified_state())
    monkeypatch.setattr(env.runtime, "_fresh", lambda: (observed, {"runtime": {"paused": False}}))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="Pause was not verified"):
        env.runtime.checkpoint_game()

    assert packages == []
    assert env.process.closes == 0


def test_checkpoint_propagates_package_failure_without_closing(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal())
    observed = observation(verified_state())
    monkeypatch.setattr(env.runtime, "_fresh", lambda: (observed, {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages, fail=CampaignError("disk full"))

    with pytest.raises(CampaignError, match="disk full"):
        env.runtime.checkpoint_game()

    assert env.runtime.latest_checkpoint is None
    assert not (env.state_dir / "last-checkpoint.json").exists()
    assert env.process.closes == 0 and env.native.closed == 0


def test_checkpoint_rejects_a_closed_runtime_without_touching_connection(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal())
    env.runtime.closed = True

    with pytest.raises(CampaignError, match="closed"):
        env.runtime.checkpoint_game()

    assert env.runtime.connection.calls == []
    assert env.process.closes == 0


def test_close_is_idempotent_and_does_not_save(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal())
    saved = []
    monkeypatch.setattr(env.runtime, "checkpoint_game", lambda: saved.append(True))

    env.runtime.close()
    env.runtime.close()

    assert saved == []
    assert env.runtime.closed is True
    assert env.native.closed == 1
    assert env.process.closes == 1
    assert not hasattr(env.runtime, "terminate")
    status = env.runtime.status()
    assert status["connected"] is False
    assert status["game_still_running"] is None
    assert status["owns_game_process"] is False
    assert status["closes_game_on_disconnect"] is False


def test_status_reports_a_live_attached_game_without_claiming_ownership(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal())
    status = env.runtime.status()
    assert status["connection_mode"] == "existing_game"
    assert status["connected"] is True
    assert status["game_still_running"] is True
    assert status["owns_game_process"] is False
    assert status["closes_game_on_disconnect"] is False


def test_disconnect_save_true_checkpoints_before_releasing_and_never_kills(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    events = []
    monkeypatch.setattr(env.runtime, "checkpoint_game",
                        lambda: events.append("checkpoint") or {"status": "checkpoint_saved"})
    original_close = env.runtime.close

    def close():
        events.append("close")
        original_close()

    monkeypatch.setattr(env.runtime, "close", close)
    result = env.runtime.disconnect_game()

    assert events == ["checkpoint", "close"]
    assert result == {"status": "disconnected", "checkpoint": {"status": "checkpoint_saved"},
                      "game_closed": False, "game_still_running": True, "saved": True}
    assert env.process.closes == 1 and env.native.closed == 1


def test_disconnect_save_failure_retains_the_connection(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)

    def fail():
        raise CampaignError("save failed")

    monkeypatch.setattr(env.runtime, "checkpoint_game", fail)
    with pytest.raises(CampaignError, match="save failed"):
        env.runtime.disconnect_game()

    assert env.runtime.closed is False
    assert env.runtime.connection is not None
    assert env.process.closes == 0 and env.native.closed == 0


def test_disconnect_save_false_releases_without_checkpoint(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    saves = []
    monkeypatch.setattr(env.runtime, "checkpoint_game", lambda: saves.append(True))
    result = env.runtime.disconnect_game(save=False)
    assert saves == []
    assert result["saved"] is False and result["checkpoint"] is None
    assert result["game_closed"] is False
    assert env.process.closes == 1


@pytest.mark.parametrize("save", [None, 1, "yes", []])
def test_disconnect_rejects_non_boolean_save_before_closing(tmp_path, monkeypatch, save):
    env = make_runtime(tmp_path)
    with pytest.raises(ValueError, match="boolean"):
        env.runtime.disconnect_game(save=save)
    assert env.process.closes == 0


def test_initialize_backs_up_state_then_loads_unique_guarded_events(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    before = observation(base_state(), "b" * 64)
    after = observation(verified_state(), "a" * 64)
    observations = iter([before, after])
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (next(observations), {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages)
    inbox_writes = []
    original_write = env.runtime._write_inbox

    def capture(config, text):
        inbox_writes.append(text)
        return original_write(config, text)

    monkeypatch.setattr(env.runtime, "_write_inbox", capture)

    result = env.runtime.initialize()

    assert result is env.runtime
    assert env.runtime.connection is not None
    assert len(packages) == 1
    assert packages[0].dump_path == env.user_dir / "save games" / "dump_qs_direct.sav.txt"
    assert packages[0].destination == env.state_dir / "before-attachment.sav"
    assert packages[0].country_id == "0"
    assert packages[0].expected_digest == "b" * 64
    assert packages[0].destination.read_bytes() == b"packaged checkpoint"

    event_path = env.user_dir / ("qs_attach_events_" + TOKEN + ".txt")
    event_text = event_path.read_text(encoding="utf-8")
    namespace = load_direct_config(env.runtime.config_path).event_namespace
    assert namespace == "qs_live_" + TOKEN
    assert event_text.startswith("namespace = " + namespace + "\n")
    for number in (1, 2, 3, 4, 99):
        assert f"id = {namespace}.{number}" in event_text
    assert "is_ai = no is_multiplayer = no is_ironman = no" in event_text
    assert f"set_country_flag = qs_direct_session_{TOKEN}" in event_text
    assert f"qs_direct_targets_{TOKEN}" in event_text

    assert inbox_writes == [f"event {namespace}.99 0\nevent {namespace}.3 0\n"]
    assert inbox_writes[0].index(".99") < inbox_writes[0].index(".3")
    config = load_direct_config(env.runtime.config_path)
    assert config.event_namespace == namespace
    assert config.allowed_actions == ("probe_connection",)
    assert set(config.allowed_native_actions) == {
        "select_research", "queue_district", "queue_building", "set_paused"}
    assert config.allowed_orders == ("fleet_wait", "fleet_orbit")
    assert (env.user_dir / "qs_direct_inbox.txt").read_text(encoding="utf-8").startswith(
        "# SAA_DIRECT_INBOX_V1 session=" + TOKEN)
    attached = json.loads((env.state_dir / "attached.json").read_text(encoding="utf-8"))
    assert attached["session"] == TOKEN
    assert attached["pid"] == env.process.pid
    assert attached["game_started"] is False
    assert attached["connection_mode"] == "existing_game"
    assert attached["engine_single_player_guard_verified"] is True
    assert env.native.calls[0][0] == "bind"
    assert ("load_events", TOKEN) in env.native.calls
    assert env.native.closed == 0 and env.process.closes == 0

    env.runtime.close()
    assert not event_path.exists()
    assert env.process.closes == 1 and env.native.closed == 1


@pytest.mark.parametrize("path,value", [
    ("country_id", "1"),
    ("metadata.player_country_ids", ["0", "1"]),
    ("metadata.player_country_ids", []),
    ("metadata.ironman", True),
    ("metadata.ironman", "yes"),
    ("metadata.multiplayer", True),
    ("metadata.multiplayer", "yes"),
    ("metadata.game_version", "Cygnus v4.4.0"),
    ("metadata.campaign_id", ""),
    ("metadata.campaign_id", None),
])
def test_initialize_rejects_wrong_build_or_human_identity_before_backup(tmp_path, monkeypatch, path, value):
    env = make_runtime(tmp_path, with_connection=False)
    state = base_state()
    set_path(state, path, value)
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (observation(state, "b" * 64), {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="single-player non-Ironman country 0"):
        env.runtime.initialize()

    assert packages == []
    assert not (env.user_dir / ("qs_attach_events_" + TOKEN + ".txt")).exists()
    assert not any(call[0] == "load_events" for call in env.native.calls)
    env.runtime.close()


def test_initialize_requires_pause_before_backup(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (observation(base_state(), "b" * 64), {"runtime": {"paused": False}}))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="Pause the loaded game"):
        env.runtime.initialize()

    assert packages == []
    assert not (env.user_dir / ("qs_attach_events_" + TOKEN + ".txt")).exists()
    env.runtime.close()


def test_initialize_requires_session_flag_in_fresh_dump(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    after = verified_state()
    after["direct_bridge"]["session_flags"] = []
    observations = iter([observation(base_state(), "b" * 64), observation(after, "a" * 64)])
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (next(observations), {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(ValueError, match="session bootstrap"):
        env.runtime.initialize()

    assert env.runtime.connection is None
    assert not (env.state_dir / "attached.json").exists()
    assert packages
    env.runtime.close()


def test_initialize_requires_target_flag_in_fresh_dump(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    after = verified_state()
    after["direct_bridge"]["target_flags"] = []
    observations = iter([observation(base_state(), "b" * 64), observation(after, "a" * 64)])
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (next(observations), {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="targets or pause"):
        env.runtime.initialize()

    assert env.runtime.connection is None
    assert not (env.state_dir / "attached.json").exists()
    env.runtime.close()


def test_initialize_requires_pause_in_fresh_dump(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    after = verified_state()
    observations = iter([observation(base_state(), "b" * 64), observation(after, "a" * 64)])
    receipts = iter([{"runtime": {"paused": True}}, {"runtime": {"paused": False}}])
    monkeypatch.setattr(env.runtime, "_fresh", lambda: (next(observations), next(receipts)))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="targets or pause"):
        env.runtime.initialize()

    assert packages
    env.runtime.close()


def test_initialize_never_overwrites_an_existing_event_file(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    event_path = env.user_dir / ("qs_attach_events_" + TOKEN + ".txt")
    event_path.write_bytes(b"sentinel")
    observations = iter([observation(base_state(), "b" * 64), observation(verified_state(), "a" * 64)])
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (next(observations), {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(FileExistsError):
        env.runtime.initialize()

    assert event_path.read_bytes() == b"sentinel"
    assert not any(call[0] == "load_events" for call in env.native.calls)
    env.runtime.close()


def test_initialize_backup_failure_prevents_event_installation(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (observation(base_state(), "b" * 64), {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages, fail=CampaignError("backup failed"))

    with pytest.raises(CampaignError, match="backup failed"):
        env.runtime.initialize()

    assert packages
    assert not (env.user_dir / ("qs_attach_events_" + TOKEN + ".txt")).exists()
    assert not any(call[0] == "load_events" for call in env.native.calls)
    env.runtime.close()


def test_target_events_default_output_is_backward_compatible():
    default = render_target_events(TOKEN)
    explicit = render_target_events(TOKEN, namespace="qs_direct_targets")
    assert default == explicit
    assert default.startswith("namespace = qs_direct_targets\n\n")
    for number in (1, 2, 3, 4):
        assert f"id = qs_direct_targets.{number}" in default
    assert f"qs_direct_session_{TOKEN}" in default
    assert f"qs_direct_targets_{TOKEN}" in default


def test_target_events_custom_namespace_changes_event_ids_not_binding_flags():
    namespace = "qs_live_" + TOKEN
    script = render_target_events(TOKEN, namespace=namespace)
    assert script.startswith("namespace = " + namespace + "\n\n")
    for number in (1, 2, 3, 4):
        assert f"id = {namespace}.{number}" in script
    assert "namespace = qs_direct_targets" not in script
    assert f"qs_direct_session_{TOKEN}" in script
    assert f"qs_direct_targets_{TOKEN}" in script


@pytest.mark.parametrize("session", [
    None, "", "A" * 32, "a" * 31, "a" * 33, "a" * 31 + "\n", "a" * 31 + "}",
    "a" * 31 + "\neffect = {", "a" * 31 + " ",
])
def test_target_events_reject_session_script_injection(session):
    with pytest.raises(ValueError, match="session token"):
        render_target_events(session)


@pytest.mark.parametrize("namespace", [
    None, "", "1bad", "qs-direct", "qs direct", "qs\neffect = {", "qs.1", "q" * 81,
])
def test_target_events_reject_namespace_script_injection(namespace):
    with pytest.raises(ValueError, match="namespace"):
        render_target_events(TOKEN, namespace=namespace)


def test_direct_config_default_namespace_is_the_existing_wire_value(tmp_path):
    config = DirectConfig(
        user_dir=tmp_path / "user", state_dir=tmp_path / "state", country_id="0",
        campaign_id="campaign-a", verified_game_version="Cygnus v4.5.2", session_token=TOKEN,
        single_player_confirmed=True, non_ironman_confirmed=True,
    )
    assert config.event_namespace == "qs_direct_targets"
    validate_direct_config(config)
    raw = {key: str(value) if isinstance(value, Path) else value for key, value in vars(config).items()}
    raw.pop("event_namespace")
    path = tmp_path / "direct.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_direct_config(path).event_namespace == "qs_direct_targets"


@pytest.mark.parametrize("namespace", ["bad name", "1bad", "qs-direct", "qs.size", "q" * 81])
def test_direct_config_rejects_invalid_event_namespace(tmp_path, namespace):
    config = DirectConfig(
        user_dir=tmp_path / "user", state_dir=tmp_path / "state", country_id="0",
        campaign_id="campaign-a", verified_game_version="Cygnus v4.5.2", session_token=TOKEN,
        single_player_confirmed=True, non_ironman_confirmed=True,
    )
    with pytest.raises(ValueError, match="event_namespace"):
        validate_direct_config(DirectConfig(**{**vars(config), "event_namespace": namespace}))


def test_existing_game_prompt_describes_checkpoint_and_disconnect_without_closing():
    text = render_prompt(existing_game=True)
    assert "attached_status()" in text
    assert "checkpoint_game" in text
    assert "disconnect_game(save=true)" in text
    assert "game_closed=false" in text
    assert "游戏仍开着" in text
    assert "save_and_close" not in text
    assert "checkpoint_campaign" not in text
    default = render_prompt()
    assert "campaign_status()" in default
    assert "save_and_close" in default
    assert "checkpoint_game" not in default


def test_build_direct_server_registers_existing_game_prompt(tmp_path):
    server = build_direct_server(tmp_path / "direct.json", connection=object(), existing_game=True)
    result = asyncio.run(server.get_prompt("stellaris_agent"))
    text = result.messages[0].content.text
    assert "attached_status()" in text
    assert "checkpoint_game" in text
    assert "disconnect_game(save=true)" in text
    assert "game_closed=false" in text


def test_connect_existing_rejects_unresolved_previous_session_before_attach(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    journal = state_root / "sessions" / "old" / "journal.sqlite"
    journal.parent.mkdir(parents=True)
    journal.touch()
    monkeypatch.setattr(module, "_PreviewJournal", lambda path: Journal([{"id": 1}]))
    calls = []
    monkeypatch.setattr(module, "attach_game", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(CampaignError, match="unresolved"):
        connect_existing(tmp_path / "stellaris.exe", state_root, user_dir=tmp_path / "user")

    assert calls == []


def test_connect_existing_passes_single_selection_and_extended_mode_to_host(tmp_path, monkeypatch):
    process, native = object(), object()
    calls = []

    class Runtime:
        def __init__(self, process_arg, native_arg, user_dir, state_dir, token, *, extended=False):
            calls.append(("init", process_arg, native_arg, Path(user_dir), Path(state_dir), token, extended))

        def initialize(self):
            calls.append(("initialize",))
            return self

    monkeypatch.setattr(module, "attach_game", lambda executable, pid=None: calls.append(("attach", executable, pid)) or process)
    monkeypatch.setattr(module, "NativeResearchBridge", lambda process_arg, executable, state_dir: calls.append(
        ("native", process_arg, executable, Path(state_dir))) or native)
    monkeypatch.setattr(module, "AttachedRuntime", Runtime)
    executable = tmp_path / "stellaris.exe"
    user_dir = tmp_path / "user"
    user_dir.mkdir()

    result = connect_existing(executable, tmp_path / "state", user_dir=user_dir, pid=8240, extended=True)

    assert isinstance(result, Runtime)
    assert calls[0] == ("attach", executable, 8240)
    assert calls[1][0] == "native" and calls[1][1] is process and calls[1][2] == executable
    assert calls[2][0] == "init" and calls[2][1] is process and calls[2][2] is native
    assert calls[2][3] == user_dir and calls[2][4].parent == (tmp_path / "state").resolve() / "sessions"
    assert calls[2][5] != "a" * 32 and re.fullmatch(r"[0-9a-f]{32}", calls[2][5])
    assert calls[2][6] is True
    assert calls[3] == ("initialize",)


def test_connect_existing_closes_runtime_when_initialize_fails(tmp_path, monkeypatch):
    closed = []

    class Runtime:
        def __init__(self, *args, **kwargs):
            pass

        def initialize(self):
            raise CampaignError("handshake failed")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "attach_game", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "NativeResearchBridge", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "AttachedRuntime", Runtime)
    user_dir = tmp_path / "user"
    user_dir.mkdir()

    with pytest.raises(CampaignError, match="handshake failed"):
        connect_existing(tmp_path / "stellaris.exe", tmp_path / "state", user_dir=user_dir)

    assert closed == [True]


def test_wait_treats_processing_as_in_progress_until_expected_completion(tmp_path, monkeypatch):
    env = make_runtime(tmp_path)
    token = "b" * 32
    results = iter([
        {"status": "processing", "consumed": True},
        {"status": "inbox_executed", "kind": "run_inbox", "token": token,
         "country_id": "0", "execution_source": "window_message"},
    ])
    calls = []
    monkeypatch.setattr(env.native, "result", lambda value: calls.append(value) or next(results))

    result = env.runtime._wait(token, "run_inbox", "inbox_executed", timeout=2)

    assert result["status"] == "inbox_executed"
    assert calls == [token, token]


def test_initialize_accepts_and_restores_legacy_read_only_inbox(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    inbox = env.user_dir / "qs_direct_inbox.txt"
    legacy = (f"# SAA_DIRECT_INBOX_V1 session={TOKEN} country=0\n"
              "debug_dumpgamestate qs_direct\n").encode()
    inbox.write_bytes(legacy)
    observations = iter([observation(base_state(), "b" * 64), observation(verified_state(), "a" * 64)])
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (next(observations), {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages)

    env.runtime.initialize()

    assert env.runtime._old_inbox == legacy
    assert inbox.read_text(encoding="utf-8").splitlines()[1] == "# disarmed"
    env.runtime.close()
    assert inbox.read_bytes() == legacy


def test_initialize_rejects_an_active_legacy_inbox(tmp_path, monkeypatch):
    env = make_runtime(tmp_path, with_connection=False)
    inbox = env.user_dir / "qs_direct_inbox.txt"
    inbox.write_bytes((f"# SAA_DIRECT_INBOX_V1 session={TOKEN} country=0\n"
                       "event qs_direct_targets.3 0\n").encode())
    monkeypatch.setattr(env.runtime, "_fresh",
                        lambda: (observation(base_state(), "b" * 64), {"runtime": {"paused": True}}))
    packages = []
    patch_package(monkeypatch, packages)

    with pytest.raises(CampaignError, match="active"):
        env.runtime.initialize()

    assert not (env.user_dir / ("qs_attach_events_" + TOKEN + ".txt")).exists()
    env.runtime.close()
