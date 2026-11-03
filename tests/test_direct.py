from dataclasses import replace
import json
from pathlib import Path

import pytest

import stellaris_autopilot.direct as direct
from stellaris_autopilot.direct import (
    DUMP_NAME,
    INBOX_NAME,
    DirectConfig,
    atomic_publish,
    disarm_inbox,
    load_direct_config,
    ownership_header,
    publish_observation,
    render_action,
    render_probe,
    validate_direct_config,
)
from stellaris_autopilot.pdx import loads


SESSION = "1" * 32
TOKEN = "2" * 32


@pytest.fixture
def config(tmp_path):
    user_dir = tmp_path / "user directory"
    user_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return DirectConfig(
        user_dir=user_dir, state_dir=state_dir, country_id="0", campaign_id="campaign-a",
        verified_game_version="4.test", session_token=SESSION, allowed_actions=("probe_connection",),
        single_player_confirmed=True, non_ironman_confirmed=True,
    )


def raw_config():
    return {
        "user_dir": "user directory", "state_dir": "state", "country_id": "0",
        "campaign_id": "campaign-a", "verified_game_version": "4.test", "session_token": SESSION,
        "allowed_actions": ["probe_connection"],
        "single_player_confirmed": True, "non_ironman_confirmed": True,
    }


def write_config(tmp_path, raw):
    path = tmp_path / "direct.json"
    path.write_text(json.dumps(raw), encoding="utf-8-sig")
    return path


def test_direct_configuration_has_separate_schema_and_fixed_paths(tmp_path, monkeypatch):
    path = write_config(tmp_path, raw_config())
    monkeypatch.chdir(tmp_path.parent)
    config = load_direct_config(path)
    assert config.user_dir == tmp_path / "user directory"
    assert config.state_dir == tmp_path / "state"
    assert config.inbox_path == config.user_dir / INBOX_NAME
    assert config.dump_path == config.user_dir / "save games" / DUMP_NAME
    assert config.allowed_actions == ("probe_connection",)
    assert (config.max_snapshot_age_seconds, config.verification_timeout_seconds, config.poll_seconds) == (180, 60, 1)
    assert not config.inbox_path.exists()


@pytest.mark.parametrize("field,value", [
    ("save_path", "something.sav"), ("resolution", [1920, 1080]), ("console_key", "`"),
    ("command", "cash 10000"), ("dump_path", "../../other.txt"),
    ("allowed_actions", ["defend_capital"]), ("allowed_actions", ["probe_connection", "probe_connection"]),
    ("allowed_actions", {}), ("allowed_actions", [{}]), ("country_id", 0), ("country_id", True),
    ("allowed_orders", ["research_technology"]), ("allowed_orders", ["fleet_wait", "fleet_wait"]),
    ("allowed_orders", {}), ("allowed_orders", [True]),
    ("allowed_native_actions", ["add_buildable"]), ("allowed_native_actions", {}),
    ("allowed_native_actions", ["select_research", "select_research"]),
    ("country_id", "１２"), ("country_id", "0\neffect cash = 1"), ("country_id", "1" * 11),
    ("session_token", "x" * 32), ("session_token", "1" * 31 + "\n"), ("session_token", []),
    ("campaign_id", " "), ("verified_game_version", "unknown"), ("verified_game_version", None),
    ("single_player_confirmed", "yes"), ("non_ironman_confirmed", 1),
    ("poll_seconds", 0), ("poll_seconds", 31), ("poll_seconds", True),
    ("verification_timeout_seconds", 601), ("max_snapshot_age_seconds", float("nan")),
    ("max_snapshot_age_seconds", float("inf")), ("max_snapshot_age_seconds", 10 ** 1000),
    ("user_dir", []), ("user_dir", ""), ("user_dir", "dir\nother"), ("state_dir", "x\0y"),
])
def test_direct_configuration_rejects_unsafe_or_unknown_fields(tmp_path, field, value):
    raw = raw_config()
    raw[field] = value
    with pytest.raises(ValueError):
        load_direct_config(write_config(tmp_path, raw))


@pytest.mark.parametrize("raw", [[], None, 1, {}, {"user_dir": "directory"}])
def test_direct_configuration_requires_an_object_and_identity(tmp_path, raw):
    with pytest.raises(ValueError):
        load_direct_config(write_config(tmp_path, raw))


