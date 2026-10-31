import json
from copy import deepcopy
import time

import pytest

from stellaris_autopilot.cli import main
from stellaris_autopilot.engine import Proposal
from stellaris_autopilot.observe import Observation
from stellaris_autopilot.saves import SaveError


def save_fixture(path):
    path.write_text('''date="2200.01.01" version="4.test" ironman=no multiplayer=no
player={country=0} country={0={name="Player Empire" resources={alloys=100}
research={physics="tech_old"}}}''', encoding="utf-8")


def test_cli_demo_executes_complete_simulation(capsys):
    assert main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "SIMULATION" in result["mode"]
    assert result["result"]["status"] == "verified"
    assert result["before"]["physics"] != result["after"]["physics"]


def test_cli_inspect_reads_real_file(tmp_path, capsys):
    save = tmp_path / "fixture.sav"
    save_fixture(save)
    assert main(["inspect", str(save)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["country_id"] == "0"
    assert result["resources"]["alloys"] == 100


def test_cli_default_dry_run_does_not_change_save(tmp_path, capsys):
    save = tmp_path / "fixture.sav"
    save_fixture(save)
    before = save.read_bytes()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"save_path": save.name, "state_dir": "runtime",
        "allowed_actions": ["select_physics"], "actions": [{
            "id": "select_physics", "description": "Calibrated selection", "category": "research",
            "steps": [{"kind": "key", "key": "f4"}],
            "expected": [{"path": "research.physics", "op": "changed"}]}]}))
    assert main(["run", "--config", str(config), "--goal", "Prioritize research"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "dry_run"
    assert result["mode"] == "dry_run"
    assert save.read_bytes() == before
    assert (tmp_path / "runtime" / "decisions.jsonl").exists()


def test_cli_installs_mod_and_preserves_existing_install(tmp_path, capsys):
    destination = tmp_path / "Documents" / "mod"
    args = ["install-mod", "--mod-dir", str(destination)]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["installed"] == str(destination / "stellaris_ai_autopilot")
    launcher = destination / "stellaris_ai_autopilot.mod"
    assert "path=" in launcher.read_text()
    sentinel = destination / "stellaris_ai_autopilot" / "user-changes.txt"
    sentinel.write_text("Keep my modifications")
    assert main(args) == 2
    assert "already exists" in capsys.readouterr().err
    assert sentinel.read_text() == "Keep my modifications"


class SequenceObserver:
    def __init__(self, observations):
        self.observations = list(observations)

    def read(self):
        assert self.observations, "CLI read past the scenario's stopping condition"
        value = self.observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def cli_observation(date="2200.01.01", digest="first", **metadata):
    state = {
        "date": date, "country_id": "0", "metadata": {
            "game_version": "4.test", "campaign_id": "campaign-a", "player_country_ids": ["0"],
            "ironman": "no", "multiplayer": "no", **metadata,
        },
    }
    return Observation(state, digest, time.time())


@pytest.fixture
def loop_harness(tmp_path, monkeypatch):
    import stellaris_autopilot.cli as cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"save_path": "unused.sav", "state_dir": "runtime",
                                  "verified_game_version": "4.test", "campaign_id": "campaign-a",
                                  "resolution": [1920, 1080]}))
    emissions = []
    planned = []
    sleeps = []

    class Planner:
        def propose(self, goal, state, config, history):
            planned.append(deepcopy(state))
            return Proposal(None, "Wait for next observation")

    monkeypatch.setattr(cli, "RulePlanner", Planner)
    monkeypatch.setattr(cli, "emit", emissions.append)
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)

    def set_observations(observations):
        observer = SequenceObserver(observations)
        monkeypatch.setattr(cli, "SaveObserver", lambda *args: observer)
        return observer

    args = ["run", "--config", str(config), "--goal", "Research for one year",
            "--continuous", "--years", "1"]
    return args, set_observations, emissions, planned, sleeps


@pytest.mark.parametrize("change", [
    {"campaign_id": "campaign-b"}, {"game_version": "4.changed"}, {"country": "1"},
])
def test_continuous_cli_rejects_campaign_player_or_version_switch_before_replanning(
    loop_harness, change, capsys,
):
    args, sequence, emissions, planned, _ = loop_harness
    before = cli_observation()
    after = cli_observation("2200.02.01", "changed", **{k: v for k, v in change.items() if k != "country"})
    if "country" in change:
        after.state["country_id"] = change["country"]
        after.state["metadata"]["player_country_ids"] = [change["country"]]
    sequence([before, after])
    assert main(args) == 2
    assert len(planned) == 1
    assert "Campaign, player or game version changed" in capsys.readouterr().err


def test_year_horizon_uses_full_start_date_instead_of_stopping_at_january(loop_harness):
    args, sequence, emissions, planned, _ = loop_harness
    sequence([cli_observation("2200.10.31"), cli_observation("2201.01.01", "january"),
              cli_observation("2201.10.31", "anniversary")])
    assert main(args) == 0
    assert [state["date"] for state in planned] == ["2200.10.31", "2201.01.01"]
    assert emissions[-1]["status"] == "completed"


