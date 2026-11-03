import json
from pathlib import Path
import shutil

import pytest

import stellaris_autopilot.cli as cli


MOD = Path(__file__).resolve().parents[1] / "mod" / "stellaris_ai_autopilot"


def isolated_wheel_layout(tmp_path, monkeypatch, *, include_mod):
    # uv can load Python modules from its cache while sys.prefix points at a
    # separate temporary environment. The wheel RECORD locates its data files.
    site = tmp_path / "cache" / "Lib" / "site-packages"
    info = site / "stellaris_ai_autopilot-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: stellaris-ai-autopilot\nVersion: 0.1.0\n",
        encoding="utf-8",
    )
    (info / "RECORD").write_text(
        "../../share/stellaris-ai-autopilot/mod/descriptor.mod,,\n",
        encoding="utf-8",
    )
    resources = tmp_path / "cache" / "share" / "stellaris-ai-autopilot" / "mod"
    if include_mod:
        shutil.copytree(MOD, resources)
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setattr(cli, "__file__", str(site / "stellaris_autopilot" / "cli.py"))
    monkeypatch.setattr(cli.sys, "prefix", str(tmp_path / "runtime"))


def test_isolated_wheel_installs_mod_using_record_when_prefix_has_no_data(tmp_path, monkeypatch, capsys):
    isolated_wheel_layout(tmp_path, monkeypatch, include_mod=True)
    destination = tmp_path / "installed-mod"
    cli.install_mod(destination)
    installed = destination / "stellaris_ai_autopilot"
    event = Path("events/saa_events.txt")
    assert (installed / event).read_bytes() == (MOD / event).read_bytes()
    localisation = Path("localisation/simp_chinese/saa_l_simp_chinese.yml")
    assert (installed / localisation).read_bytes() == (MOD / localisation).read_bytes()
    assert (destination / "stellaris_ai_autopilot.mod").is_file()
    assert json.loads(capsys.readouterr().out)["installed"] == str(installed)


def test_isolated_wheel_missing_recorded_resources_does_not_create_install(tmp_path, monkeypatch):
    isolated_wheel_layout(tmp_path, monkeypatch, include_mod=False)
    destination = tmp_path / "installed-mod"
    with pytest.raises(ValueError, match="Bundled Mod data missing"):
        cli.install_mod(destination)
    assert not destination.exists()
