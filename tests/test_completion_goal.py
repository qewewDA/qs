from copy import deepcopy
import hashlib
import json

from pydantic import TypeAdapter, ValidationError
import pytest

from stellaris_autopilot.completion_goal import (
    CompletionIntent, EnterSystemIntent, SurveyBodyIntent, SurveySystemIntent,
    TimeAuthorization, evaluate_goal, game_day, normalize_completion,
)


BODY = {"kind": "survey_body", "fleet_id": "3", "planet_id": "8", "system_id": "92"}
SYSTEM = {"kind": "survey_system", "fleet_id": "3", "system_id": "92", "planet_ids": ["9", "8"]}
ENTER = {"kind": "enter_system", "fleet_id": "3", "planet_id": "8", "system_id": "92"}
TIME = {"allow_time_advance": True, "max_game_days": 90, "max_wall_seconds": 120, "final_pause": True}


@pytest.fixture
def state():
    return {
        "country_id": "0", "metadata": {"missing_fields": []},
        "fleets_by_id": {"3": {
            "id": "3", "owner": 0, "controller": "0", "ship_class": "shipclass_science_ship",
            "mobile": True, "combat": {"start_date": "0.01.01"}, "system_id": "91",
            "orders_known": True, "actions": {}, "current_order": {},
        }},
        "space": {
            "systems_by_id": {"92": {"id": "92", "visited": True, "planets_known": True,
                                        "planet_ids": ["8", "9"]}},
            "planets_by_id": {
                "8": {"id": "8", "system_id": "92", "surveyed_known": True, "surveyed": False},
                "9": {"id": "9", "system_id": "92", "surveyed_known": True, "surveyed": True},
            },
            "surveyed_planets_known": True, "surveyed_planet_ids": ["9"],
        },
    }


@pytest.mark.parametrize("intent,model", [
    (BODY, SurveyBodyIntent), (SYSTEM, SurveySystemIntent), (ENTER, EnterSystemIntent),
])
def test_discriminated_intents_accept_dicts_and_models(intent, model):
    parsed = TypeAdapter(CompletionIntent).validate_python(intent)
    assert isinstance(parsed, model)
    normalized, authorization, digest = normalize_completion(parsed, TimeAuthorization(**TIME))
    assert normalized == intent
    assert authorization == TIME
    assert len(digest) == 64


def test_preconstructed_models_cannot_bypass_authorization_or_id_validation():
    bad_intent = SurveyBodyIntent.model_construct(**{**BODY, "fleet_id": 3})
    with pytest.raises(ValidationError):
        normalize_completion(bad_intent, TIME)
    bad_time = TimeAuthorization.model_construct(**{**TIME, "allow_time_advance": 1})
    with pytest.raises(ValidationError):
        normalize_completion(BODY, bad_time)


@pytest.mark.parametrize("identifier", [
    None, 3, True, 3.0, "", "03", "+3", "-3", "3.0", " 3", "3 ", "３", "3\n",
    "4294967295", "9999999999", "10000000000",
])
@pytest.mark.parametrize("field", ["fleet_id", "planet_id", "system_id"])
def test_all_ids_reject_coercion_noncanonical_and_sentinel(identifier, field):
    with pytest.raises(ValidationError):
        normalize_completion({**BODY, field: identifier}, TIME)


@pytest.mark.parametrize("identifier", ["0", "1", "4294967294"])
def test_ids_include_zero_and_highest_non_sentinel(identifier):
    assert normalize_completion({**BODY, "planet_id": identifier}, TIME)[0]["planet_id"] == identifier


@pytest.mark.parametrize("intent", [
    {}, {**BODY, "kind": "survey_planet"}, {**BODY, "script": "cash 1"},
    {**ENTER, "planet_ids": ["8"]}, {**SYSTEM, "planet_id": "8"},
    {key: value for key, value in BODY.items() if key != "planet_id"},
])
def test_intents_forbid_unknown_kinds_missing_and_extra_fields(intent):
    with pytest.raises(ValidationError):
        normalize_completion(intent, TIME)


