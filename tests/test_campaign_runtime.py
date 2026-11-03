import asyncio
import copy
import json
import os
import sqlite3
import time

import pytest

pytest.importorskip("mcp")

from stellaris_autopilot.campaign import CampaignError, CampaignStore
from stellaris_autopilot.campaign_runtime import CampaignConnection, CampaignRuntime, validate_format_proof
from stellaris_autopilot.direct import load_direct_config
from stellaris_autopilot.direct_mcp import DirectConnection, build_direct_server
from stellaris_autopilot.native import PINNED_EXE_SHA256


SAVE = """version="Cygnus v4.5.2" date="2200.01.01" galaxy={name="runtime-campaign"}
ironman=no multiplayer=no player={country=0} species_db={7={portrait="human"}}
country={0={name="Earth" founder_species_ref=7 resources={minerals=617.55104}
flag={icon={category="human" file="flag_human_9.dds"}}}}
"""


@pytest.fixture
def campaign(tmp_path):
    original = tmp_path / "source.sav"
    original.write_text(SAVE, encoding="utf-8")
    store = CampaignStore(tmp_path / "campaign")
    with store.locked():
        store.initialize(original, country_id="0", single_player_confirmed=True, non_ironman_confirmed=True)
        session = store.begin_session()
        store.update_session(session["token"], status="connected", owned_pid=123)
        config_path = store.root / "sessions" / session["token"] / "direct.json"
        userdir = store.root / "userdir"
        userdir.mkdir()
        config_path.write_text(
            json.dumps(
                {
                    "user_dir": str(userdir),
                    "state_dir": str(config_path.parent),
                    "country_id": "0",
                    "campaign_id": "runtime-campaign",
                    "verified_game_version": "Cygnus v4.5.2",
                    "session_token": session["token"],
                    "single_player_confirmed": True,
                    "non_ironman_confirmed": True,
                    "allowed_actions": ["probe_connection"],
                    "allowed_native_actions": ["set_paused"],
                    "verification_timeout_seconds": 0.5,
                    "poll_seconds": 0.001,
                }
            ),
            encoding="utf-8",
        )
        config = load_direct_config(config_path)
        config.dump_path.parent.mkdir(parents=True)
        # Match the checkpoint and identify only this session's bootstrap.
        fresh = SAVE.replace(
            '0={name="Earth"', "0={flags={qs_direct_session_" + session["token"] + '=1} name="Earth"'
        )
        config.dump_path.write_text(fresh, encoding="utf-8")
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        (game_dir / "userdir.txt").write_text(userdir.as_posix() + "/", encoding="utf-8")
        yield store, session, config_path, config, fresh, game_dir


def connection(campaign):
    store, session, config_path, *_ = campaign
    return CampaignConnection(config_path, store=store, session=session)


def test_first_write_confirms_reload_before_dispatch(campaign, monkeypatch):
    calls = []
    monkeypatch.setattr(
        DirectConnection, "_execute", lambda *args, **kwargs: calls.append(args) or {"status": "ok"}
    )
    bound = connection(campaign)
    assert bound.execute("probe_connection")["status"] == "ok"
    store = campaign[0]
    assert len(calls) == 1
    assert store.latest()[1]["reload_verified"] is True
    assert len(store.latest()[1]["reload_gameplay_sha256"]) == 64


