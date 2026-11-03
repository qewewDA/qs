import hashlib
import os
import stat
import time
from types import SimpleNamespace

import pytest

import stellaris_autopilot.direct_observe as module
from stellaris_autopilot.direct_observe import DirectObserver
from stellaris_autopilot.saves import SaveError, read_save


SESSION = "qs_direct_session_" + "a" * 32
DONE = "qs_direct_done_" + "1" * 32
OTHER_DONE = "qs_direct_done_" + "2" * 32
SAVE = '''version="4.5.2" date=2200.01.01 ironman=no multiplayer=no
player={country=0}
country={0={name="Earth" resources={alloys=10}} 1={name="Other"}}
'''


def write_dump(tmp_path, text=SAVE):
    path = tmp_path / "dump_qs_direct.sav.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_direct_dump_matches_save_normalization_and_hashes_same_bytes(tmp_path):
    path = write_dump(tmp_path)
    original = path.read_bytes()
    observation = DirectObserver(path, "0").read()
    expected = read_save(path, country_id="0")
    assert observation.state.pop("direct_bridge") == {
        "session_flags": [], "done_flags": [], "target_flags": [], "receipt_only": True,
    }
    assert observation.state == expected
    assert observation.digest == hashlib.sha256(original).hexdigest()
    assert observation.modified_at == path.stat().st_mtime
    assert observation.state["metadata"]["war_status_known"] is False
    assert "at_war" in observation.state["metadata"]["missing_fields"]


def test_only_selected_country_bridge_flags_are_exposed(tmp_path):
    text = f'''date=2200.01.01 player={{country=0}}
    flags={{{OTHER_DONE}=62808000}}
    country={{
      0={{flags={{
        {SESSION}=62808000
        qs_direct_targets_{"a" * 32}=62808000
        {DONE}={{flag_date=62808000 flag_days=10}}
        secret_country_flag=62808000
        qs_direct_session_short=62808000
        qs_direct_done_{"B" * 32}=62808000
        qs_direct_done_{"3" * 33}=62808000
        qs_direct_done_{"4" * 32}_extra=62808000
      }}}}
      1={{flags={{{OTHER_DONE}=62808000}}}}
    }}'''
    observation = DirectObserver(write_dump(tmp_path, text), "0").read()
    assert observation.state["direct_bridge"] == {
        "session_flags": [SESSION], "done_flags": [DONE],
        "target_flags": ["qs_direct_targets_" + "a" * 32], "receipt_only": True,
    }
    assert "secret_country_flag" not in str(observation.state)
    assert OTHER_DONE not in str(observation.state)
    assert "62808000" not in str(observation.state)
    other = DirectObserver(tmp_path / "dump_qs_direct.sav.txt", "1").read()
    assert other.state["direct_bridge"]["done_flags"] == [OTHER_DONE]


@pytest.mark.parametrize("flag_value", ["no", "-1", "1.5", "{}", "{flag_days=10}"])
def test_unrecognized_flag_values_cannot_acknowledge_an_operation(tmp_path, flag_value):
    text = f"date=2200.01.01 player={{country=0}} country={{0={{flags={{{DONE}={flag_value}}}}}}}"
    assert DirectObserver(write_dump(tmp_path, text)).read().state["direct_bridge"]["done_flags"] == []


def test_repeated_flag_containers_cannot_acknowledge_an_operation(tmp_path):
    text = f"date=2200.01.01 country={{0={{flags={{{DONE}=62808000}} flags={{}}}}}}"
    assert DirectObserver(write_dump(tmp_path, text), "0").read().state["direct_bridge"]["done_flags"] == []


def test_source_can_advance_during_slow_parse_without_invalidating_copied_snapshot(tmp_path, monkeypatch):
    path = write_dump(tmp_path)
    original_bytes = path.read_bytes()
    original_modified = path.stat().st_mtime
    parse = module.loads
    calls = []

    def update_during_parse(text):
        calls.append(text)
        path.write_text(SAVE.replace("alloys=10", "alloys=20"), encoding="utf-8")
        return parse(text)

    monkeypatch.setattr(module, "loads", update_during_parse)
    observer = DirectObserver(path)
    first = observer.read()
    assert len(calls) == 1
    assert first.state["resources"]["alloys"] == 10
    assert first.digest == hashlib.sha256(original_bytes).hexdigest()
    assert first.modified_at == original_modified
    monkeypatch.setattr(module, "loads", parse)
    second = observer.read()
    assert second.state["resources"]["alloys"] == 20
    assert second.digest != first.digest


class ReadMutation:
    def __init__(self, stream, mutate):
        self.stream, self.mutate = stream, mutate

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def fileno(self):
        return self.stream.fileno()

    def read(self, size):
        data = self.stream.read(size)
        self.mutate(size)
        return data


def test_same_size_write_restoring_mtime_during_copy_is_rejected(tmp_path, monkeypatch):
    path = write_dump(tmp_path)
    old = path.stat()
    real_open = type(path).open
    mutated = False

    def mutate(size):
        nonlocal mutated
        assert size == 128 * 1024 * 1024 + 1
        path.write_text(SAVE.replace("alloys=10", "alloys=20"), encoding="utf-8")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        mutated = True

    def racing_open(self, *args, **kwargs):
        stream = real_open(self, *args, **kwargs)
        return ReadMutation(stream, mutate) if self == path and args == ("rb",) and not mutated else stream

    monkeypatch.setattr(type(path), "open", racing_open)
    monkeypatch.setattr(module, "loads", lambda text: pytest.fail("must reject before parsing"))
    with pytest.raises(SaveError, match="changed during observation"):
        DirectObserver(path).read()