def test_duplicate_direct_configuration_fields_are_rejected(tmp_path):
    path = tmp_path / "direct.json"
    path.write_text('{"country_id":"0","country_id":"1"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        load_direct_config(path)


def test_programmatic_configuration_also_requires_absolute_paths(config):
    with pytest.raises(ValueError, match="absolute"):
        validate_direct_config(replace(config, user_dir=Path("relative")))


def test_probe_has_only_selected_session_receipt_and_fixed_dump(config):
    script = render_probe(config, TOKEN)
    header, effect, dump = script.splitlines()
    assert header + "\n" == ownership_header(config)
    assert f"session={SESSION} country=0" in header
    assert dump == "debug_dumpgamestate qs_direct"
    root = loads(effect.removeprefix("effect "))
    assert list(key for key, _ in root.items()) == ["if"]
    body = root.get("if")
    limit = body.get("limit")
    assert limit.get("is_ai") == "no"
    assert limit.get("is_ironman") == "no"
    assert limit.get("is_multiplayer") == "no"
    assert limit.get("has_country_flag") == f"qs_direct_session_{SESSION}"
    assert limit.get("NOT").get("has_country_flag") == f"qs_direct_done_{TOKEN}"
    assert body.get("set_country_flag") == f"qs_direct_done_{TOKEN}"
    assert set(key for key, _ in body.items()) == {"limit", "set_country_flag", "log"}
    assert script.count("set_country_flag") == 1
    assert "every_country" not in script and "country:" not in script
    assert render_probe(config, TOKEN) == script
    assert render_action(config, "probe_connection", TOKEN) == script


def test_hold_action_requires_separate_authorization_and_renders_only_normal_wait_orders(config):
    with pytest.raises(ValueError, match="not locally authorized"):
        atomic_publish(config, TOKEN, action_id="hold_military_fleets")
    assert list(config.user_dir.iterdir()) == []
    authorized = replace(config, allowed_actions=("hold_military_fleets",))
    script = render_action(authorized, "hold_military_fleets", TOKEN)
    body = loads(script.splitlines()[1].removeprefix("effect ")).get("if")
    assert body.get("limit").get("has_country_flag") == f"qs_direct_session_{SESSION}"
    assert body.get("limit").get("NOT").get("has_country_flag") == f"qs_direct_done_{TOKEN}"
    fleets = body.get("every_owned_fleet")
    assert dict(fleets.get("limit").items()) == {
        "is_mobile": "yes", "is_ship_class": "shipclass_military", "is_in_combat": "no",
    }
    assert fleets.get("clear_orders") == "yes"
    assert dict(fleets.get("queue_actions").items()) == {"wait": 30}
    assert set(key for key, _ in body.items()) == {"limit", "every_owned_fleet", "set_country_flag", "log"}
    assert script.index("clear_orders") < script.index("set_country_flag")
    assert script.endswith("debug_dumpgamestate qs_direct\n")
    atomic_publish(authorized, TOKEN, action_id="hold_military_fleets")
    assert config.inbox_path.read_text(encoding="ascii") == script


@pytest.mark.parametrize("action_id", [None, [], {}, "", "cash", "probe_connection\ncash 1"])
def test_action_id_cannot_supply_arbitrary_console_text(config, action_id):
    with pytest.raises(ValueError, match="action ID"):
        atomic_publish(config, TOKEN, action_id=action_id)
    assert list(config.user_dir.iterdir()) == []


def test_untrusted_identity_text_and_paths_never_enter_commands(config):
    marker = 'UNTRUSTED" }\ncash 10000\n'
    altered = replace(config, campaign_id=marker, verified_game_version=marker)
    script = render_probe(altered, TOKEN)
    assert marker not in script
    assert str(config.user_dir) not in script and str(config.state_dir) not in script


@pytest.mark.parametrize("token", [None, [], "", "2" * 31, "2" * 33, "Ｆ" * 32, "g" * 32,
                                   "2" * 31 + "\n", "2" * 31 + '"'])
def test_operation_token_injection_is_rejected_without_writing(config, token):
    with pytest.raises(ValueError, match="token"):
        atomic_publish(config, token)
    assert list(config.user_dir.iterdir()) == []


@pytest.mark.parametrize("change", [
    {"allowed_actions": ()}, {"single_player_confirmed": False}, {"non_ironman_confirmed": False},
])
def test_probe_requires_explicit_local_authorization_before_any_write(config, change):
    with pytest.raises(ValueError):
        atomic_publish(replace(config, **change), TOKEN)
    assert list(config.user_dir.iterdir()) == []


def test_publication_is_complete_ascii_and_uses_atomic_replace(config, monkeypatch):
    calls = []
    original = direct.os.replace
    def replace_complete(source, destination):
        calls.append((source, destination))
        assert Path(source).read_text(encoding="ascii") == render_probe(config, TOKEN)
        assert Path(source).parent == config.user_dir
        original(source, destination)
    monkeypatch.setattr(direct.os, "replace", replace_complete)
    assert atomic_publish(config, TOKEN) == config.inbox_path
    assert calls and config.inbox_path.read_text(encoding="ascii") == render_probe(config, TOKEN)
    assert list(config.user_dir.iterdir()) == [config.inbox_path]


@pytest.mark.parametrize("existing", ["user's commands\n", "", direct.OWNERSHIP_HEADER + "\n",
                                      f"{direct.OWNERSHIP_HEADER} session={'3' * 32} country=0\n"])
def test_unknown_or_other_session_inbox_is_preserved(config, existing):
    config.inbox_path.write_text(existing, encoding="utf-8")
    for publish in (lambda: atomic_publish(config, TOKEN), lambda: disarm_inbox(config),
                    lambda: publish_observation(config)):
        with pytest.raises(ValueError, match="not owned"):
            publish()
        assert config.inbox_path.read_text(encoding="utf-8") == existing
    assert list(config.user_dir.iterdir()) == [config.inbox_path]


def test_inbox_ownership_binds_country_as_well_as_session(config):
    atomic_publish(config, TOKEN)
    original = config.inbox_path.read_bytes()
    with pytest.raises(ValueError, match="not owned"):
        disarm_inbox(replace(config, country_id="1"))
    assert config.inbox_path.read_bytes() == original


def test_stop_blocks_publication_without_creating_inbox(config):
    (config.state_dir / "STOP").touch()
    with pytest.raises(ValueError, match="STOP"):
        atomic_publish(config, TOKEN)
    assert list(config.user_dir.iterdir()) == []


def test_stop_arriving_while_writing_preserves_prior_inbox_and_removes_temporary(config, monkeypatch):
    disarm_inbox(config)
    before = config.inbox_path.read_bytes()
    original = direct.os.fsync
    def stop_after_sync(descriptor):
        original(descriptor)
        (config.state_dir / "STOP").touch()
    monkeypatch.setattr(direct.os, "fsync", stop_after_sync)
    with pytest.raises(ValueError, match="STOP"):
        atomic_publish(config, TOKEN)
    assert config.inbox_path.read_bytes() == before
    assert list(config.user_dir.iterdir()) == [config.inbox_path]


def test_unknown_inbox_appearing_during_write_is_preserved(config, monkeypatch):
    original = direct.os.fsync
    def create_after_sync(descriptor):
        original(descriptor)
        config.inbox_path.write_text("preserve concurrent user file\n", encoding="utf-8")
    monkeypatch.setattr(direct.os, "fsync", create_after_sync)
    with pytest.raises(ValueError, match="not owned"):
        atomic_publish(config, TOKEN)
    assert config.inbox_path.read_text() == "preserve concurrent user file\n"
    assert list(config.user_dir.iterdir()) == [config.inbox_path]


def test_failed_atomic_replace_preserves_owned_inbox_and_cleans_temporary(config, monkeypatch):
    disarm_inbox(config)
    before = config.inbox_path.read_bytes()
    def denied(*args):
        raise PermissionError("simulated game sharing violation")
    monkeypatch.setattr(direct.os, "replace", denied)
    with pytest.raises(PermissionError):
        atomic_publish(config, TOKEN)
    assert config.inbox_path.read_bytes() == before
    assert list(config.user_dir.iterdir()) == [config.inbox_path]


def test_disarming_remains_available_after_stop_and_permission_revocation(config):
    atomic_publish(config, TOKEN)
    (config.state_dir / "STOP").touch()
    revoked = replace(config, allowed_actions=(), single_player_confirmed=False, non_ironman_confirmed=False)
    assert disarm_inbox(revoked) == config.inbox_path
    content = config.inbox_path.read_text(encoding="ascii")
    assert content == ownership_header(config) + "# disarmed\n"
    assert all(line.startswith("#") for line in content.splitlines())


def test_observation_request_has_no_probe_effect_and_needs_no_action_permission(config):
    readonly = replace(config, allowed_actions=())
    assert publish_observation(readonly) == config.inbox_path
    assert config.inbox_path.read_text(encoding="ascii") == (
        ownership_header(config) + "debug_dumpgamestate qs_direct\n"
    )
    assert not config.dump_path.exists()


@pytest.mark.parametrize("change", [
    {"single_player_confirmed": False}, {"non_ironman_confirmed": False},
])
def test_observation_publication_requires_mode_confirmation(config, change):
    with pytest.raises(ValueError, match="confirmed"):
        publish_observation(replace(config, **change))
    assert list(config.user_dir.iterdir()) == []


def test_stop_also_blocks_new_dump_request(config):
    (config.state_dir / "STOP").touch()
    with pytest.raises(ValueError, match="STOP"):
        publish_observation(config)
    assert list(config.user_dir.iterdir()) == []


def test_directory_cannot_be_replaced_as_inbox(config):
    config.inbox_path.mkdir()
    sentinel = config.inbox_path / "user.txt"
    sentinel.write_text("preserve", encoding="ascii")
    with pytest.raises(ValueError, match="regular file"):
        atomic_publish(config, TOKEN)
    assert sentinel.read_text() == "preserve"


def test_inbox_symlink_is_rejected_without_modifying_target(config, tmp_path):
    target = tmp_path / "user.txt"
    target.write_text("preserve", encoding="ascii")
    try:
        config.inbox_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is unavailable for this account")
    with pytest.raises(ValueError, match="regular file"):
        atomic_publish(config, TOKEN)
    assert target.read_text() == "preserve"
