import pytest

from stellaris_autopilot.fleet_observe import observe_space
from stellaris_autopilot.pdx import loads


def _root(country="visited_objects={92 247} surveyed_deposit_holders={{type=0 id=3}}", systems=None, planets=None):
    if systems is None:
        systems = '92={name={key=NAME_Sol} planet=3 hyperlane={to=247}} 247={planet=2124} 500={planet=999}'
    if planets is None:
        planets = ('3={name={key=NAME_Earth} coordinate={origin=92} owner=0 colony=0 planet_class=pc_continental} '
                   '2124={coordinate={origin=247} planet_class=pc_g_star} 999={coordinate={origin=500}}')
    return loads(f"country={{0={{{country}}}}} galactic_object={{{systems}}} planets={{planet={{{planets}}}}}")


def test_space_uses_visited_ledger_and_keeps_physical_colony_namespaces_separate():
    result = observe_space(_root(), "0")
    assert result["visited_system_ids"] == ["92", "247"]
    assert set(result["systems_by_id"]) == {"92", "247"}
    assert set(result["planets_by_id"]) == {"3", "2124"}
    earth = result["planets_by_id"]["3"]
    assert earth["id"] == "3" and earth["colony"] == "0" and earth["system_id"] == "92"
    assert earth["surveyed_known"] is True and earth["surveyed"] is True
    assert result["planets_by_id"]["2124"]["surveyed"] is False
    assert "owner" not in result["planets_by_id"]["2124"]
    assert result["systems_by_id"]["92"]["hyperlane_system_ids"] == ["247"]


def test_missing_visited_ledger_does_not_reveal_all_galaxy_bodies():
    result = observe_space(_root(country=""), "0")
    assert result["visited_systems_known"] is False
    assert result["systems_by_id"] == result["planets_by_id"] == {}


def test_missing_survey_ledger_is_unknown():
    result = observe_space(_root(country="visited_objects={92 247}"), "0")
    assert result["surveyed_planets_known"] is False
    assert result["planets_by_id"]["3"]["surveyed"] is None


def test_current_format_hyperlane_anonymous_list_is_observed():
    result = observe_space(_root(systems="92={planet=3 hyperlane={{to=247} {to=500}}}"), "0")
    assert result["systems_by_id"]["92"]["hyperlane_system_ids"] == ["247", "500"]


def test_ambiguous_system_or_planet_coordinates_block_membership():
    for root in (_root(systems="92={planet=3} 92={planet=3}"),
                 _root(planets="3={coordinate={origin=92} coordinate={origin=92}}"),
                 _root(planets="3={coordinate={origin=247}}")):
        assert "3" not in observe_space(root, "0")["planets_by_id"]


def test_malformed_visited_or_survey_ledger_never_becomes_known_empty():
    for value in ("visited_objects={92 92}", "visited_objects={4294967295}", "visited_objects={id=92}",
                  "visited_objects={92} visited_objects={92}"):
        result = observe_space(_root(country=value), "0")
        assert result["visited_systems_known"] is False
    for value in ("{type=0 id=3} {type=0 id=3}", "{type=3 id=3}", "{type=0}", "0={type=0 id=3}"):
        result = observe_space(_root(country=f"visited_objects={{92}} surveyed_deposit_holders={{{value}}}"), "0")
        assert result["surveyed_planets_known"] is False


def test_current_format_system_starbase_slots_preserve_positive_and_explicit_empty_references():
    result = observe_space(_root(systems="92={planet=3 starbases={0}} 247={planet=2124 starbases={4294967295}}"), "0")
    sol, other = result["systems_by_id"]["92"], result["systems_by_id"]["247"]
    assert sol["starbase_known"] is True and sol["starbase"] == "0"
    assert other["starbase_known"] is True and other["starbase"] is None
    for system in (sol, other):
        assert system["owner_known"] is system["controller_known"] is False
        assert "owner" not in system and "controller" not in system


@pytest.mark.parametrize("key", ["owner", "controller", "starbase"])
@pytest.mark.parametrize("value,expected", [("0", "0"), ("16777217", "16777217"), ("4294967295", None)])
def test_system_unique_references_keep_full_handles_and_explicit_unset(key, value, expected):
    system = observe_space(_root(systems=f"92={{planet=3 {key}={value}}}"), "0")["systems_by_id"]["92"]
    assert system[f"{key}_known"] is True and system[key] == expected


@pytest.mark.parametrize("key", ["owner", "controller", "starbase"])
@pytest.mark.parametrize("source", ["", "{key}=0 {key}=0", "{key}=0 {key}=1", "{key}=-1",
                                    "{key}=4294967296", "{key}=yes", "{key}={{id=0}}"])
def test_system_missing_or_ambiguous_reference_stays_unknown(key, source):
    system = observe_space(_root(systems=f"92={{planet=3 {source.format(key=key)}}}"), "0")["systems_by_id"]["92"]
    assert system[f"{key}_known"] is False and key not in system


@pytest.mark.parametrize("source", ["starbases={0 0}", "starbases={0 1}", "starbases={0 -1}",
                                    "starbases={id=0}", "starbases=0", "starbases={0} starbases={0}",
                                    "starbase=0 starbases={0}"])
def test_ambiguous_system_starbase_slots_never_select_a_convenient_first_value(source):
    system = observe_space(_root(systems=f"92={{planet=3 {source}}}"), "0")["systems_by_id"]["92"]
    assert system["starbase_known"] is False and "starbase" not in system


@pytest.mark.parametrize("values,expected", [("", None), ("4294967295 4294967295", None),
                                           ("4294967295 16777217", "16777217")])
def test_complete_system_starbase_slots_distinguish_empty_from_missing(values, expected):
    system = observe_space(_root(systems=f"92={{planet=3 starbases={{{values}}}}}"), "0")["systems_by_id"]["92"]
    assert system["starbase_known"] is True and system["starbase"] == expected