def test_replacement_of_path_during_copy_is_rejected_even_if_old_handle_is_unchanged(tmp_path, monkeypatch):
    path = write_dump(tmp_path)
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(path.read_bytes())
    old = path.stat()
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    real_open = type(path).open
    opens = 0

    def replaced_open(self, *args, **kwargs):
        nonlocal opens
        if self == path and args == ("rb",):
            opens += 1
            # Simulate an atomic replacement without requiring Windows to
            # permit renaming a path whose original read handle is open.
            return real_open(replacement if opens > 1 else self, *args, **kwargs)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", replaced_open)
    with pytest.raises(SaveError, match="changed during observation"):
        DirectObserver(path).read()


def test_path_stat_creation_time_can_differ_from_handle_ctime(tmp_path, monkeypatch):
    path = write_dump(tmp_path)
    real_stat = type(path).stat

    def creation_time_stat(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        if self != path:
            return info
        return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                               st_size=info.st_size, st_mtime=info.st_mtime,
                               st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns - 1)

    monkeypatch.setattr(type(path), "stat", creation_time_stat)
    assert DirectObserver(path).read().state["resources"]["alloys"] == 10


def test_windows_change_time_is_checked_even_when_stat_fields_are_unchanged(tmp_path, monkeypatch):
    path = write_dump(tmp_path)
    changes = iter([100, 101])
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_windows_change_time", lambda stream: next(changes))
    with pytest.raises(SaveError, match="changed during observation"):
        DirectObserver(path).read()


def test_cache_avoids_reparsing_and_callers_cannot_mutate_future_observations(tmp_path, monkeypatch):
    path = write_dump(tmp_path, SAVE.replace('name="Earth"', f'name="Earth" flags={{{DONE}=62808000}}'))
    parse = module.loads
    calls = []

    def tracked_parse(text):
        calls.append(text)
        return parse(text)

    monkeypatch.setattr(module, "loads", tracked_parse)
    observer = DirectObserver(path)
    first = observer.read()
    first.state["resources"]["alloys"] = 9999
    first.state["direct_bridge"]["done_flags"].append(OTHER_DONE)
    second = observer.read()
    assert second.state["resources"]["alloys"] == 10
    assert second.state["direct_bridge"]["done_flags"] == [DONE]
    assert len(calls) == 1


def test_size_limit_is_checked_before_parse_and_reads_remain_bounded(tmp_path, monkeypatch):
    path = write_dump(tmp_path)
    monkeypatch.setattr(module, "loads", lambda text: pytest.fail("must check input size first"))
    with pytest.raises(SaveError, match="10-byte input limit"):
        DirectObserver(path, max_bytes=10).read()


def test_dump_growth_during_copy_cannot_exceed_read_limit(tmp_path, monkeypatch):
    path = write_dump(tmp_path)
    real_open = type(path).open
    limit = len(path.read_bytes()) + 10
    reads = []

    def growing_open(self, *args, **kwargs):
        stream = real_open(self, *args, **kwargs)
        if self == path and args == ("rb",):
            def grow(size):
                reads.append(size)
                with real_open(path, "ab") as writer:
                    writer.write(b"x" * 1000)
            return ReadMutation(stream, grow)
        return stream

    monkeypatch.setattr(type(path), "open", growing_open)
    monkeypatch.setattr(module, "loads", lambda text: pytest.fail("must reject changed bytes"))
    with pytest.raises(SaveError, match="changed during observation"):
        DirectObserver(path, max_bytes=limit).read()
    assert reads == [limit + 1]


def test_future_timestamp_and_nonregular_file_are_rejected_before_open(tmp_path, monkeypatch):
    path = write_dump(tmp_path)
    future = time.time() + 60
    os.utime(path, (future, future))
    real_stat = type(path).stat

    def reject_open(*args, **kwargs):
        pytest.fail("invalid input must be rejected before opening")

    monkeypatch.setattr(type(path), "open", reject_open)
    with pytest.raises(SaveError, match="in the future"):
        DirectObserver(path).read()

    def pipe_stat(self, *args, **kwargs):
        return SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0) if self == path else real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "stat", pipe_stat)
    with pytest.raises(SaveError, match="regular file"):
        DirectObserver(path).read()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_byte_limit_is_rejected(tmp_path, limit):
    with pytest.raises(ValueError, match="positive integer"):
        DirectObserver(tmp_path / "unused.txt", max_bytes=limit)


@pytest.mark.parametrize("payload", [b"\xff", b"date=2200.01.01 country={", b"\x00binary"])
def test_unreadable_dump_is_reported_as_save_error(tmp_path, payload):
    path = tmp_path / "bad.txt"
    path.write_bytes(payload)
    with pytest.raises(SaveError, match="Cannot read textual direct dump"):
        DirectObserver(path).read()


def test_missing_empire_is_not_guessed(tmp_path):
    path = write_dump(tmp_path, 'date=2200.01.01 country={0={name="AI"}}')
    with pytest.raises(SaveError, match="explicit country_id"):
        DirectObserver(path).read()
