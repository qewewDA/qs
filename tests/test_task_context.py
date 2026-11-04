import copy
import json

from stellaris_autopilot.action_context import current_plan_context
from stellaris_autopilot.observe import Observation


def observation():
    return Observation({
        "country_id": "0", "date": "2200.01.01", "resources": {"alloys": 900},
        "metadata": {"source": "C:/private/save", "missing_fields": ["fleets"]},
        "planets_by_id": {"3": {"id": "3", "build_queue": "8", "zones_known": False}},
        "fleets_by_id": {"10": {"id": "10"}},
        "shipbuilding": {"designs_known": True, "shipyards_known": True, "species_known": False,
                         "designs_by_id": {"7": {"id": "7", "name": "Corvette"}, "9": {"id": "9"}},
                         "shipyards_by_id": {"12": {"id": "12", "queue_id": "8"}}},
        "construction": {"known": True, "queues_by_id": {
            "8": {"id": "8", "items_known": False, "items": None}, "9": {"id": "9"}}},
        "research_status": {"known": False, "completed_known": False, "completed_technologies": None,
                            "areas": {"physics": {"queue_known": False, "queue": None},
                                      "society": {"queue_known": True, "queue": []}}},
    }, "now", 100)


def test_stale_context_selects_exact_ship_design_shipyard_and_queue_only():
    context = current_plan_context(observation(), [{"kind": "queue_ship", "starbase_id": "12", "design_id": "7"}])
    assert set(context["shipbuilding"]["designs_by_id"]) == {"7"}
    assert set(context["shipbuilding"]["shipyards_by_id"]) == {"12"}
    assert set(context["queues_by_id"]) == {"8"}
    assert context["queues_by_id"]["8"]["items_known"] is False
    assert context["queues_by_id"]["8"]["items"] is None
    assert '"source":' not in json.dumps(context) and "C:/private/save" not in json.dumps(context)
    assert "dispatch_state" not in context and "resource_change" not in context


def test_stale_context_preserves_unknown_research_and_returns_only_requested_area():
    context = current_plan_context(observation(), [{"kind": "select_research", "area": "physics",
                                                    "technology_id": "tech_shields_2"}])
    research = context["research_status"]
    assert research["known"] is False and research["completed_technologies"] is None
    assert research["areas"] == {"physics": {"queue_known": False, "queue": None}}


def test_stale_context_missing_requested_objects_remain_null_and_does_not_mutate_snapshot():
    observed = observation()
    before = copy.deepcopy(observed)
    context = current_plan_context(observed, [{"kind": "survey_planet", "fleet_id": "99", "planet_id": "3"}])
    assert context["targets"]["fleet_id:99"] is None
    assert context["targets"]["planet_id:3"]["zones_known"] is False
    assert context["missing_fields"] == ["fleets"]
    context["targets"]["planet_id:3"]["id"] = "modified"
    context["resources"]["alloys"] = 0
    assert observed == before
