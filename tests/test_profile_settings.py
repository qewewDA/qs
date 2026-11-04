import hashlib
import importlib.util
from pathlib import Path

import pytest

from stellaris_autopilot import profile_settings
from stellaris_autopilot.profile_settings import seed_profile_settings


SETTINGS_180HZ = b"""force_pow2_textures=no
language="l_simp_chinese"
graphics=
{
    size=
    {
        x=2560
        y=1440
    }

    gui_scale=1.000000
    refreshRate=180
    refreshCap=180
    fullScreen=no
    borderless=yes
    vsync=no
}
autosave=0
tutorial=0
"""

PDX_SETTINGS = b"""language="l_simp_chinese"
"""


def _source_profile(tmp_path: Path, *, settings: bool = True, pdx: bool = True) -> Path:
    source = tmp_path / "documents" / "Paradox Interactive" / "Stellaris"
    source.mkdir(parents=True)
    if settings:
        (source / "settings.txt").write_bytes(SETTINGS_180HZ)
    if pdx:
        (source / "pdx_settings.txt").write_bytes(PDX_SETTINGS)
    return source


def test_reconnect_preserves_existing_180hz_settings_and_pdx_bytes(tmp_path):
    user_dir = tmp_path / "campaign" / "userdir"
    user_dir.mkdir(parents=True)
    settings = user_dir / "settings.txt"
    pdx = user_dir / "pdx_settings.txt"
    settings.write_bytes(SETTINGS_180HZ)
    pdx.write_bytes(PDX_SETTINGS)
    source = _source_profile(tmp_path)

    copied = seed_profile_settings(user_dir, source_user_dir=source)

    assert copied == []
    assert settings.read_bytes() == SETTINGS_180HZ
    assert pdx.read_bytes() == PDX_SETTINGS


def test_first_seed_copies_settings_and_pdx_from_explicit_source(tmp_path):
    user_dir = tmp_path / "campaign" / "userdir"
    user_dir.mkdir(parents=True)
    source = _source_profile(tmp_path)

    copied = seed_profile_settings(user_dir, source_user_dir=source)

    assert copied == [user_dir / "settings.txt", user_dir / "pdx_settings.txt"]
    assert (user_dir / "settings.txt").read_bytes() == SETTINGS_180HZ
    assert (user_dir / "pdx_settings.txt").read_bytes() == PDX_SETTINGS


def test_only_the_two_fixed_profile_files_are_considered(tmp_path):
    user_dir = tmp_path / "campaign" / "userdir"
    user_dir.mkdir(parents=True)
    source = _source_profile(tmp_path)
    (source / "other.txt").write_bytes(b"must not be copied")

    copied = seed_profile_settings(user_dir, source_user_dir=source)

    assert copied == [user_dir / "settings.txt", user_dir / "pdx_settings.txt"]
    assert not (user_dir / "other.txt").exists()


def test_missing_source_files_create_nothing(tmp_path):
    user_dir = tmp_path / "campaign" / "userdir"
    user_dir.mkdir(parents=True)
    source = tmp_path / "documents" / "Paradox Interactive" / "Stellaris"
    source.mkdir(parents=True)

    copied = seed_profile_settings(user_dir, source_user_dir=source)

    assert copied == []
    assert list(user_dir.iterdir()) == []


def test_missing_source_directory_creates_nothing(tmp_path):
    user_dir = tmp_path / "campaign" / "userdir"
    user_dir.mkdir(parents=True)

    copied = seed_profile_settings(user_dir, source_user_dir=tmp_path / "missing")

    assert copied == []
    assert list(user_dir.iterdir()) == []


def test_partial_target_only_fills_the_missing_file(tmp_path):
    user_dir = tmp_path / "campaign" / "userdir"
    user_dir.mkdir(parents=True)
    settings = user_dir / "settings.txt"
    settings.write_bytes(SETTINGS_180HZ)
    source = _source_profile(tmp_path)

    copied = seed_profile_settings(user_dir, source_user_dir=source)

    assert copied == [user_dir / "pdx_settings.txt"]
    assert settings.read_bytes() == SETTINGS_180HZ
    assert (user_dir / "pdx_settings.txt").read_bytes() == PDX_SETTINGS


def test_non_windows_default_source_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_settings, "_IS_WINDOWS", False)
    user_dir = tmp_path / "campaign" / "userdir"
    user_dir.mkdir(parents=True)

    copied = seed_profile_settings(user_dir)

    assert copied == []
    assert list(user_dir.iterdir()) == []


def _load_serve_campaign():
    pytest.importorskip("mcp")
    script = Path(__file__).resolve().parents[1] / "scripts" / "serve_campaign.py"
    spec = importlib.util.spec_from_file_location("serve_campaign_profile_settings_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeStore:
    def __init__(self, root: Path, source: Path):
        self.root = root
        self._source = source

    def load(self):
        return {"country_id": "0", "campaign_id": "campaign-1", "game_version": "Cygnus v4.5.2"}

    def latest(self):
        return self._source, {
            "id": "checkpoint-1",
            "sha256": hashlib.sha256(self._source.read_bytes()).hexdigest(),
            "date": "2200.01.01",
        }


def test_prepare_profile_preserves_180hz_settings_across_reconnect(tmp_path, monkeypatch):
    serve_campaign = _load_serve_campaign()
    root = tmp_path / "campaign"
    user_dir = root / "userdir"
    user_dir.mkdir(parents=True)
    settings = user_dir / "settings.txt"
    pdx = user_dir / "pdx_settings.txt"
    settings.write_bytes(SETTINGS_180HZ)
    pdx.write_bytes(PDX_SETTINGS)
    source = tmp_path / "source.sav"
    source.write_bytes(b"fake checkpoint bytes")
    monkeypatch.setattr(profile_settings, "_IS_WINDOWS", False)
    store = _FakeStore(root, source)

    serve_campaign.prepare_profile(store, {"token": "a" * 32})
    serve_campaign.prepare_profile(store, {"token": "a" * 32})

    assert settings.read_bytes() == SETTINGS_180HZ
    assert pdx.read_bytes() == PDX_SETTINGS


def test_prepare_profile_does_not_create_a_hardcoded_settings_template(tmp_path, monkeypatch):
    serve_campaign = _load_serve_campaign()
    root = tmp_path / "campaign"
    user_dir = root / "userdir"
    user_dir.mkdir(parents=True)
    source = tmp_path / "source.sav"
    source.write_bytes(b"fake checkpoint bytes")
    monkeypatch.setattr(profile_settings, "_IS_WINDOWS", False)
    store = _FakeStore(root, source)

    serve_campaign.prepare_profile(store, {"token": "a" * 32})

    assert not (user_dir / "settings.txt").exists()
    assert not (user_dir / "pdx_settings.txt").exists()