@pytest.mark.parametrize("bodies", [[], ["8", "8"], [8], ["08"], {"8"}, "8", None, [str(n) for n in range(101)]])
def test_system_freeze_requires_one_to_hundred_unique_ordered_exact_ids(bodies):
    with pytest.raises(ValidationError):
        normalize_completion({**SYSTEM, "planet_ids": bodies}, TIME)


def test_system_ids_frozen_preserve_input_order_and_isolate_caller_mutation():
    bodies = ["9", "8"]
    model = SurveySystemIntent(**{**SYSTEM, "planet_ids": bodies})
    bodies.append("10")
    assert model.planet_ids == ("9", "8")
    with pytest.raises(ValidationError):
        model.planet_ids = ("8",)
    assert normalize_completion(model, TIME)[0]["planet_ids"] == ["9", "8"]
    assert normalize_completion({**SYSTEM, "planet_ids": [str(n) for n in range(100)]}, TIME)[0]


@pytest.mark.parametrize("field", ["allow_time_advance", "final_pause"])
@pytest.mark.parametrize("value", [False, 0, 1, "true", "yes", None])
def test_time_flags_require_explicit_true_boolean(field, value):
    with pytest.raises(ValidationError):
        normalize_completion(BODY, {**TIME, field: value})


@pytest.mark.parametrize("field", ["max_game_days", "max_wall_seconds"])
@pytest.mark.parametrize("value", [0, -1, 3601, True, False, "90", 90.0, None])
def test_time_bounds_are_strict_integer(field, value):
    with pytest.raises(ValidationError):
        normalize_completion(BODY, {**TIME, field: value})


@pytest.mark.parametrize("field", list(TIME))
def test_time_authorization_has_no_implicit_defaults(field):
    with pytest.raises(ValidationError):
        normalize_completion(BODY, {key: value for key, value in TIME.items() if key != field})


def test_time_authorization_forbids_extras_and_accepts_boundary_integers():
    with pytest.raises(ValidationError):
        normalize_completion(BODY, {**TIME, "speed": 5})
    assert normalize_completion(BODY, {**TIME, "max_game_days": 1, "max_wall_seconds": 3600})[1]


def test_normalization_hash_covers_contract_intent_authorization_and_snapshot():
    normalized, authorization, digest = normalize_completion(SYSTEM, TIME, "snapshot:abc_1")
    payload = {"type": "natural_completion_v1", "intent": normalized, "time_authorization": authorization,
               "expected_snapshot": "snapshot:abc_1"}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                        allow_nan=False).encode()).hexdigest()
    assert digest == expected
    assert normalize_completion(dict(reversed(list(SYSTEM.items()))), dict(reversed(list(TIME.items()))),
                                "snapshot:abc_1")[2] == digest
    for intent, time, snapshot in (
        ({**SYSTEM, "planet_ids": ["8", "9"]}, TIME, "snapshot:abc_1"),
        ({**SYSTEM, "fleet_id": "4"}, TIME, "snapshot:abc_1"),
        (SYSTEM, {**TIME, "max_game_days": 91}, "snapshot:abc_1"),
        (SYSTEM, TIME, None),
    ):
        assert normalize_completion(intent, time, snapshot)[2] != digest


@pytest.mark.parametrize("snapshot", ["", "x" * 129, "snapshot\n", "空", 1, True, "with space"])
def test_expected_snapshot_matches_existing_plan_bounded_format(snapshot):
    with pytest.raises(ValueError, match="expected_snapshot"):
        normalize_completion(BODY, TIME, snapshot)


def test_survey_body_false_then_true_from_ledger_not_fleet_orders(state):
    assert evaluate_goal(state, BODY)["value"] is False
    state["fleets_by_id"]["3"]["current_order"] = {"survey_planet_order": {"progress": 1.0}}
    assert evaluate_goal(state, BODY)["value"] is False
    state["space"]["planets_by_id"]["8"]["surveyed"] = True
    state["space"]["surveyed_planet_ids"].append("8")
    assert evaluate_goal(state, BODY)["value"] is True
    state["fleets_by_id"]["3"]["current_order"] = {}
    assert evaluate_goal(state, BODY)["value"] is True


