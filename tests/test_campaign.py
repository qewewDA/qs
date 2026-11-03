import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile

import pytest

import stellaris_autopilot.campaign as module
from stellaris_autopilot.campaign import CampaignError, CampaignStore, package_dump
from stellaris_autopilot.direct_observe import DirectObserver


SAVE = '''version="Cygnus v4.5.2" version_control_revision=101 name="Earth"
date="2200.01.01" galaxy={name="campaign-123"} ironman=no multiplayer=no
required_dlcs={"Utopia"} player={country=0}
species_db={2348810241={portrait="human"}}
country={0={name="Earth" founder_species_ref=2348810241
flag={icon={category="human" file="flag_human_9.dds"}
background={category="backgrounds" file="00_solid.dds"} colors={"blue" "black" "null" "null" "null" "null"}}
resources={minerals=617.55104}} 1={name="Other"}}
'''


def source(tmp_path, text=SAVE):
    path = tmp_path / "external-original.sav"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("gamestate", text)
        archive.writestr("meta", 'date="2200.01.01"')
    return path


def initialize(tmp_path):
    original = source(tmp_path)
    store = CampaignStore(tmp_path / "persistent")
    with store.locked():
        store.initialize(original, country_id="0", single_player_confirmed=True, non_ironman_confirmed=True)
    return store, original


def dump(tmp_path, text=None):
    path = tmp_path / "dump.txt"
    path.write_text(text or SAVE.replace("2200.01.01", "2200.01.02"), encoding="utf-8")
    return path


def checkpoint(store, tmp_path, token):
    path = dump(tmp_path)
    observation = DirectObserver(path, "0").read()
    return store.checkpoint_dump(path, session_token=token, expected_digest=observation.digest,
                                 expected_date=observation.state["date"])


def test_checkpoint_retains_exact_gamestate_bytes_and_atomic_identity(tmp_path):
    store, original = initialize(tmp_path)
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    with store.locked():
        session = store.begin_session()
        store.update_session(session["token"], status="connected", owned_pid=23)
        result = checkpoint(store, tmp_path, session["token"])
        path, latest = store.latest()
        with zipfile.ZipFile(path) as archive:
            assert archive.read("gamestate") == (tmp_path / "dump.txt").read_bytes()
            metadata = archive.read("meta").decode()
            assert 'date="2200.01.02"' in metadata
            assert '"Utopia"' in metadata
            assert 'player_portrait="human"' in metadata
            assert 'flag=' in metadata
            assert 'file="flag_human_9.dds"' in metadata
        assert result == latest
        assert latest["parse_verified"] is True
        assert latest["reload_verified"] is False
        store.finish_session(session["token"])
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256((store.root / "original.sav").read_bytes()).hexdigest() == original_hash


def test_later_session_uses_latest_checkpoint_without_reimport(tmp_path):
    store, original = initialize(tmp_path)
    with store.locked():
        session = store.begin_session()
        store.update_session(session["token"], status="connected")
        saved = checkpoint(store, tmp_path, session["token"])
        store.finish_session(session["token"])
        original.write_text("the user changed their external save", encoding="utf-8")
        later = store.begin_session()
        assert later["loaded_checkpoint"] == saved["id"]
        assert store.latest()[1]["date"] == "2200.01.02"
        with pytest.raises(CampaignError, match="already exists"):
            store.initialize(original, country_id="0", single_player_confirmed=True, non_ironman_confirmed=True)


@pytest.mark.parametrize("replacement", [
    SAVE.replace('name="campaign-123"', 'name="another-campaign"'),
    SAVE.replace("version=\"Cygnus v4.5.2\"", "version=\"Another build\""),
    SAVE.replace("player={country=0}", "player={country=1}"),
    SAVE.replace("ironman=no", "ironman=yes"),
    SAVE.replace("multiplayer=no", "multiplayer=yes"),
    SAVE.replace('date="2200.01.01"', 'date="2199.01.01"'),
    SAVE + ' galaxy={name="campaign-123"}',
    SAVE + ' date="2200.01.01"',
])
def test_identity_or_rewind_failure_never_advances_manifest(tmp_path, replacement):
    store, _ = initialize(tmp_path)
    with store.locked():
        session = store.begin_session()
        store.update_session(session["token"], status="connected")
        before = store.load()["latest_checkpoint"]
        path = dump(tmp_path, replacement)
        with pytest.raises((CampaignError, ValueError)):
            store.checkpoint_dump(path, session_token=session["token"],
                                  expected_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
                                  expected_date="2200.01.01")
        assert store.load()["latest_checkpoint"] == before


