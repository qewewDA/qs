from copy import deepcopy
import json

import pytest

from stellaris_autopilot.config import load_config
from stellaris_autopilot.predicates import MISSING, evaluate, lookup, validate_predicate
from stellaris_autopilot.windows import DriverError


BASE = {
    "save_path": "saves/game.sav", "state_dir": "state", "log_path": "logs/game.log",
    "actions": [{"id": "physics", "description": "choose physics", "category": "research",
                 "steps": [{"kind": "key", "key": "f4"}],
                 "expected": [{"path": "research.physics", "op": "eq", "value": "tech_lasers_2"}]}],
    "allowed_actions": ["physics"], "resolution": [1920, 1080],
}


def write_config(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_paths_are_relative_to_config_not_calling_directory(tmp_path, monkeypatch):
    path = write_config(tmp_path, BASE)
    monkeypatch.chdir(tmp_path.parent)
    config = load_config(path)
    assert config.save_path == tmp_path / "saves/game.sav"
    assert config.state_dir == tmp_path / "state"
    assert config.log_path == tmp_path / "logs/game.log"
    assert config.resolution == (1920, 1080)


@pytest.mark.parametrize("mutate", [
    lambda raw: raw.update(typo_timeout=5),
    lambda raw: raw.update(allowed_actions=["invented"]),
    lambda raw: raw.update(resolution=[True, 1080]),
    lambda raw: raw.update(poll_seconds=0),
    lambda raw: raw.update(verification_timeout_seconds=601),
    lambda raw: raw.update(max_save_age_seconds=float("nan")),
    lambda raw: raw.update(single_player_confirmed="yes"),
    lambda raw: raw.update(country_id="0;quit"),
    lambda raw: raw["actions"].append(deepcopy(raw["actions"][0])),
    lambda raw: raw["actions"][0].update(expected=[]),
    lambda raw: raw["actions"][0].update(event="saa.20"),
    lambda raw: raw["actions"][0].update(steps=[], event="cash"),
    lambda raw: raw["actions"][0].update(category="shell"),
])
def test_invalid_or_unauthorized_configuration_is_rejected(tmp_path, mutate):
    raw = deepcopy(BASE)
    mutate(raw)
    with pytest.raises((ValueError, DriverError)):
        load_config(write_config(tmp_path, raw))


def test_fixed_mod_event_requires_no_dummy_macro(tmp_path):
    raw = deepcopy(BASE)
    raw["actions"][0].update(id="defend_capital", steps=[], event="saa.20")
    raw["allowed_actions"] = ["defend_capital"]
    assert load_config(write_config(tmp_path, raw)).actions["defend_capital"].event == "saa.20"


def test_empty_actions_are_a_valid_observer_only_profile(tmp_path):
    config = load_config(write_config(tmp_path, {"save_path": "save.sav"}))
    assert not config.actions and not config.allowed_actions


def test_new_expected_paths_require_explicit_local_opt_in(tmp_path):
    default = load_config(write_config(tmp_path, BASE))
    assert default.actions["physics"].allow_new_expected_paths == []
    raw = deepcopy(BASE)
    raw["actions"][0]["allow_new_expected_paths"] = ["research.physics"]
    config = load_config(write_config(tmp_path, raw))
    assert config.actions["physics"].allow_new_expected_paths == ["research.physics"]


@pytest.mark.parametrize("paths", [
    None, "research.physics", {}, [None], [123], [[]], [""], ["missing.path"],
    ["research.physics", "research.physics"],
])
def test_invalid_new_expected_paths_are_actionable_config_errors(tmp_path, paths):
    raw = deepcopy(BASE)
    raw["actions"][0]["allow_new_expected_paths"] = paths
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, raw))


@pytest.mark.parametrize("predicate", [
    {"path": "research.physics", "op": "changed"},
    {"path": "research.physics", "op": "exists"},
    {"path": "research.physics", "op": "contains", "value": "tech_lasers_2"},
    {"path": "research.physics", "op": "gte", "value": 1},
    *({"path": "research.physics", "op": "eq", "value": value}
      for value in (None, "", "  ", [], {}, float("nan"), float("inf"))),
])
def test_new_expected_paths_require_exact_nonempty_known_values(tmp_path, predicate):
    raw = deepcopy(BASE)
    raw["actions"][0].update(allow_new_expected_paths=["research.physics"], expected=[predicate])
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, raw))


