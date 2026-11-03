from copy import deepcopy
from dataclasses import replace
import time
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from stellaris_autopilot.config import Action, Config
from stellaris_autopilot.engine import Engine, Proposal, validate_live
from stellaris_autopilot.journal import Journal
from stellaris_autopilot.observe import Observation


def state():
    return {
        "country_id": "0", "date": "2200.01.01", "resources": {"alloys": 200.0},
        "research": {"physics": "tech_lasers_1"},
        "metadata": {"game_version": "4.3.1", "campaign_id": "campaign-a", "ironman": "no", "multiplayer": "no",
                     "player_country_ids": ["0"]},
    }


def observation(value=None, digest="before", age=0):
    return Observation(value or state(), digest, time.time() - age)


class Observer:
    def __init__(self, *observations):
        self.observations = list(observations)
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]


class Driver:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def execute(self, steps):
        self.calls.append(steps)
        if self.error:
            raise self.error

    def execute_event(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error


@pytest.fixture
def harness(tmp_path):
    action = Action(
        id="select_physics", description="Select calibrated physics research", category="research",
        steps=[{"kind": "key", "key": "f4"}],
        preconditions=[{"path": "resources.alloys", "op": "gte", "value": 100}],
        expected=[{"path": "research.physics", "op": "eq", "value": "tech_lasers_2"}],
    )
    config = Config(
        save_path=tmp_path / "save.sav", state_dir=tmp_path,
        actions={action.id: action}, allowed_actions=[action.id], verified_game_version="4.3.1",
        campaign_id="campaign-a",
        resolution=(1920, 1080), verification_timeout_seconds=0.005, poll_seconds=0.001,
    )
    journal = Journal(tmp_path / "operations.sqlite")
    before = observation()
    after_state = deepcopy(before.state)
    after_state["research"]["physics"] = "tech_lasers_2"
    after = Observation(after_state, "after", before.modified_at + 0.001)
    driver = Driver()
    engine = Engine(config, journal, Observer(before, after), driver)
    yield engine, config, journal, driver, before, after
    journal.close()


def test_only_new_save_with_expected_result_marks_verified(harness):
    engine, config, journal, driver, before, after = harness
    result = engine.run(Proposal("select_physics", "Research focus"), before, live=True)
    assert result["status"] == "verified"
    assert len(driver.calls) == 1
    assert journal.records()[0]["status"] == "verified"
    assert after.digest in journal.records()[0]["detail"]


def test_dry_run_never_dispatches_or_creates_delivery_record(harness):
    engine, _, journal, driver, before, _ = harness
    assert engine.run(Proposal("select_physics", "test"), before)["status"] == "dry_run"
    assert driver.calls == []
    assert journal.records() == []


@pytest.mark.parametrize("action_id", ["invented_shell_command", "select_physics"])
def test_model_cannot_grant_itself_authority(harness, action_id):
    engine, config, journal, driver, before, _ = harness
    config.allowed_actions = []
    assert engine.run(Proposal(action_id, "model says approved"), before, live=True)["status"] == "blocked"
    assert driver.calls == []
    assert journal.records() == []


def test_missing_expected_field_is_not_treated_as_success(harness):
    engine, _, journal, driver, before, _ = harness
    del before.state["research"]["physics"]
    result = engine.run(Proposal("select_physics", "test"), before, live=True)
    assert result["status"] == "blocked"
    assert "Unobservable" in result["reason"]
    assert not journal.records() and not driver.calls


def test_explicitly_authorized_new_expected_path_requires_a_matching_new_save(harness):
    engine, config, journal, driver, before, after = harness
    config.actions["select_physics"] = replace(config.actions["select_physics"],
                                              allow_new_expected_paths=["research.physics"])
    before.state["research"] = {}
    before.state["metadata"]["missing_fields"] = ["research"]
    result = engine.run(Proposal("select_physics", "First physics selection"), before, live=True)
    assert result["status"] == "verified"
    assert len(driver.calls) == 1
    assert journal.records()[0]["status"] == "verified"
    assert after.digest in journal.records()[0]["detail"]
    assert before.state["research"] == {}  # Unknown was never replaced with a guessed baseline.


@pytest.mark.parametrize("failure", ["wrong_value", "still_missing", "same_digest", "older_mtime"])
def test_new_expected_path_cannot_verify_without_fresh_exact_evidence(harness, failure):
    engine, config, journal, driver, before, after = harness
    config.actions["select_physics"] = replace(config.actions["select_physics"],
                                              allow_new_expected_paths=["research.physics"])
    before.state["research"] = {}
    if failure == "wrong_value":
        after.state["research"]["physics"] = "tech_other"
    elif failure == "still_missing":
        after.state["research"] = {}
    elif failure == "same_digest":
        after = Observation(after.state, before.digest, after.modified_at)
    else:
        after = Observation(after.state, after.digest, before.modified_at - 0.001)
    engine.observer = Observer(before, after)
    result = engine.run(Proposal("select_physics", "First physics selection"), before, live=True)
    assert result["status"] == "unverified"
    assert len(driver.calls) == 1
    assert journal.records()[0]["status"] == "unverified"
    assert journal.unresolved()
    retry = engine.run(Proposal("select_physics", "Do not retry ambiguous input"), before, live=True)
    assert retry["status"] == "blocked" and len(driver.calls) == 1


def test_new_expected_path_opt_in_does_not_bypass_missing_preconditions(harness):
    engine, config, journal, driver, before, _ = harness
    config.actions["select_physics"] = replace(
        config.actions["select_physics"],
        allow_new_expected_paths=["research.physics"],
        preconditions=[{"path": "research.physics", "op": "eq", "value": "tech_lasers_1"}],
    )
    before.state["research"] = {}
    result = engine.run(Proposal("select_physics", "Missing precondition"), before, live=True)
    assert result["status"] == "blocked" and "Unobservable" in result["reason"]
    assert not driver.calls and not journal.records()


def test_new_expected_path_opt_in_never_supplies_a_change_baseline(harness):
    engine, config, journal, driver, before, _ = harness
    config.actions["select_physics"] = replace(
        config.actions["select_physics"],
        allow_new_expected_paths=["research.physics"],
        expected=[{"path": "research.physics", "op": "changed"}],
    )
    before.state["research"] = {}
    result = engine.run(Proposal("select_physics", "Missing change baseline"), before, live=True)
    assert result["status"] == "blocked" and "Unobservable" in result["reason"]
    assert not driver.calls and not journal.records()


def test_failed_precondition_prevents_input(harness):
    engine, _, _, driver, before, _ = harness
    before.state["resources"]["alloys"] = 2
    assert engine.run(Proposal("select_physics", "test"), before, live=True)["status"] == "blocked"
    assert not driver.calls


def test_existing_desired_state_does_not_repeat_purchase(harness):
    engine, _, journal, driver, before, after = harness
    result = engine.run(Proposal("select_physics", "test"), after, live=True)
    assert result["status"] == "waiting"
    assert not driver.calls and not journal.records()


@pytest.mark.parametrize("metadata, message", [
    ({"ironman": "yes"}, "Ironman"),
    ({"multiplayer": "yes"}, "Multiplayer"),
    ({"game_version": "9.9"}, "version"),
    ({"player_country_ids": ["7"]}, "human player"),
    ({"player_country_ids": []}, "human player"),
    ({"ironman": "unknown"}, "Ironman status unknown"),
    ({"multiplayer": "unknown"}, "Single-player status unknown"),
])
def test_live_readiness_rejections_never_dispatch(harness, metadata, message):
    engine, config, journal, driver, before, _ = harness
    before.state["metadata"].update(metadata)
    with pytest.raises(ValueError, match=message):
        engine.run(Proposal("select_physics", "test"), before, live=True)
    assert not driver.calls and not journal.records()


def test_local_confirmations_cannot_override_known_ironman_or_multiplayer(harness):
    _, config, _, _, before, _ = harness
    config.single_player_confirmed = config.non_ironman_confirmed = True
    before.state["metadata"]["ironman"] = "yes"
    with pytest.raises(ValueError, match="Ironman"):
        validate_live(config, before)
    before.state["metadata"]["ironman"] = "no"
    before.state["metadata"]["multiplayer"] = "yes"
    with pytest.raises(ValueError, match="Multiplayer"):
        validate_live(config, before)


def test_stale_snapshot_is_rejected(harness):
    engine, _, journal, driver, before, _ = harness
    stale = Observation(before.state, before.digest, time.time() - 1000)
    with pytest.raises(ValueError, match="stale"):
        engine.run(Proposal("select_physics", "test"), stale, live=True)
    assert not driver.calls and not journal.records()


def test_replanning_required_when_game_saved_during_llm_request(harness):
    engine, _, journal, driver, before, after = harness
    engine.observer = Observer(after)
    result = engine.run(Proposal("select_physics", "test"), before, live=True)
    assert result["status"] == "blocked" and "Snapshot changed" in result["reason"]
    assert not driver.calls and not journal.records()


def test_timeout_is_uncertain_and_blocks_later_actions_even_with_new_snapshot(harness):
    engine, _, journal, driver, before, after = harness
    engine.observer = Observer(before)
    first = engine.run(Proposal("select_physics", "test"), before, live=True)
    assert first["status"] == "unverified"
    second = engine.run(Proposal("select_physics", "retry"), after, live=True)
    assert second["status"] == "blocked"
    assert len(driver.calls) == 1
    assert journal.unresolved()[0]["status"] == "unverified"


def test_new_save_without_postcondition_does_not_prove_execution(harness):
    engine, _, journal, driver, before, _ = harness
    changed = deepcopy(before.state)
    changed["date"] = "2200.02.01"
    engine.observer = Observer(before, Observation(changed, "next-month", before.modified_at + 0.001))
    result = engine.run(Proposal("select_physics", "test"), before, live=True)
    assert result["status"] == "unverified"
    assert len(driver.calls) == 1 and journal.unresolved()


@pytest.mark.parametrize("change", ["empire", "date"])
def test_wrong_campaign_or_backward_date_cannot_verify(harness, change):
    engine, _, journal, _, before, after = harness
    if change == "empire":
        after.state["country_id"] = "1"
        after.state["metadata"]["player_country_ids"] = ["1"]
    else:
        after.state["date"] = "2199.12.30"
    engine.observer = Observer(before, after)
    assert engine.run(Proposal("select_physics", "test"), before, live=True)["status"] == "unverified"
    assert journal.unresolved()


def test_partial_delivery_error_remains_unresolved_after_restart(harness):
    engine, config, journal, driver, before, after = harness
    driver.error = RuntimeError("focus lost after first click")
    with pytest.raises(RuntimeError, match="focus lost"):
        engine.run(Proposal("select_physics", "test"), before, live=True)
    assert journal.records()[0]["status"] == "unverified"
    reopened = Journal(config.state_dir / "operations.sqlite")
    try:
        assert reopened.claim(after.digest, "select_physics", after.state) is None
        assert len(driver.calls) == 1
    finally:
        reopened.close()


def test_claim_excludes_another_connection_and_requires_inspection_note(harness):
    _, config, journal, _, before, after = harness
    operation = journal.claim(before.digest, "select_physics", before.state)
    assert operation
    second = Journal(config.state_dir / "operations.sqlite")
    try:
        assert second.claim(after.digest, "other_action", after.state) is None
        with pytest.raises(ValueError, match="inspection note"):
            second.reconcile(operation, " ")
        second.reconcile(operation, "Inspected in game: no research order was sent")
        # A reconciled delivery never permits dispatching the same snapshot/action again.
        assert second.claim(before.digest, "select_physics", before.state) is None
        assert second.claim(after.digest, "select_physics", after.state)
    finally:
        second.close()


def test_stop_file_prevents_new_delivery(harness):
    engine, config, journal, driver, before, _ = harness
    (config.state_dir / "STOP").touch()
    assert engine.run(Proposal("select_physics", "test"), before, live=True)["status"] == "stopped"
    assert not driver.calls and not journal.records()


def test_fixed_event_transport_receives_owned_country_and_not_model_text(harness):
    engine, config, journal, driver, before, after = harness
    action = Action("defend_capital", "Regroup", "defense", event="saa.20",
                    expected=[{"path": "research.physics", "op": "changed"}])
    config.actions = {action.id: action}
    config.allowed_actions = [action.id]
    assert engine.run(Proposal("defend_capital", "defend"), before, live=True)["status"] == "verified"
    assert driver.calls == [("saa.20", "0", "`")]
    assert journal.records()[0]["status"] == "verified"


@pytest.mark.parametrize("version", ["unknown", "UNKNOWN", "None", ""])
def test_user_cannot_mark_an_unknown_save_version_as_verified(harness, version):
    engine, config, journal, driver, before, _ = harness
    config.verified_game_version = version
    before.state["metadata"]["game_version"] = version
    with pytest.raises(ValueError, match="Unconfirmed game version"):
        engine.run(Proposal("select_physics", "test"), before, live=True)
    assert not driver.calls and not journal.records()


def test_new_campaign_with_same_player_and_successful_predicate_does_not_verify(harness):
    engine, _, journal, driver, before, after = harness
    before.state["metadata"]["campaign_id"] = "campaign-a"
    after.state["metadata"]["campaign_id"] = "campaign-b"
    after.state["date"] = "2300.01.01"
    engine.observer = Observer(before, after)
    result = engine.run(Proposal("select_physics", "test"), before, live=True)
    assert result["status"] == "unverified"
    assert "campaign" in result["reason"].lower()
    assert len(driver.calls) == 1 and journal.unresolved()


@pytest.mark.parametrize("before_date,after_date,expected", [
    ("2200.2.1", "2200.10.1", "verified"),
    ("2200.10.1", "2200.2.1", "unverified"),
])
def test_non_zero_padded_dates_compare_chronologically(harness, before_date, after_date, expected):
    engine, _, journal, _, before, after = harness
    before.state["date"] = before_date
    after.state["date"] = after_date
    result = engine.run(Proposal("select_physics", "test"), before, live=True)
    assert result["status"] == expected
    assert journal.records()[0]["status"] == expected


@pytest.mark.parametrize("metadata_id", [None, "", 123, {}, [], "campaign-b"])
def test_live_dispatch_requires_known_metadata_campaign_matching_local_authorization(harness, metadata_id):
    engine, _, journal, driver, before, _ = harness
    before.state["metadata"]["campaign_id"] = metadata_id
    with pytest.raises(ValueError, match="(?i)campaign"):
        engine.run(Proposal("select_physics", "test"), before, live=True)
    assert not driver.calls and not journal.records()


def test_missing_metadata_campaign_cannot_use_version_and_country_as_substitute(harness):
    engine, _, journal, driver, before, _ = harness
    del before.state["metadata"]["campaign_id"]
    with pytest.raises(ValueError, match="(?i)campaign"):
        engine.run(Proposal("select_physics", "test"), before, live=True)
    assert not driver.calls and not journal.records()


@pytest.mark.parametrize("configured_id", ["", "campaign-b"])
def test_local_live_permissions_do_not_transfer_between_campaigns(harness, configured_id):
    engine, config, journal, driver, before, _ = harness
    config.campaign_id = configured_id
    with pytest.raises(ValueError, match="(?i)campaign"):
        engine.run(Proposal("select_physics", "test"), before, live=True)
    assert not driver.calls and not journal.records()


def test_snapshot_that_expires_while_being_read_is_not_dispatched(harness):
    engine, _, journal, driver, before, _ = harness
    engine.observer = Observer(Observation(before.state, before.digest, time.time() - 1000))
    with pytest.raises(ValueError, match="stale"):
        engine.run(Proposal("select_physics", "test"), before, live=True)
    assert not driver.calls and not journal.records()


def test_stop_created_during_save_read_prevents_dispatch(harness):
    engine, config, journal, driver, before, _ = harness

    class StoppingObserver:
        def read(self):
            (config.state_dir / "STOP").touch()
            return before

    engine.observer = StoppingObserver()
    result = engine.run(Proposal("select_physics", "test"), before, live=True)
    assert result["status"] == "stopped"
    assert not driver.calls and not journal.records()


@pytest.mark.parametrize("bad_state,error", [
    ({"country_id": "0", "unserializable": object()}, TypeError),
    ({"country_id": "0", "value": float("nan")}, ValueError),
    ({"missing_country": "0"}, KeyError),
])
def test_malformed_claim_never_leaves_write_lock_held(harness, bad_state, error):
    _, config, journal, _, before, _ = harness
    with pytest.raises(error):
        journal.claim("bad", "select_physics", bad_state)
    assert journal.db.in_transaction is False
    second = Journal(config.state_dir / "operations.sqlite")
    try:
        assert second.claim(before.digest, "select_physics", before.state)
    finally:
        second.close()


def test_database_claim_error_rolls_back_before_next_process_tries(harness):
    _, config, journal, _, before, _ = harness
    journal.db.execute("""CREATE TRIGGER reject_claim BEFORE INSERT ON operations
                          BEGIN SELECT RAISE(FAIL, 'test insert failure'); END""")
    journal.db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="test insert failure"):
        journal.claim(before.digest, "select_physics", before.state)
    assert not journal.db.in_transaction
    journal.db.execute("DROP TRIGGER reject_claim")
    journal.db.commit()
    second = Journal(config.state_dir / "operations.sqlite")
    try:
        assert second.claim(before.digest, "select_physics", before.state)
    finally:
        second.close()