@pytest.mark.parametrize(
    "old,new",
    [
        ("minerals=617.55104", "minerals=42"),
        ('date="2200.01.01"', 'date="2199.12.30"'),
        ("runtime-campaign", "wrong-campaign"),
    ],
)
def test_wrong_loaded_state_cannot_dispatch_even_on_same_date(campaign, monkeypatch, old, new):
    _, _, _, config, fresh, _ = campaign
    config.dump_path.write_text(fresh.replace(old, new), encoding="utf-8")
    calls = []
    monkeypatch.setattr(DirectConnection, "_execute", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        connection(campaign).execute("probe_connection")
    assert calls == []
    assert campaign[0].latest()[1]["reload_verified"] is False


def test_new_session_receipts_and_source_path_do_not_change_gameplay(campaign, monkeypatch):
    monkeypatch.setattr(DirectConnection, "request_observation", lambda self: {"status": "observed"})
    assert connection(campaign).request_observation()["status"] == "observed"
    latest = campaign[0].latest()[1]
    assert latest["reload_verified"] is True
    assert latest["reload_session_token"] == campaign[1]["token"]


def test_unavailable_observation_does_not_claim_reload(campaign, monkeypatch):
    monkeypatch.setattr(DirectConnection, "request_observation", lambda self: {"status": "unavailable"})
    assert connection(campaign).request_observation()["status"] == "unavailable"
    assert campaign[0].latest()[1]["reload_verified"] is False


def test_reload_gate_runs_once_before_later_gameplay_changes(campaign, monkeypatch):
    monkeypatch.setattr(DirectConnection, "_execute", lambda *args, **kwargs: {"status": "ok"})
    bound = connection(campaign)
    bound.execute("probe_connection")
    _, _, _, config, fresh, _ = campaign
    config.dump_path.write_text(fresh.replace("minerals=617.55104", "minerals=42"), encoding="utf-8")
    assert bound.execute("probe_connection")["status"] == "ok"


def test_mcp_server_uses_the_shared_campaign_gate(campaign, monkeypatch):
    monkeypatch.setattr(DirectConnection, "_execute", lambda *args, **kwargs: {"status": "ok"})
    bound = connection(campaign)
    server = build_direct_server(campaign[2], connection=bound)
    asyncio.run(server._tool_manager.call_tool("execute_action", {"action_id": "probe_connection"}))
    assert campaign[0].latest()[1]["reload_verified"] is True


class OwnedProcess:
    def __init__(self):
        self.live, self.terminated = True, False
        self.waited = False

    def poll(self):
        return None if self.live else 1

    def terminate(self):
        self.terminated, self.live = True, False

    def wait(self, timeout):
        assert timeout == 20
        self.waited = True
        return 1


class NativeSnapshot:
    def __init__(self, config, snapshot, *, paused=True):
        self.config, self.snapshot, self.paused = config, snapshot, paused
        self.tokens, self.cancelled = [], []

    def request_dump(self, token):
        self.tokens.append(token)
        self.config.dump_path.write_text(self.snapshot, encoding="utf-8")
        # CampaignRuntime samples published_at immediately before this call.
        # Windows can report the write's mtime just before that sample, so
        # stamp the fixture dump a few milliseconds after it was written.
        stamp = time.time_ns() + 10_000_000
        os.utime(self.config.dump_path, ns=(stamp, stamp))

    def result(self, token):
        return {
            "status": "dump_requested",
            "kind": "request_observation",
            "country_id": "0",
            "token": token,
            "execution_source": "window_message",
            "runtime": {"paused": self.paused, "protected_pause": False},
        }

    def cancel(self, token):
        self.cancelled.append(token)


def runtime(campaign, monkeypatch, *, format_verified=True, paused=True):
    store, session, config_path, config, fresh, game_dir = campaign
    process, native = OwnedProcess(), NativeSnapshot(config, fresh, paused=paused)
    bound = CampaignRuntime(
        store, session, config_path, process, native, format_verified=format_verified, game_dir=game_dir
    )
    monkeypatch.setattr(bound.connection, "request_observation", lambda: {"status": "observed"})
    monkeypatch.setattr(bound.connection, "set_paused", lambda value: {"status": "pause_verified"})
    return bound, process, native


def test_save_and_close_publishes_checkpoint_before_closing_exact_process(campaign, monkeypatch):
    bound, process, native = runtime(campaign, monkeypatch)
    original_terminate = process.terminate

    def terminate_after_save():
        latest = campaign[0].latest()[1]
        assert latest["session_token"] == campaign[1]["token"]
        assert latest["parse_verified"] is True
        original_terminate()

    monkeypatch.setattr(process, "terminate", terminate_after_save)
    result = bound.checkpoint(close=True)
    assert result["status"] == "saved_and_closed"
    assert process.terminated and process.waited
    assert campaign[0].status()["current_session"]["status"] == "closed"
    assert not (campaign[5] / "userdir.txt").exists()
    assert native.cancelled == native.tokens


def test_checkpoint_keeps_game_and_redirect_for_continuing_play(campaign, monkeypatch):
    bound, process, _ = runtime(campaign, monkeypatch)
    assert bound.checkpoint()["status"] == "checkpoint_verified"
    assert process.live and not process.terminated
    assert (campaign[5] / "userdir.txt").exists()
    assert campaign[0].status()["current_session"]["status"] == "checkpointed"


def test_failed_save_never_closes_owned_game_or_removes_redirect(campaign, monkeypatch):
    bound, process, native = runtime(campaign, monkeypatch)
    before = campaign[0].load()["latest_checkpoint"]

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(campaign[0], "checkpoint_dump", disk_full)
    with pytest.raises(OSError, match="disk full"):
        bound.checkpoint(close=True)
    assert process.live and not process.terminated
    assert (campaign[5] / "userdir.txt").exists()
    assert campaign[0].load()["latest_checkpoint"] == before
    assert native.cancelled == native.tokens


def test_no_format_proof_retains_saved_checkpoint_and_paused_game(campaign, monkeypatch):
    bound, process, _ = runtime(campaign, monkeypatch, format_verified=False)
    with pytest.raises(CampaignError, match="real-game reload proof"):
        bound.checkpoint(close=True)
    assert process.live and not process.terminated
    assert campaign[0].status()["current_session"]["status"] == "checkpointed"
    assert not (campaign[3].state_dir / "STOP").exists()


def test_nonpaused_native_snapshot_cannot_publish_or_close(campaign, monkeypatch):
    bound, process, native = runtime(campaign, monkeypatch, paused=False)
    before = campaign[0].load()["latest_checkpoint"]
    with pytest.raises(CampaignError, match="paused native snapshot"):
        bound.checkpoint(close=True)
    assert campaign[0].load()["latest_checkpoint"] == before
    assert process.live and not process.terminated
    assert native.cancelled == native.tokens


def test_unresolved_order_blocks_save_and_close_before_requesting_snapshot(campaign, monkeypatch):
    bound, process, native = runtime(campaign, monkeypatch)
    with sqlite3.connect(campaign[3].state_dir / "journal.sqlite") as database:
        database.execute("CREATE TABLE operations (id INTEGER, action TEXT, status TEXT)")
        database.execute("INSERT INTO operations VALUES (1,'queue_building','unverified')")
    with pytest.raises(CampaignError, match="unresolved"):
        bound.checkpoint(close=True)
    assert native.tokens == []
    assert process.live and not process.terminated


def test_dead_owned_process_is_not_saved_or_reopened(campaign, monkeypatch):
    bound, process, native = runtime(campaign, monkeypatch)
    process.live = False
    with pytest.raises(CampaignError, match="exited before"):
        bound.checkpoint(close=True)
    assert native.tokens == []
    assert not process.terminated


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("token", "b" * 32),
        ("started_at", time.time() + 3600),
    ],
)
def test_reload_requires_own_fresh_bootstrap(campaign, field, replacement):
    store, session, *_ = campaign
    manifest = store.load()
    manifest["sessions"][-1][field] = replacement
    store._write(manifest)
    with pytest.raises(CampaignError, match="fresh matching"):
        connection(campaign)._confirm_reload()
    assert store.latest()[1]["reload_verified"] is False


