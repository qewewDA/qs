"""A compact receipt reuses proof and cannot cause a second game observation."""
import copy
import json
import math
from pathlib import Path

import pytest

from stellaris_autopilot.action_context import verified_context
from stellaris_autopilot.assistant_prompts import render_prompt
from stellaris_autopilot.observe import Observation


def observations():
    state = {
        "date": "2200.01.02", "country_id": "0",
        "resources": {"energy": 75, "minerals": 10, "alloys": None},
        "metadata": {"source": "C:/private/secret-save.sav"},
        "planets_by_id": {
            "3": {
                "id": "3", "name": "Earth", "owner": "0", "colony": "6",
                "district_levels": {"district_city": 5},
                "zones_by_id": {
                    "4": {"id": "4", "buildings": [{"id": "17"}]},
                    "5": {"id": "5", "buildings": [{"id": "18"}]},
                },
                "metadata": {"source": "C:/private/planet-source"},
            },
            "9": {"id": "9", "name": "Unrelated planet"},
        },
        "fleets_by_id": {
            "1": {"id": "1", "orders_known": True, "orders": [{"kind": "move"}]},
            "2": {"id": "2", "name": "Unrelated fleet"},
        },
        "construction": {"queues_by_id": {
            "8": {"id": "8", "items": [{"id": "31"}]},
            "9": {"id": "9", "items": [{"id": "32"}]},
        }},
    }
    before = copy.deepcopy(state)
    before["date"] = "2200.01.01"
    before["resources"]["energy"] = 100
    return Observation(before, "before", 10.0), Observation(state, "after", 11.0)


def test_receipt_uses_verified_snapshot_without_another_read(monkeypatch):
    def unexpected_read(*_args, **_kwargs):
        pytest.fail("Formatting an existing proof must not read files or observe the game")

    before, after = observations()
    monkeypatch.setattr("builtins.open", unexpected_read)
    monkeypatch.setattr(Path, "read_text", unexpected_read)
    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    result = verified_context(before, after, "queue_building", {"planet_id": "3"})
    assert result["dispatch_state"] == "verified"
    assert result["context"]["snapshot"] == "after"
    assert result["context"]["observed_at"] == 11.0
    assert result["context"]["date"] == "2200.01.02"


def test_receipt_selects_only_referenced_objects_zone_and_queue_without_paths():
    before, after = observations()
    result = verified_context(before, after, "upgrade_building", {
        "planet_id": "3", "fleet_id": "1", "zone_id": "4", "queue_id": "8",
    })
    context = result["context"]
    assert set(context["objects"]) == {"planet_id", "fleet_id"}
    assert context["objects"]["planet_id"]["id"] == "3"
    assert set(context["objects"]["planet_id"]["zones_by_id"]) == {"4"}
    assert context["objects"]["fleet_id"]["id"] == "1"
    assert context["queue"] == {"id": "8", "items": [{"id": "31"}]}
    serialized = json.dumps(result)
    assert "private" not in serialized and '"source"' not in serialized
    assert "Unrelated" not in serialized
    assert "planets_by_id" not in context and "fleets_by_id" not in context


def test_receipt_is_independent_of_its_inputs_in_both_directions():
    before, after = observations()
    payload = {"planet_id": "3", "zone_id": "4", "queue_id": "8"}
    runtime = {"paused": True}
    original = copy.deepcopy((before, after, payload, runtime))
    context = verified_context(before, after, "upgrade_building", payload, runtime)["context"]
    assert (before, after, payload, runtime) == original
    context["resources"]["energy"] = -1
    context["objects"]["planet_id"]["zones_by_id"]["4"]["buildings"].clear()
    context["queue"]["items"].clear()
    context["target_refs"]["planet_id"] = "other"
    assert (before, after, payload, runtime) == original


def test_receipt_does_not_infer_objects_or_missing_resource_balances():
    before, after = observations()
    after.state.pop("resources")
    context = verified_context(before, after, "queue_ship", {"planet_id": "unknown", "queue_id": "missing"})["context"]
    assert context["resources"] is None
    assert context["resource_change"] == {}
    assert context["resource_change_known"] is False
    assert context["objects"] == {}
    assert context["queue"] is None
    assert context["paused"] is None and context["pause_known"] is False


def test_receipt_falls_back_to_physical_planet_index_without_copying_space():
    before, after = observations()
    planets = after.state.pop("planets_by_id")
    after.state["space"] = {"planets_by_id": planets, "unrelated": {"name": "secret"}}
    context = verified_context(before, after, "survey_planet", {"planet_id": "3"})["context"]
    assert context["objects"]["planet_id"]["id"] == "3"
    assert "space" not in context