def test_terminal_operation_cannot_be_downgraded_by_a_late_process(harness):
    _, config, journal, _, before, _ = harness
    operation = journal.claim(before.digest, "select_physics", before.state)
    journal.update(operation, "dispatched", "Input sent")
    journal.update(operation, "verified", "Result observed")
    second = Journal(config.state_dir / "operations.sqlite")
    try:
        with pytest.raises(ValueError, match="status changed"):
            second.update(operation, "unverified", "Late failure from another runner")
        with pytest.raises(ValueError, match="status changed"):
            second.reconcile(operation, "Stale manual review")
        assert second.records()[0]["status"] == "verified"
        assert not second.db.in_transaction
    finally:
        second.close()


def test_journal_never_marks_undispatched_input_as_verified(harness):
    _, _, journal, _, before, _ = harness
    operation = journal.claim(before.digest, "select_physics", before.state)
    with pytest.raises(ValueError, match="status changed"):
        journal.update(operation, "verified", "No input actually sent")
    assert journal.unresolved()[0]["status"] == "pending"


def test_recent_history_is_bounded_chronological_and_excludes_large_saved_states(harness):
    _, _, journal, _, before, _ = harness
    for index in range(6):
        operation = journal.claim(f"snapshot-{index}", "select_physics", before.state)
        journal.reconcile(operation, "Fixture operation manually resolved")
    recent = journal.recent_records(limit=3)
    assert [row["snapshot"] for row in recent] == ["snapshot-3", "snapshot-4", "snapshot-5"]
    assert all("before_json" not in row for row in recent)
    assert len(journal.records()) == 6  # Full evidence stays available locally.
    with pytest.raises(ValueError, match="History limit"):
        journal.recent_records(limit=0)