def reload_proof():
    proof = {
        "verified": True,
        "exe_sha256": PINNED_EXE_SHA256,
        "game_version": "Cygnus v4.5.2",
        "keyboard_mouse_input": False,
        "checkpoint_format": "zip-meta-gamestate-from-debug-dump",
        "packaged": {"campaign_id": "runtime-campaign", "country_id": "0", "game_version": "Cygnus v4.5.2"},
        "cleanup": {"owned_game_closed": True, "redirect_removed": True, "unresolved": []},
        "cycles": [],
    }
    for current, saved in (("first", "second"), ("second", "third")):
        proof["cycles"].append(
            {
                "reload_verified": True,
                "campaign_id": "runtime-campaign",
                "country_id": "0",
                "expected_date": "2200.01.01",
                "observed_date": "2200.01.01",
                "loaded_checkpoint": current,
                "loaded_sha256": "a" * 64,
                "expected_state_sha256": "b" * 64,
                "observed_state_sha256": "b" * 64,
                "save_and_close": {
                    "status": "saved_and_closed",
                    "owned_process_closed": True,
                    "checkpoint": {"id": saved, "sha256": "a" * 64, "parse_verified": True},
                },
            }
        )
    return proof


@pytest.mark.parametrize(
    "edit",
    [
        lambda proof: proof.update(verified=False),
        lambda proof: proof.update(exe_sha256="wrong"),
        lambda proof: proof.update(keyboard_mouse_input=True),
        lambda proof: proof["packaged"].update(campaign_id="other"),
        lambda proof: proof["cleanup"].update(unresolved=["unknown command"]),
        lambda proof: proof.update(cycles=[{}, {}]),
        lambda proof: proof.update(cycles=[True, True]),
        lambda proof: proof["cycles"][1].update(loaded_checkpoint="first"),
        lambda proof: proof["cycles"][1].update(loaded_checkpoint="unrelated"),
        lambda proof: proof["cycles"][1].update(loaded_sha256="wrong"),
        lambda proof: proof["cycles"][1].update(observed_state_sha256="wrong"),
        lambda proof: proof["cycles"][1]["save_and_close"].update(owned_process_closed=False),
    ],
)
def test_reload_report_requires_actual_correlated_reload_and_close(tmp_path, edit):
    valid = reload_proof()
    manifest = copy.deepcopy(valid["packaged"])
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(valid), encoding="utf-8")
    assert validate_format_proof(path, manifest) is True
    edit(valid)
    path.write_text(json.dumps(valid), encoding="utf-8")
    assert validate_format_proof(path, manifest) is False


@pytest.mark.parametrize("text", ["[]", "null", "42", "{broken", "\ufffd"])
def test_bad_or_missing_format_report_is_not_accepted(tmp_path, text):
    manifest = reload_proof()["packaged"]
    assert validate_format_proof(None, manifest) is False
    path = tmp_path / "proof.json"
    assert validate_format_proof(path, manifest) is False
    path.write_text(text, encoding="utf-8")
    assert validate_format_proof(path, manifest) is False