def test_survey_body_ignores_other_bodies_unknown_survey_but_requires_membership(state):
    state["space"]["planets_by_id"]["9"].pop("surveyed_known")
    assert evaluate_goal(state, BODY)["value"] is False
    state["space"]["planets_by_id"].pop("9")
    assert evaluate_goal(state, BODY)["value"] is None


def test_survey_system_requires_all_frozen_bodies_and_preserves_authorized_order(state):
    result = evaluate_goal(state, SYSTEM)
    assert result["value"] is False
    assert result["evidence"]["surveyed_planet_ids"] == ["9"]
    assert result["evidence"]["unsurveyed_planet_ids"] == ["8"]
    state["space"]["surveyed_planet_ids"].append("8")
    state["space"]["planets_by_id"]["8"]["surveyed"] = True
    result = evaluate_goal(state, SYSTEM)
    assert result["value"] is True
    assert result["evidence"]["surveyed_planet_ids"] == ["9", "8"]


@pytest.mark.parametrize("bodies", [["8"], ["8", "9", "10"]])
def test_frozen_membership_change_is_unknown_and_never_auto_expands(state, bodies):
    state["space"]["systems_by_id"]["92"]["planet_ids"] = bodies
    state["space"]["planets_by_id"] = {
        body: {"id": body, "system_id": "92", "surveyed_known": True, "surveyed": True} for body in bodies
    }
    result = evaluate_goal(state, SYSTEM)
    assert result["value"] is None
    assert result["reason"] == "frozen_system_membership_changed"


def test_current_system_membership_order_can_change_without_changing_frozen_set(state):
    state["space"]["systems_by_id"]["92"]["planet_ids"].reverse()
    assert evaluate_goal(state, SYSTEM)["value"] is False


@pytest.mark.parametrize("intent", [BODY, SYSTEM, ENTER])
@pytest.mark.parametrize("field,value", [
    ("owner", 1), ("owner", True), ("controller", "1"), ("mobile", False),
    ("combat", {}), ("combat", {"start_date": "2200.01.01"}),
    ("orders_known", False), ("orders_known", "yes"), ("system_id", None), ("system_id", 91),
    ("system_id", "091"), ("system_id", "4294967295"),
])
def test_unsafe_incomplete_and_noncanonical_fleet_state_remains_unknown(state, intent, field, value):
    state["fleets_by_id"]["3"][field] = value
    result = evaluate_goal(state, intent)
    assert result["value"] is None
    assert result["unknown_fields"]


@pytest.mark.parametrize("intent", [BODY, SYSTEM, ENTER])
def test_missing_fleet_ownership_and_unknown_order_fields_do_not_become_false(state, intent):
    for modified in ({}, {**state, "metadata": {}}, {**state, "metadata": {"missing_fields": ["fleets"]}},
                     {**state, "fleets_by_id": {}}):
        assert evaluate_goal(modified, intent)["value"] is None


@pytest.mark.parametrize("intent", [BODY, SYSTEM])
@pytest.mark.parametrize("ship_class", ["shipclass_military", "shipclass_constructor", "shipclass_colonizer"])
def test_survey_completion_requires_exact_science_fleet_even_after_survey(state, intent, ship_class):
    state["fleets_by_id"]["3"]["ship_class"] = ship_class
    state["space"]["planets_by_id"]["8"]["surveyed"] = True
    state["space"]["surveyed_planet_ids"].append("8")
    assert evaluate_goal(state, intent)["value"] is None


@pytest.mark.parametrize("intent", [BODY, SYSTEM, ENTER])
@pytest.mark.parametrize("field,value", [
    ("id", "93"), ("visited", False), ("visited", 1), ("planets_known", False),
    ("planet_ids", []), ("planet_ids", ["8", "8"]), ("planet_ids", [8, "9"]),
    ("planet_ids", ["08", "9"]), ("planet_ids", None),
])
def test_complete_visited_canonical_system_membership_required(state, intent, field, value):
    state["space"]["systems_by_id"]["92"][field] = value
    assert evaluate_goal(state, intent)["value"] is None