def test_secondary_journal_failure_does_not_hide_partial_input_error(harness, monkeypatch):
    engine, _, journal, driver, before, _ = harness
    driver.error = RuntimeError("partial game input")

    def failed_update(*args):
        raise sqlite3.OperationalError("journal disk unavailable")

    monkeypatch.setattr(journal, "update", failed_update)
    with pytest.raises(RuntimeError, match="partial game input"):
        engine.run(Proposal("select_physics", "test"), before, live=True)
    assert journal.unresolved()[0]["status"] == "pending"


def test_sqlite_permission_error_during_claim_releases_transaction(harness):
    _, config, journal, _, before, _ = harness

    def deny_inserts(operation, table, *args):
        return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_INSERT and table == "operations" else sqlite3.SQLITE_OK

    journal.db.set_authorizer(deny_inserts)
    with pytest.raises(sqlite3.DatabaseError):
        journal.claim(before.digest, "select_physics", before.state)
    assert not journal.db.in_transaction
    journal.db.set_authorizer(None)
    second = Journal(config.state_dir / "operations.sqlite")
    try:
        assert second.claim(before.digest, "select_physics", before.state)
    finally:
        second.close()


def test_two_real_processes_cannot_both_reserve_game_input(harness):
    _, config, journal, _, _, _ = harness
    script = """import sys
from pathlib import Path
from stellaris_autopilot.journal import Journal
journal = Journal(Path(sys.argv[1]))
try:
    print(journal.claim(sys.argv[2], 'select_physics', {'country_id': '0'}))
finally:
    journal.close()
"""

    def reserve(digest):
        result = subprocess.run([sys.executable, "-c", script, str(config.state_dir / "operations.sqlite"), digest],
                                capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["first-process", "second-process"]))
    assert results.count("None") == 1
    assert len(journal.unresolved()) == 1