def test_continuous_cli_tolerates_two_incomplete_saves_then_recovers(loop_harness):
    args, sequence, emissions, planned, _ = loop_harness
    sequence([SaveError("partial ZIP"), OSError("file being replaced"), cli_observation(),
              cli_observation("2201.01.01", "complete")])
    assert main(args) == 0
    assert len(planned) == 1
    assert [entry["status"] for entry in emissions].count("waiting_for_save") == 2
    assert emissions[-1]["status"] == "completed"


def test_third_consecutive_save_failure_stops_instead_of_running_blind(loop_harness, capsys):
    args, sequence, emissions, planned, _ = loop_harness
    sequence([SaveError("partial ZIP")] * 3)
    assert main(args) == 2
    assert not planned
    assert len(emissions) == 2
    assert "partial ZIP" in capsys.readouterr().err


def test_snapshot_changed_during_planning_replans_on_next_save(loop_harness, monkeypatch):
    import stellaris_autopilot.cli as cli

    args, sequence, emissions, planned, _ = loop_harness
    sequence([cli_observation(), cli_observation("2200.02.01", "second"),
              cli_observation("2201.01.01", "end")])
    calls = []

    class Engine:
        def __init__(self, *args):
            pass

        def run(self, *args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return {"status": "blocked", "reason": "Snapshot changed while planning; replan"}
            return {"status": "waiting"}

    monkeypatch.setattr(cli, "Engine", Engine)
    assert main(args) == 0
    assert len(planned) == len(calls) == 2
    assert emissions[-1]["status"] == "completed"


def test_live_arm_delay_precedes_planning_and_defaults_to_five_seconds(loop_harness, monkeypatch):
    import stellaris_autopilot.windows as windows

    args, sequence, emissions, planned, sleeps = loop_harness
    sequence([cli_observation(), cli_observation(), cli_observation("2201.01.01", "end")])
    monkeypatch.setattr(windows, "WindowsDriver", lambda **kwargs: object())
    assert main(args + ["--live"]) == 0
    assert emissions[0]["status"] == "arming"
    assert emissions[0]["delay_seconds"] == 5
    assert sleeps[0] == 5
    assert len(planned) == 1


def test_live_cli_requires_campaign_lock_before_initializing_any_input_driver(loop_harness, monkeypatch, capsys):
    import stellaris_autopilot.windows as windows
    from pathlib import Path

    args, sequence, emissions, planned, _ = loop_harness
    path = Path(args[2])
    config = json.loads(path.read_text())
    del config["campaign_id"]
    path.write_text(json.dumps(config))
    sequence([cli_observation()])
    monkeypatch.setattr(windows, "WindowsDriver", lambda **kwargs: pytest.fail("must reject before UI access"))
    assert main(args + ["--live"]) == 2
    assert "campaign" in capsys.readouterr().err.lower()
    assert not planned and not emissions


@pytest.mark.parametrize("status,expected_exit", [("blocked", 2), ("unverified", 2), ("stopped", 0)])
def test_cli_shell_exit_reflects_failed_execution_outcome(loop_harness, monkeypatch, status, expected_exit):
    import stellaris_autopilot.cli as cli

    args, sequence, emissions, planned, _ = loop_harness
    sequence([cli_observation()])

    class Engine:
        def __init__(self, *args):
            pass

        def run(self, *args, **kwargs):
            return {"status": status, "reason": "Test execution outcome"}

    monkeypatch.setattr(cli, "Engine", Engine)
    assert main(args) == expected_exit
    assert emissions[-1]["status"] == status
    assert len(planned) == 1


def test_failed_doctor_has_nonzero_exit_for_shell_automation(tmp_path, capsys):
    save = tmp_path / "save.txt"
    save_fixture(save)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"save_path": str(save)}), encoding="utf-8")
    assert main(["doctor", "--config", str(config)]) == 2
    assert json.loads(capsys.readouterr().out)["save_policy_ready"] is False


def test_rejected_live_setup_does_not_create_journal(loop_harness, capsys):
    from pathlib import Path

    args, sequence, *_ = loop_harness
    config = Path(args[2])
    content = json.loads(config.read_text())
    content["campaign_id"] = "wrong"
    config.write_text(json.dumps(content))
    sequence([cli_observation()])
    assert main(args + ["--live"]) == 2
    assert not (config.parent / "runtime").exists()


def test_mod_launcher_created_during_install_is_not_overwritten(tmp_path, monkeypatch, capsys):
    import stellaris_autopilot.cli as cli

    original_copy = cli.shutil.copytree
    destination = tmp_path / "mod"
    launcher = destination / "stellaris_ai_autopilot.mod"

    def copy_then_race(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        launcher.write_text("preserve concurrent user file", encoding="utf-8")
        return result

    monkeypatch.setattr(cli.shutil, "copytree", copy_then_race)
    assert main(["install-mod", "--mod-dir", str(destination)]) == 2
    assert launcher.read_text() == "preserve concurrent user file"