def test_changed_dump_between_observation_and_save_is_rejected(tmp_path):
    path = dump(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(SAVE, encoding="utf-8")
    target = tmp_path / "new.sav"
    with pytest.raises(CampaignError, match="changed"):
        package_dump(path, target, "0", expected_digest=digest)
    assert not target.exists()


def test_packaging_refuses_to_overwrite_existing_checkpoint(tmp_path):
    target = tmp_path / "new.sav"
    target.write_bytes(b"precious")
    with pytest.raises(CampaignError, match="overwritten"):
        package_dump(dump(tmp_path), target, "0")
    assert target.read_bytes() == b"precious"


@pytest.mark.parametrize("replacement", [
    SAVE.replace('portrait="human"', ''),
    SAVE.replace('founder_species_ref=2348810241', 'founder_species_ref=8'),
    SAVE.replace('founder_species_ref=2348810241', ''),
    SAVE.replace('flag={icon=', 'another_field={icon='),
])
def test_dump_packaging_requires_actual_selected_player_browser_metadata(tmp_path, replacement):
    with pytest.raises(CampaignError):
        package_dump(dump(tmp_path, replacement), tmp_path / "candidate.sav", "0")
    assert not (tmp_path / "candidate.sav").exists()


def test_manifest_failure_leaves_old_pointer_and_never_adopts_orphan(tmp_path, monkeypatch):
    store, _ = initialize(tmp_path)
    with store.locked():
        session = store.begin_session()
        store.update_session(session["token"], status="connected")
        before = store.load()["latest_checkpoint"]
        monkeypatch.setattr(module, "_atomic", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            checkpoint(store, tmp_path, session["token"])
        assert store.load()["latest_checkpoint"] == before
        assert len(list((store.root / "checkpoints").glob("*.sav"))) == 1
        assert store.latest()[0] == store.root / "original.sav"


@pytest.mark.parametrize("operation_status", ["pending", "dispatched", "unverified"])
def test_unresolved_commands_block_restart_and_save_without_replay(tmp_path, operation_status):
    store, _ = initialize(tmp_path)
    with store.locked():
        session = store.begin_session()
        store.update_session(session["token"], status="connected")
        journal = store.root / "sessions" / session["token"] / "journal.sqlite"
        with sqlite3.connect(journal) as database:
            database.execute("CREATE TABLE operations (id INTEGER, action TEXT, status TEXT)")
            database.execute("INSERT INTO operations VALUES (7, 'queue_building', ?)", (operation_status,))
        before = journal.read_bytes()
        with pytest.raises(CampaignError, match="unresolved"):
            store.begin_session(recover_last_checkpoint=True)
        with pytest.raises(CampaignError, match="unresolved"):
            checkpoint(store, tmp_path, session["token"])
        assert journal.read_bytes() == before
        assert store.unresolved() == [{"session_token": session["token"], "id": 7,
                                       "action": "queue_building", "status": operation_status}]


def test_interrupted_session_requires_explicit_recovery_and_retains_history(tmp_path):
    store, _ = initialize(tmp_path)
    with store.locked():
        old = store.begin_session()
    with CampaignStore(store.root).locked() as reopened:
        with pytest.raises(CampaignError, match="Interrupted"):
            reopened.begin_session()
        new = reopened.begin_session(recover_last_checkpoint=True)
        assert new["token"] != old["token"]
        assert new["recovered_interrupted_session"] is True
        assert reopened.load()["sessions"][0]["status"] == "interrupted"
        assert reopened.load()["sessions"][0]["token"] == old["token"]
        reopened.update_session(new["token"], status="connected")
        checkpoint(reopened, tmp_path, new["token"])
        reopened.finish_session(new["token"])
        later = reopened.begin_session()
        assert later["recovered_interrupted_session"] is False


def test_cannot_finish_session_using_prior_checkpoint(tmp_path):
    store, _ = initialize(tmp_path)
    with store.locked():
        session = store.begin_session()
        store.update_session(session["token"], status="connected")
        with pytest.raises(CampaignError, match="own verified checkpoint"):
            store.finish_session(session["token"])


def test_checkpoint_tampering_prevents_resume(tmp_path):
    store, _ = initialize(tmp_path)
    (store.root / "original.sav").write_text(SAVE + " # edited", encoding="utf-8")
    with store.locked(), pytest.raises(CampaignError, match="changed"):
        store.begin_session()


@pytest.mark.parametrize("field,value", [
    ("date", "2200.01.02"),
    ("gamestate_sha256", "f" * 64),
])
def test_cached_latest_still_rejects_changed_checkpoint_metadata(tmp_path, field, value):
    store, _ = initialize(tmp_path)
    store.latest()
    with store.locked():
        manifest = store.load()
        manifest["checkpoints"][0][field] = value
        store._write(manifest)
        with pytest.raises(CampaignError, match="content changed"):
            store.latest()


@pytest.mark.parametrize("field,value", [
    ("campaign_id", "another-campaign"),
    ("game_version", "Cygnus v4.5.3"),
])
def test_cached_latest_rechecks_changed_campaign_identity(tmp_path, field, value):
    store, _ = initialize(tmp_path)
    store.latest()
    with store.locked():
        manifest = store.load()
        manifest[field] = value
        store._write(manifest)
        with pytest.raises(CampaignError, match="does not match"):
            store.latest()


def test_cached_latest_hashes_unchanged_size_file_again(tmp_path):
    store, _ = initialize(tmp_path)
    path, _ = store.latest()
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(CampaignError, match="bytes changed"):
        store.latest()


def test_reload_proof_needs_distinct_session_fresh_observation_and_bootstrap(tmp_path):
    store, _ = initialize(tmp_path)
    with store.locked():
        first = store.begin_session()
        store.update_session(first["token"], status="connected")
        saved = checkpoint(store, tmp_path, first["token"])
        observation = DirectObserver(tmp_path / "dump.txt", "0").read()
        with pytest.raises(CampaignError, match="distinct"):
            store.verify_reload(saved["id"], observation)
        store.finish_session(first["token"])
        second = store.begin_session()
        with pytest.raises(CampaignError, match="fresh"):
            store.verify_reload(saved["id"], observation)
        text = (tmp_path / "dump.txt").read_text().replace(
            '0={name="Earth"', '0={flags={qs_direct_session_' + second["token"] + '=1} name="Earth"')
        time.sleep(0.005)
        (tmp_path / "dump.txt").write_text(text, encoding="utf-8")
        fresh = DirectObserver(tmp_path / "dump.txt", "0").read()
        result = store.verify_reload(saved["id"], fresh)
        assert result["reload_verified"] is True
        assert result["reload_session_token"] == second["token"]
        assert result["gamestate_sha256"] != fresh.digest


def test_campaign_lock_excludes_other_process_and_releases_after_crash(tmp_path):
    store, _ = initialize(tmp_path)
    script = '''from pathlib import Path
import sys
from stellaris_autopilot.campaign import CampaignStore
with CampaignStore(Path(sys.argv[1])).locked():
 print("locked", flush=True)
 sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, "-u", "-c", script, str(store.root)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(CampaignError, match="Another controller"), store.locked():
            pass
        process.terminate()
        process.wait(timeout=5)
        # Windows can signal the process handle just before the kernel releases
        # its remaining file locks. Bound that cleanup wait instead of assuming
        # zero latency after TerminateProcess.
        deadline = time.monotonic() + 3
        while True:
            try:
                with store.locked():
                    break
            except CampaignError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        with store.locked():
            assert store.load()["country_id"] == "0"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


def test_mutations_require_campaign_lock(tmp_path):
    store, _ = initialize(tmp_path)
    with pytest.raises(CampaignError, match="Acquire"):
        store.begin_session()


def test_manifest_escape_path_is_rejected(tmp_path):
    store, original = initialize(tmp_path)
    manifest = store.load()
    manifest["checkpoints"][0]["path"] = str(original)
    (store.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CampaignError, match="relative path"):
        store.latest()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires privileges")
def test_symlink_checkpoint_is_rejected(tmp_path):
    store, original = initialize(tmp_path)
    (store.root / "original.sav").unlink()
    (store.root / "original.sav").symlink_to(original)
    with pytest.raises(CampaignError, match="symbolic"):
        store.latest()
