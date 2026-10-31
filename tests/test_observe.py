import hashlib
import os
import stat
import time
from types import SimpleNamespace

import pytest

from stellaris_autopilot.observe import LogTail, Observation, SaveObserver, fresh
from stellaris_autopilot.saves import SaveError


SAVE = 'version="4.3.1" date="2200.01.01" player={country=0} country={0={name="Test" resources={alloys=10}}}'


def test_observation_hashes_original_file_and_selects_human_empire(tmp_path):
    path = tmp_path / "save.sav"
    path.write_text(SAVE, encoding="utf-8")
    observation = SaveObserver(path).read()
    assert observation.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert observation.state["country_id"] == "0"
    assert observation.modified_at == path.stat().st_mtime


def test_save_write_race_is_rejected_instead_of_mixing_state_and_digest(tmp_path, monkeypatch):
    import stellaris_autopilot.observe as module

    path = tmp_path / "save.sav"
    path.write_text(SAVE, encoding="utf-8")
    original = module.read_save

    def racy_read(path, **kwargs):
        result = original(path, **kwargs)
        path.write_text(SAVE + '\n# a new save was written', encoding="utf-8")
        return result

    monkeypatch.setattr(module, "read_save", racy_read)
    with pytest.raises(SaveError, match="changed during observation"):
        SaveObserver(path).read()


def test_log_tail_ignores_old_entries_and_waits_for_complete_lines(tmp_path):
    path = tmp_path / "game.log"
    path.write_bytes(b"SAA|old\n")
    tail = LogTail(path)
    assert tail.read() == []
    with path.open("ab") as stream:
        stream.write(b"unrelated\nSAA|bridge_")
    assert tail.read() == []
    with path.open("ab") as stream:
        stream.write(b"enabled\nsaa_DEFEND_DISPATCHED|country=Test\n")
    assert tail.read() == ["SAA|bridge_enabled", "saa_DEFEND_DISPATCHED|country=Test"]
    assert tail.read() == []


def test_log_rotation_and_truncation_reset_cursor(tmp_path):
    path = tmp_path / "game.log"
    path.write_bytes(b"irrelevant long previous log contents\n")
    tail = LogTail(path)
    path.write_bytes(b"SAA|new\n")
    assert tail.read() == ["SAA|new"]
    replacement = tmp_path / "new.log"
    replacement.write_bytes(b"SAA|rotated\n")
    os.replace(replacement, path)
    assert tail.read() == ["SAA|rotated"]


def test_log_file_created_after_watcher_is_observed(tmp_path):
    path = tmp_path / "later.log"
    tail = LogTail(path)
    assert tail.read() == []
    path.write_bytes(b"SAA|created\n")
    assert tail.read() == ["SAA|created"]


def test_freshness_rejects_stale_and_implausibly_future_snapshots():
    assert fresh(Observation({}, "x", time.time()), 180)
    assert not fresh(Observation({}, "x", time.time() - 181), 180)
    assert not fresh(Observation({}, "x", time.time() + 10), 180)


@pytest.mark.parametrize("participants", [
    "{ { country=0 call_type=primary } }",
    "{ 0={ country=0 call_type=primary } }",
    "{ country=0 call_type=primary }",
])
def test_save_observer_identifies_war_participants_across_list_layouts(tmp_path, participants):
    path = tmp_path / "war.sav"
    path.write_text(SAVE + " war={ 10={ attackers=" + participants + " defenders={country=7} } }",
                    encoding="utf-8")
    observed = SaveObserver(path).read().state
    assert observed["metadata"]["war_status_known"] is True
    assert observed["at_war"] is True


def test_unchanged_save_is_not_reparsed_and_cached_state_cannot_be_mutated(tmp_path, monkeypatch):
    import stellaris_autopilot.observe as module

    path = tmp_path / "save.sav"
    path.write_text(SAVE, encoding="utf-8")
    parse = module.read_save
    calls = []

    def tracked_parse(*args, **kwargs):
        calls.append(args)
        return parse(*args, **kwargs)

    monkeypatch.setattr(module, "read_save", tracked_parse)
    observer = SaveObserver(path)
    before = observer.read()
    before.state["resources"]["alloys"] = 9999
    second = observer.read()
    assert second.state["resources"]["alloys"] == 10
    assert before.digest == second.digest
    assert len(calls) == 1


def test_same_size_rewrite_restoring_mtime_still_invalidates_cached_observation(tmp_path):
    path = tmp_path / "save.sav"
    path.write_text(SAVE, encoding="utf-8")
    observer = SaveObserver(path)
    before = observer.read()
    old = path.stat()
    path.write_text(SAVE.replace("alloys=10", "alloys=20"), encoding="utf-8")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    after = observer.read()
    assert after.modified_at == before.modified_at
    assert after.state["resources"]["alloys"] == 20
    assert after.digest != before.digest


def test_parser_and_hash_cannot_mix_after_a_same_size_rewrite_with_restored_mtime(tmp_path, monkeypatch):
    import stellaris_autopilot.observe as module

    path = tmp_path / "save.sav"
    path.write_text(SAVE, encoding="utf-8")
    parse = module.read_save

    def rewrite_after_parse(*args, **kwargs):
        result = parse(*args, **kwargs)
        old = path.stat()
        path.write_text(SAVE.replace("alloys=10", "alloys=20"), encoding="utf-8")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        return result

    monkeypatch.setattr(module, "read_save", rewrite_after_parse)
    with pytest.raises(SaveError, match="changed during observation"):
        SaveObserver(path).read()


def test_input_limit_is_checked_before_parsing_large_save(tmp_path, monkeypatch):
    import stellaris_autopilot.observe as module

    path = tmp_path / "save.sav"
    path.write_bytes(b"x" * 101)
    monkeypatch.setattr(module, "read_save", lambda *args, **kwargs: pytest.fail("must check size first"))
    with pytest.raises(SaveError, match="100-byte input limit"):
        SaveObserver(path, max_bytes=100).read()


def test_hash_read_stays_bounded_when_game_appends_during_hashing(tmp_path, monkeypatch):
    import stellaris_autopilot.observe as module

    path = tmp_path / "save.sav"
    path.write_text(SAVE, encoding="utf-8")
    sha256 = module.hashlib.sha256

    class AppendingDigest:
        def __init__(self):
            self.real = sha256()
            self.appended = False

        def update(self, value):
            self.real.update(value)
            if not self.appended:
                with path.open("ab") as stream:
                    stream.write(b"x" * 1000)
                self.appended = True

    monkeypatch.setattr(module.hashlib, "sha256", AppendingDigest)
    with pytest.raises(SaveError, match="input limit"):
        SaveObserver(path, max_bytes=len(SAVE.encode()) + 100).read()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_observer_rejects_invalid_byte_limits(tmp_path, limit):
    with pytest.raises(ValueError, match="positive integer"):
        SaveObserver(tmp_path / "unused.sav", max_bytes=limit)


def test_nonregular_save_is_rejected_without_opening_a_potentially_blocking_pipe(tmp_path, monkeypatch):
    path = tmp_path / "configured-pipe.sav"
    real_stat = type(path).stat
    real_open = type(path).open

    def pipe_stat(self, *args, **kwargs):
        return (SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_size=0)
                if self == path else real_stat(self, *args, **kwargs))

    def reject_pipe_open(self, *args, **kwargs):
        if self == path:
            pytest.fail("a configured FIFO must be rejected before attempting open")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "stat", pipe_stat)
    monkeypatch.setattr(type(path), "open", reject_pipe_open)
    with pytest.raises(SaveError, match="regular file"):
        SaveObserver(path).read()