@pytest.mark.parametrize("intent", [BODY, SYSTEM, ENTER])
@pytest.mark.parametrize("field,value", [("id", "10"), ("system_id", "93"), ("system_id", 92)])
def test_every_system_body_must_match_reverse_membership(state, intent, field, value):
    state["space"]["planets_by_id"]["9"][field] = value
    assert evaluate_goal(state, intent)["value"] is None


@pytest.mark.parametrize("intent", [BODY, SYSTEM, ENTER])
def test_unlisted_reverse_system_body_is_contradiction(state, intent):
    state["space"]["planets_by_id"]["10"] = {"id": "10", "system_id": "92"}
    assert evaluate_goal(state, intent)["value"] is None


@pytest.mark.parametrize("intent", [BODY, ENTER])
def test_exact_target_body_is_required_in_authorized_system(state, intent):
    assert evaluate_goal(state, {**intent, "planet_id": "10"})["value"] is None
    assert evaluate_goal(state, {**intent, "system_id": "93"})["value"] is None


@pytest.mark.parametrize("intent", [BODY, SYSTEM])
@pytest.mark.parametrize("field,value", [
    ("surveyed_known", False), ("surveyed_known", 1), ("surveyed", None), ("surveyed", 1),
    ("surveyed", "yes"), ("surveyed", True),
])
def test_unknown_survey_flags_and_ledger_disagreement_are_unknown(state, intent, field, value):
    state["space"]["planets_by_id"]["8"][field] = value
    assert evaluate_goal(state, intent)["value"] is None


@pytest.mark.parametrize("intent", [BODY, SYSTEM])
@pytest.mark.parametrize("ledger", [None, ["9", "9"], [9], ["09"], ["8", "9"]])
def test_missing_malformed_and_contradictory_global_survey_ledger(state, intent, ledger):
    state["space"]["surveyed_planet_ids"] = ledger
    assert evaluate_goal(state, intent)["value"] is None


def test_survey_unknown_dominates_a_known_unsurveyed_body(state):
    state["space"]["planets_by_id"]["9"]["surveyed_known"] = False
    assert evaluate_goal(state, {**SYSTEM, "planet_ids": ["8", "9"]})["value"] is None


def test_enter_system_only_proves_membership_and_does_not_require_survey_ledger(state):
    assert evaluate_goal(state, ENTER)["value"] is False
    fleet = state["fleets_by_id"]["3"]
    fleet["system_id"] = "92"
    fleet["ship_class"] = "shipclass_military"
    fleet["current_order"] = {"move_to": {"coordinate": {"x": 500, "y": 500, "origin": 92}}}
    state["space"]["surveyed_planets_known"] = False
    state["space"].pop("surveyed_planet_ids")
    result = evaluate_goal(state, ENTER)
    assert result["value"] is True
    assert result["evidence"]["completion_scope"] == "system_entry_only"


def test_goal_evaluation_is_pure_and_model_input_supported(state):
    original = deepcopy(state)
    evaluate_goal(state, SurveyBodyIntent(**BODY))
    assert state == original


@pytest.mark.parametrize("date,expected", [
    ("0.01.01", 0), ("0.1.1", 0), ("0.01.30", 29), ("0.02.01", 30),
    ("0.12.30", 359), ("1.01.01", 360), ("2200.01.01", 792000),
])
def test_game_day_uses_exact_thirty_day_months(date, expected):
    assert game_day(date) == expected


@pytest.mark.parametrize("date", [
    None, True, 22000101, "", "-1.01.01", "+1.01.01", "1.00.01", "1.13.01", "1.01.00",
    "1.01.31", "2200.12.31", "1.01.01\n", "１.01.01", "1.1.1.1", "1-01-01", "1.001.01",
])
def test_game_day_rejects_non_calendar_and_non_ascii_dates(date):
    with pytest.raises(ValueError):
        game_day(date)