def test_new_expected_paths_cannot_hide_a_change_predicate_on_the_same_path(tmp_path):
    raw = deepcopy(BASE)
    raw["actions"][0]["allow_new_expected_paths"] = ["research.physics"]
    raw["actions"][0]["expected"].append({"path": "research.physics", "op": "changed"})
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, raw))


def test_a_precondition_path_alone_cannot_authorize_a_new_expected_path(tmp_path):
    raw = deepcopy(BASE)
    raw["actions"][0].update(
        allow_new_expected_paths=["resources.alloys"],
        preconditions=[{"path": "resources.alloys", "op": "gte", "value": 100}],
    )
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, raw))


@pytest.mark.parametrize("value", [0, False, 1.5, "tech_physics_1"])
def test_explicit_scalar_targets_remain_distinct_from_unknown(tmp_path, value):
    raw = deepcopy(BASE)
    raw["actions"][0]["allow_new_expected_paths"] = ["research.physics"]
    raw["actions"][0]["expected"][0]["value"] = value
    assert load_config(write_config(tmp_path, raw)).actions["physics"].expected[0]["value"] == value


@pytest.mark.parametrize("predicate", [
    {"path": "x", "op": "execute", "value": "shell"},
    {"path": "", "op": "eq", "value": 0},
    {"path": "a..b", "op": "eq", "value": 0},
    {"path": "x", "op": "eq"},
    {"path": "x", "op": "gte", "value": True},
    {"path": "x", "op": "gte", "value": float("inf")},
    {"path": "x", "op": "changed", "code": "exec"},
])
def test_predicate_language_cannot_execute_or_use_invalid_numbers(predicate):
    with pytest.raises(ValueError):
        validate_predicate(predicate)


def test_unknown_paths_never_pass_as_zero_or_change():
    assert lookup({"fleets": []}, "fleets.0.order") is MISSING
    assert not evaluate({"path": "resources.alloys", "op": "eq", "value": 0}, {})
    assert not evaluate({"path": "x", "op": "changed"}, {"x": 2}, {})
    assert not evaluate({"path": "x", "op": "gte", "value": 0}, {"x": True})
    assert not evaluate({"path": "x", "op": "eq", "value": 1}, {"x": True})
    assert evaluate({"path": "fleets.0.order", "op": "eq", "value": "move"},
                    {"fleets": [{"order": "move"}]})


@pytest.mark.parametrize("update", [
    {"save_path": None}, {"save_path": []}, {"state_dir": ""}, {"state_dir": None},
    {"log_path": {}}, {"console_key": []}, {"allowed_actions": [{}]},
    {"save_steps": {}}, {"poll_seconds": 10 ** 1000},
])
def test_invalid_profile_types_are_actionable_errors_not_tracebacks(tmp_path, update):
    raw = {**deepcopy(BASE), **update}
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, raw))


@pytest.mark.parametrize("update", [
    {"id": []}, {"id": 123}, {"id": "wait"}, {"id": "a" * 81},
    {"description": None}, {"category": []}, {"steps": None}, {"event": {}},
])
def test_invalid_action_identity_or_shape_is_rejected(tmp_path, update):
    raw = deepcopy(BASE)
    raw["actions"][0].update(update)
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, raw))


def test_windows_utf8_bom_config_is_supported(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(BASE), encoding="utf-8-sig")
    assert load_config(path).allowed_actions == ["physics"]


def test_predicates_fail_closed_for_unicode_indices_and_overflow():
    assert lookup({"fleet": [1]}, "fleet.²") is MISSING
    assert not evaluate({"path": "value", "op": "gte", "value": 1}, {"value": 10 ** 1000})
    with pytest.raises(ValueError):
        validate_predicate({"path": "value", "op": ["eq"], "value": 1})
    with pytest.raises(ValueError):
        validate_predicate({"path": "value", "op": "gte", "value": 10 ** 1000})
