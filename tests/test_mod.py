from pathlib import Path

import pytest

from stellaris_autopilot.cli import install_mod
from stellaris_autopilot.pdx import PDXNode, loads


MOD = Path(__file__).resolve().parents[1] / "mod" / "stellaris_ai_autopilot"


def walk(node):
    if isinstance(node, PDXNode):
        for key, value in node.entries:
            yield key, value
            yield from walk(value)


def events():
    parsed = loads((MOD / "events" / "saa_events.txt").read_text(encoding="utf-8-sig"))
    assert parsed.get("namespace") == "saa"
    result = {event.get("id"): event for event in parsed.get_all("country_event")}
    assert len(result) == len(parsed.get_all("country_event")), "Duplicate event IDs overwrite game definitions"
    return result


def test_mod_installer_copies_complete_bridge_and_generates_absolute_launcher(tmp_path, capsys):
    mod_dir = tmp_path / "Paradox Interactive" / "Stellaris" / "mod"
    install_mod(mod_dir)
    installed = mod_dir / "stellaris_ai_autopilot"
    assert (installed / "descriptor.mod").is_file()
    assert (installed / "events" / "saa_events.txt").read_bytes() == (MOD / "events" / "saa_events.txt").read_bytes()
    assert (installed / "common" / "on_actions" / "saa_on_actions.txt").is_file()
    assert (installed / "common" / "scripted_effects" / "saa_effects.txt").is_file()
    launcher = loads((mod_dir / "stellaris_ai_autopilot.mod").read_text(encoding="utf-8"))
    assert Path(launcher.get("path")) == installed.resolve()
    assert launcher.get("name") == loads((installed / "descriptor.mod").read_text()).get("name")
    assert "installed" in capsys.readouterr().out


def test_reinstall_never_overwrites_user_modifications(tmp_path, capsys):
    install_mod(tmp_path)
    destination = tmp_path / "stellaris_ai_autopilot" / "descriptor.mod"
    destination.write_text("user content", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        install_mod(tmp_path)
    assert destination.read_text(encoding="utf-8") == "user content"


def test_console_events_are_opt_in_and_guarded_in_effect_body():
    parsed = events()
    assert {"saa.1", "saa.2", "saa.20"} <= parsed.keys()
    for event in parsed.values():
        assert event.get("is_triggered_only") == "yes"
        # Direct console invocation may bypass trigger evaluation. The actual
        # effect body must independently reject AI, multiplayer and ironman.
        effect = event.get("immediate")
        conditional = effect.get("if")
        limit = conditional.get("limit")
        for condition in ("is_ai", "is_multiplayer", "is_ironman"):
            assert limit.get(condition) == "no"
        if event.get("id") not in {"saa.1", "saa.2"}:
            assert limit.get("has_country_flag") == "saa_bridge_enabled"


def test_defense_orders_only_owned_noncombat_military_fleets():
    event = events()["saa.20"]
    body = event.get("immediate").get("if")
    fleet = body.get("every_owned_fleet")
    assert fleet is not None
    assert fleet.get("limit").get("is_in_combat") == "no"
    assert fleet.get("limit").get("is_mobile") == "yes"
    assert fleet.get("limit").get("is_ship_class") == "shipclass_military"
    target_name = body.get("capital_scope").get("save_event_target_as")
    assert fleet.get("queue_actions").get("move_to") == f"event_target:{target_name}"
    assert body.get("limit").get("exists") == "capital_scope"


def test_monthly_hook_observes_only_and_does_not_order_fleets():
    hook = loads((MOD / "common" / "on_actions" / "saa_on_actions.txt").read_text())
    monthly_events = list(hook.get("on_monthly_pulse_country").get("events").values())
    assert monthly_events == ["saa.10"]
    pulse = events()["saa.10"]
    keys = {key for key, _ in walk(pulse)}
    assert "every_owned_fleet" not in keys and "queue_actions" not in keys
    assert "saa_log_observation" in keys


def test_no_resource_grants_free_construction_or_research_cheats_in_bridge():
    forbidden = {"add_resource", "add_resources", "add_monthly_resource_mult", "add_building",
                 "add_modifier", "research_technology", "create_fleet", "create_ship"}
    for path in MOD.rglob("*.txt"):
        if "localisation" in path.parts:
            continue
        parsed = loads(path.read_text(encoding="utf-8-sig"))
        assert not ({key for key, _ in walk(parsed)} & forbidden), path


def test_localisation_has_required_utf8_bom_and_language_header():
    files = list((MOD / "localisation").rglob("*.yml"))
    assert files
    for path in files:
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), path
        header = raw.decode("utf-8-sig").splitlines()[0]
        assert header.startswith("l_") and header.endswith(":"), path