def test_resource_difference_is_an_observed_balance_not_a_price_quote():
    before, after = observations()
    before.state["resources"]["income_and_purchase"] = 100
    after.state["resources"]["income_and_purchase"] = 120
    context = verified_context(before, after, "queue_building")["context"]
    assert context["resource_change"] == {"energy": -25, "income_and_purchase": 20}
    assert context["resources"]["alloys"] is None
    assert "not an effective-price quote" in context["resource_change_note"]
    assert not ({"price", "cost", "effective_cost"} & context.keys())


@pytest.mark.parametrize("value", [None, True, False, "10", float("nan"), float("inf"), -float("inf")])
def test_invalid_balances_never_become_a_zero_or_numeric_delta(value):
    before, after = observations()
    before.state["resources"] = {"energy": value}
    after.state["resources"] = {"energy": 1}
    context = verified_context(before, after, "queue_ship")["context"]
    assert context["resource_change"] == {}
    assert context["resource_change_known"] is False
    assert context["resource_change_unknown"] == ["energy"]
    before.state["resources"] = {"energy": 1}
    after.state["resources"] = {"energy": value}
    assert verified_context(before, after, "queue_ship")["context"]["resource_change"] == {}


def test_missing_balances_are_explicitly_unknown_without_synthetic_zero_deltas():
    before, after = observations()
    before.state["resources"] = {"old_only": 2, "stable": 3}
    after.state["resources"] = {"new_only": 1, "stable": 3}
    context = verified_context(before, after, "queue_ship")["context"]
    assert context["resource_change"] == {}
    assert context["resource_change_known"] is False
    assert set(context["resource_change_unknown"]) == {"old_only", "new_only"}


def test_complete_unchanged_balances_prove_no_observed_change():
    before, after = observations()
    before.state["resources"] = after.state["resources"] = {"energy": 3, "minerals": 0}
    context = verified_context(before, after, "queue_ship")["context"]
    assert context["resource_change"] == {}
    assert context["resource_change_known"] is True
    assert context["resource_change_unknown"] == []


@pytest.mark.parametrize("old,new", [(10**500, 10**500 + 1), (-1e308, 1e308)])
def test_numeric_edges_cannot_crash_a_receipt_after_the_action_is_verified(old, new):
    before, after = observations()
    before.state["resources"] = {"energy": old}
    after.state["resources"] = {"energy": new}
    result = verified_context(before, after, "queue_ship")
    delta = result["context"]["resource_change"]
    if "energy" in delta:
        assert type(delta["energy"]) in (int, float)
        assert math.isfinite(delta["energy"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("runtime", [None, {}, {"paused": None}, {"paused": 1}, {"paused": "true"}])
def test_pause_is_unknown_without_strict_boolean_proof(runtime):
    before, after = observations()
    context = verified_context(before, after, "queue_ship", runtime=runtime)["context"]
    assert context["paused"] is None and context["pause_known"] is False


@pytest.mark.parametrize("paused", [True, False])
def test_explicit_runtime_pause_proof_is_preserved(paused):
    before, after = observations()
    context = verified_context(before, after, "queue_ship", runtime={"paused": paused})["context"]
    assert context["paused"] is paused and context["pause_known"] is True


@pytest.mark.parametrize("kind", ["set_paused", "demolish_building", "cancel_construction", "fleet_stop", "probe_connection"])
def test_immediate_actions_complete_their_verified_goal(kind):
    before, after = observations()
    assert verified_context(before, after, kind)["goal_state"] == "completed"


@pytest.mark.parametrize("kind", ["queue_ship", "queue_building", "upgrade_building", "convert_zone", "survey_planet", "build_station", "colonize_planet", "fleet_move", "hold_military_fleets", "select_research"])
def test_dispatch_verification_does_not_claim_natural_completion(kind):
    before, after = observations()
    result = verified_context(before, after, kind)
    assert result["dispatch_state"] == "verified" and result["goal_state"] == "pending"


def test_onboarding_reuses_proof_but_invalidates_state_for_relevant_changes():
    prompt = render_prompt(existing_game=True)
    assert "直接使用回执附带的 context" in prompt
    assert "用户手动操作、时间推进" in prompt
    assert "暂停未知或正在运行" in prompt
    assert "每步合法性、付费与后置条件由服务端重新核验" in prompt
    assert "同一舰队的多条未完成订单可能相互覆盖" in prompt
    assert "prepare_task" not in prompt and "execute_task" not in prompt


def test_onboarding_distinguishes_unknown_from_empty():
    prompt = render_prompt(existing_game=True)
    assert "未知、已知为空、已知有值" in prompt
    assert "queue_known=false" in prompt
    assert "buildings_known=false" in prompt
    assert "不能说成空槽、空队列或空建筑" in prompt
