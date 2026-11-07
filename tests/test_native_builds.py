"""Offline regression tests for the reviewed native 4.5.3 (Cygnus) profile.

Nothing here reads a real stellaris.exe, attaches to a game, or starts and stops a
process. The generated JavaScript is compiled and executed only inside Node with
fake Process/Memory/NativeFunction/Interceptor globals, so a mismatching live code
prefix must abort before any NativeFunction or Interceptor is created.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import inspect
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from stellaris_autopilot import connection_diagnostics, native_builds
from stellaris_autopilot.attached_runtime import AttachedRuntime
from stellaris_autopilot.campaign import CampaignError
from stellaris_autopilot.native import (
    BuildingConstruction,
    BuildingRequest,
    DistrictConstruction,
    DistrictRequest,
    NativeResearchBridge,
    PauseRequest,
    ResearchSelection,
)
from stellaris_autopilot.ship_commands import ShipConstruction


PACKAGE_DIR = Path(native_builds.__file__).parent
TOKEN = "7" * 32
LEGACY = native_builds.build_for_hash(native_builds.LEGACY_SHA256)
CYGNUS = native_builds.build_for_hash(native_builds.CYGNUS_453_SHA256)
GUARD_ERROR = "Loaded game code does not match the reviewed native profile"
GATE_ERROR = "This build permits observation, pause and checkpoints only"
LOOKUP_ERROR = "Gameplay definitions use the versioned knowledge bundle"
GUARD_END = "if (actual !== expected) throw new Error('" + GUARD_ERROR + "');\n} }\n"
GATE_START = "\n{ const enqueue = rpc.exports.enqueue;"

OBSERVATION_CALLS = [
    {"kind": "request_observation", "country_id": "0", "token": TOKEN},
    {"kind": "run_inbox", "country_id": "0", "token": TOKEN},
    {"kind": "load_attached_events", "country_id": "0", "token": TOKEN},
    {"kind": "set_paused", "country_id": "0", "token": TOKEN, "paused": True},
]
REFUSED_CALLS = [
    {"kind": "set_paused", "country_id": "0", "token": TOKEN, "paused": False},
    {"kind": "set_paused", "country_id": "0", "token": TOKEN, "paused": 1},
    {"kind": "select_research", "country_id": "0", "token": TOKEN,
     "area": "physics", "technology": "tech_shields_2"},
    {"kind": "queue_building", "country_id": "0", "token": TOKEN, "planet_id": "1", "zone_id": "2",
     "building": "building_farm", "queue_id": "3", "colony_id": "4"},
    None,
    {},
    {"kind": "unknown"},
]

NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');

const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function segments(signatures) {
  const map = new Map();
  for (const [rva, hex] of signatures) {
    const bytes = new Uint8Array(hex.length / 2);
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = parseInt(hex.slice(index * 2, index * 2 + 2), 16);
    }
    map.set(rva, bytes);
  }
  return map;
}

function corrupt(map, mutations) {
  for (const [rva, index, value] of mutations || []) {
    const segment = map.get(rva);
    if (segment && index < segment.length) segment[index] = value;
  }
}

function parseOnly(entry) {
  try {
    new vm.Script(entry.source, { filename: entry.name + '.js' });
    return { name: entry.name, ok: true };
  } catch (thrown) {
    return { name: entry.name, ok: false, message: String(thrown && thrown.message) };
  }
}

class Ptr {
  constructor(address, map, ops) { this.address = address; this.map = map; this.ops = ops; }
  add(offset) { return new Ptr(this.address + offset, this.map, this.ops); }
  sub(offset) { return new Ptr(this.address - offset, this.map, this.ops); }
  isNull() { return this.address === 0; }
  readByteArray(size) {
    this.ops.push('read');
    const segment = this.map.get(this.address);
    const length = segment ? Math.min(size, segment.length) : 0;
    const buffer = new ArrayBuffer(length);
    if (segment) new Uint8Array(buffer).set(segment.subarray(0, length));
    return buffer;
  }
  readPointer() { this.ops.push('readPointer'); return new Ptr(0, this.map, this.ops); }
  readU8() { return 0; }
  readU32() { return 0; }
  readU64() { return { toNumber: function () { return 0; } }; }
  readUtf8String() { return ''; }
  readUtf16String() { return ''; }
  writeByteArray() {}
  writePointer() {}
  writeU8() {}
  writeU32() {}
  writeUtf8String() {}
}

function runGuard(spec) {
  const map = segments(job.guard.signatures);
  corrupt(map, spec.mutations);
  if (spec.truncate) {
    const segment = map.get(spec.truncate.rva);
    if (segment) map.set(spec.truncate.rva, segment.subarray(0, spec.truncate.length));
  }
  const ops = [];
  const sandbox = {
    Process: {
      getModuleByName: function () {
        ops.push('getModuleByName');
        return { base: new Ptr(0, map, ops) };
      },
    },
    __guardCompleted: function () { ops.push('guardCompleted'); return true; },
  };
  let error = null;
  try {
    new vm.Script(job.guard.prefix + '\n__guardCompleted();\n', { filename: 'native453.prefix.js' })
      .runInContext(vm.createContext(sandbox));
  } catch (thrown) {
    error = { message: String(thrown && thrown.message) };
  }
  return { name: spec.name, error: error, ops: ops };
}

function runFull(entry) {
  const map = segments(job.guard.signatures);
  corrupt(map, entry.mutations);
  const ops = [];
  const delegated = [];
  const stub = function () { return null; };
  let exportsObject = null;
  let bodyEnqueue = null;
  const rpc = {};
  Object.defineProperty(rpc, 'exports', {
    configurable: true,
    get: function () { return exportsObject; },
    set: function (value) {
      exportsObject = value;
      if (value && typeof value.enqueue === 'function') {
        bodyEnqueue = value.enqueue;
        value.enqueue = function (request) {
          delegated.push(JSON.parse(JSON.stringify(request === undefined ? null : request)));
          return { status: 'delegated', kind: request && request.kind };
        };
      }
    },
  });
  const sandbox = {
    Process: {
      id: 0,
      getModuleByName: function () {
        ops.push('getModuleByName');
        const base = new Ptr(0, map, ops);
        return {
          base: base,
          getExportByName: function () { return base; },
          findExportByName: function () { return base; },
        };
      },
      getCurrentThreadId: function () { ops.push('getCurrentThreadId'); return 1; },
    },
    NativeFunction: function () { ops.push('NativeFunction'); return stub; },
    NativeCallback: function () { ops.push('NativeCallback'); return stub; },
    Memory: { alloc: function () { ops.push('Memory.alloc'); return new Ptr(0, map, ops); } },
    Interceptor: {
      attach: function () { ops.push('Interceptor.attach'); return { detach: function () {} }; },
      replace: function () { ops.push('Interceptor.replace'); },
    },
    ptr: function () { return new Ptr(0, map, ops); },
    NULL: new Ptr(0, map, ops),
    send: function () {},
    console: console,
    rpc: rpc,
  };
  let error = null;
  try {
    new vm.Script(entry.source, { filename: entry.name + '.js' }).runInContext(vm.createContext(sandbox));
  } catch (thrown) {
    error = { name: thrown && thrown.name, message: String(thrown && thrown.message) };
  }
  const gate = [];
  const lookup = [];
  if (exportsObject) {
    for (const payload of entry.gate_calls || []) {
      try {
        gate.push({ payload: payload, result: exportsObject.enqueue(payload) });
      } catch (thrown) {
        gate.push({ payload: payload, error: String(thrown && thrown.message) });
      }
    }
    for (const key of entry.lookup_calls || []) {
      try {
        lookup.push({ key: key, result: exportsObject.lookup(key) });
      } catch (thrown) {
        lookup.push({ key: key, error: String(thrown && thrown.message) });
      }
    }
  }
  return {
    name: entry.name, error: error, ops: ops, delegated: delegated, gate: gate, lookup: lookup,
    has_body_enqueue: bodyEnqueue !== null, exports_assigned: exportsObject !== null,
  };
}

process.stdout.write(JSON.stringify({
  parse: (job.parse || []).map(parseOnly),
  guard: job.guard ? job.guard.cases.map(runGuard) : [],
  execute: (job.execute || []).map(runFull),
}));
"""


def _rendered(build) -> str:
    return native_builds.render_native_source(build, PACKAGE_DIR)


def _split_generated(source: str):
    """Split the reviewed 4.5.3 script into guard prefix, body and trailing gate."""
    head, found, remainder = source.partition(GUARD_END)
    assert found, "the reviewed guard block is missing"
    prefix = head + GUARD_END
    body, found_gate, gate_tail = remainder.rpartition(GATE_START)
    assert found_gate, "the reviewed rpc.exports gate is missing"
    return prefix, body, GATE_START + gate_tail


def _source_root(tmp_path: Path, **edits) -> Path:
    """A private copy of the packaged sources, so render input can be perturbed."""
    root = tmp_path / "sources"
    root.mkdir()
    for name in native_builds.SOURCES:
        text = (PACKAGE_DIR / name).read_text(encoding="utf-8")
        replacement = edits.get(name)
        if replacement is not None:
            text = replacement(text)
        (root / name).write_text(text, encoding="utf-8")
    return root


def _recording_bridge(monkeypatch, *, observation_only: bool):
    bridge = object.__new__(NativeResearchBridge)
    bridge.observation_only = observation_only
    sent = []

    def _call(self, method, *arguments):
        sent.append((method, arguments))
        return {"status": "submitted"}

    monkeypatch.setattr(NativeResearchBridge, "_call", _call)
    return bridge, sent


@pytest.fixture(scope="module")
def node_binary():
    binary = shutil.which("node")
    if binary is None:
        pytest.skip("Node.js is required to execute the generated native script offline")
    return binary


@pytest.fixture(scope="module")
def node_runs(node_binary, tmp_path_factory):
    workspace = tmp_path_factory.mktemp("native453")
    rendered_cygnus, rendered_legacy = _rendered(CYGNUS), _rendered(LEGACY)
    prefix = _split_generated(rendered_cygnus)[0]
    signatures = [[rva, expected] for rva, expected in native_builds._CORE_SIGNATURES_453]
    cases = [{"name": "match"}]
    for rva, expected in native_builds._CORE_SIGNATURES_453:
        cases.append({"name": "flip-{0:x}".format(rva),
                      "mutations": [[rva, len(expected) // 2 - 1, int(expected[-2:], 16) ^ 0xff]]})
    first_corrupt = native_builds._CORE_SIGNATURES_453[5]
    cases.append({"name": "truncated-read",
                  "truncate": {"rva": first_corrupt[0], "length": 31}})
    job = {
        "parse": [{"name": "native453", "source": rendered_cygnus},
                  {"name": "native452", "source": rendered_legacy}],
        "guard": {"prefix": prefix, "signatures": signatures, "cases": cases},
        "execute": [
            {"name": "native453", "source": rendered_cygnus,
             "gate_calls": OBSERVATION_CALLS + REFUSED_CALLS, "lookup_calls": ["tech_shields_2"]},
            {"name": "native453-corrupt", "source": rendered_cygnus,
             "mutations": [[native_builds._CORE_SIGNATURES_453[7][0], 0,
                            int(native_builds._CORE_SIGNATURES_453[7][1][:2], 16) ^ 0xff]]},
            {"name": "native452", "source": rendered_legacy,
             "gate_calls": [REFUSED_CALLS[2], REFUSED_CALLS[0], None],
             "lookup_calls": ["tech_shields_2"]},
        ],
    }
    job_path = workspace / "native453-job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    harness = workspace / "native453-harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    completed = subprocess.run([node_binary, str(harness), str(job_path)],
                               capture_output=True, text=True, timeout=240, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------- profiles


def test_exact_hashes_select_the_two_reviewed_profiles():
    assert LEGACY is not None and CYGNUS is not None
    assert (LEGACY.game_version, LEGACY.observation_only) == ("Cygnus v4.5.2", False)
    assert (CYGNUS.game_version, CYGNUS.observation_only) == ("Cygnus v4.5.3", True)
    assert native_builds.supported_versions() == ["Cygnus v4.5.2", "Cygnus v4.5.3"]
    for build in (LEGACY, CYGNUS):
        assert re.fullmatch(r"[0-9a-f]{64}", build.sha256)
    assert LEGACY.sha256 != CYGNUS.sha256
    assert native_builds.build_for_hash(LEGACY.sha256) is LEGACY
    assert native_builds.build_for_hash(CYGNUS.sha256) is CYGNUS


@pytest.mark.parametrize("digest", ["0" * 64, "", LEGACY.sha256.upper(), CYGNUS.sha256[:-1] + "0",
                                    LEGACY.sha256[:63], " " + LEGACY.sha256])
def test_unknown_hashes_are_never_mapped_to_a_profile(digest):
    assert native_builds.build_for_hash(digest) is None


def test_unreviewed_executable_bytes_are_rejected_without_being_modified(tmp_path):
    executable = tmp_path / "stellaris.exe"
    executable.write_bytes(b"unreviewed fake executable image")
    before = executable.read_bytes()
    with pytest.raises(ValueError, match="Unsupported game executable"):
        native_builds.build_for_executable(executable)
    assert executable.read_bytes() == before


def test_forged_native_build_objects_are_rejected():
    forged_version = native_builds.NativeBuild(native_builds.CYGNUS_453_SHA256, "Cygnus v4.5.2", False)
    forged_scope = native_builds.NativeBuild(native_builds.CYGNUS_453_SHA256, "Cygnus v4.5.3", False)
    invented = native_builds.NativeBuild("f" * 64, "Cygnus v4.5.3", True)
    for build in (forged_version, forged_scope, invented):
        with pytest.raises(ValueError, match="Unreviewed native profile"):
            native_builds.render_native_source(build, PACKAGE_DIR)
    with pytest.raises(TypeError):
        native_builds.NativeBuild(native_builds.CYGNUS_453_SHA256, "Cygnus v4.5.3", True,
                                  rva=0x961930)
    with pytest.raises(FrozenInstanceError):
        CYGNUS.observation_only = False
    assert (CYGNUS.game_version, CYGNUS.observation_only) == ("Cygnus v4.5.3", True)


# --------------------------------------------------------------------------- rendering


def test_legacy_render_is_byte_identical_to_the_five_packaged_sources():
    expected = "\n".join((PACKAGE_DIR / name).read_text(encoding="utf-8") for name in native_builds.SOURCES)
    rendered = _rendered(LEGACY)
    assert rendered == expected
    assert len(native_builds.SOURCES) == 5
    assert "0x6d1b90" in rendered and "0x6d2a80" not in rendered
    assert GUARD_ERROR not in rendered and "rpc.exports.enqueue =" not in rendered


def test_453_render_translates_every_reviewed_address():
    assert len(native_builds._RVA_453) == 56
    rendered = _rendered(CYGNUS)
    token = re.compile(r"/\*.*?\*/|//[^\n]*|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|0x[0-9a-fA-F]+",
                       re.DOTALL)
    literals = [text for text in token.findall(rendered) if text.startswith("0x")]
    addresses = {int(text, 16) for text in literals}
    translated = set(native_builds._RVA_453.values())
    # Only reviewed large addresses move; small structure offsets stay untouched.
    assert {address for address in addresses if address > 0x150000} == translated | {0xffffff}
    assert not addresses & set(native_builds._RVA_453)
    for old in native_builds._RVA_453:
        assert hex(old) not in rendered
    for new in translated:
        assert hex(new) in rendered


def test_453_render_rejects_an_unmapped_new_address(tmp_path):
    def edit(text):
        return text + "\nconst unreviewed = base.add(0x999999);\n"

    root = _source_root(tmp_path, **{"native_fleet.js": edit})
    with pytest.raises(ValueError, match="Unmapped native source address"):
        native_builds.render_native_source(CYGNUS, root)


def test_453_render_rejects_a_missing_reviewed_address(tmp_path):
    def edit(text):
        assert "base.add(0x6d1b90)" in text
        return text.replace("base.add(0x6d1b90)", "base.add(0)")

    root = _source_root(tmp_path, **{"native_research.js": edit})
    with pytest.raises(ValueError, match="Native source address inventory changed"):
        native_builds.render_native_source(CYGNUS, root)


def test_453_render_never_rewrites_comments_or_string_literals(tmp_path):
    note = ("// Legacy disassembly kept as evidence: 0x6d1b90 and 0x15bcf0 are the 4.5.2 RVAs.\n"
            "const disassemblyNote = '0x6d1b90 stays quoted, 0x15bcf0 too';\n")

    root = _source_root(tmp_path, **{"native_ship.js": lambda text: note + text})
    rendered = native_builds.render_native_source(CYGNUS, root)
    assert note in rendered
    assert "0x6d1b90" in rendered and "0x15bcf0" in rendered
    assert "base.add(0x6d1b90)" not in rendered
    assert "0x6d2a80" in rendered


def test_453_render_prefix_and_trailing_gate_are_reviewed():
    rendered = _rendered(CYGNUS)
    prefix, body, gate = _split_generated(rendered)
    assert rendered == prefix + body + gate
    assert prefix.startswith("'use strict';\n/* Reviewed 4.5.3 translation; observation/checkpoint capability only. */")
    assert GUARD_ERROR in prefix
    assert "NativeFunction" not in prefix and "Interceptor" not in prefix
    assert body.lstrip().startswith("/* Exact-build research command experiment")
    assert "rpc.exports =" in body
    for kind in ("request_observation", "run_inbox", "load_attached_events"):
        assert "'" + kind + "'" in gate
    assert "request.paused === true" in gate
    assert LOOKUP_ERROR in gate
    assert "rpc.exports.enqueue =" not in _rendered(LEGACY)


# --------------------------------------------------------------------------- node


def test_generated_scripts_compile_in_node_vm(node_runs):
    parsed = {entry["name"]: entry for entry in node_runs["parse"]}
    assert set(parsed) == {"native452", "native453"}
    assert parsed["native452"]["ok"] is True and parsed["native453"]["ok"] is True


def test_guard_covers_the_32_reviewed_live_code_prefixes():
    signatures = native_builds._CORE_SIGNATURES_453
    assert len(signatures) == 32
    assert {rva for rva, _ in signatures} <= set(native_builds._RVA_453.values())
    assert all(re.fullmatch(r"[0-9a-f]{64}", expected) for _, expected in signatures)
    rendered = _rendered(CYGNUS)
    head, found, tail = rendered.partition("for (const [rva, expected] of ")
    assert found and head
    embedded, separator, _ = tail.partition(") {\n")
    assert separator
    assert json.loads(embedded) == [[rva, expected] for rva, expected in signatures]


def test_guard_passes_only_after_reading_every_reviewed_prefix(node_runs):
    result = {case["name"]: case for case in node_runs["guard"]}["match"]
    assert result["error"] is None
    assert result["ops"] == ["getModuleByName"] + ["read"] * 32 + ["guardCompleted"]


def test_every_mismatching_prefix_aborts_before_native_functions(node_runs):
    cases = {case["name"]: case for case in node_runs["guard"]}
    for index, (rva, _) in enumerate(native_builds._CORE_SIGNATURES_453):
        name = "flip-{0:x}".format(rva)
        case = cases[name]
        assert case["error"] == {"message": GUARD_ERROR}, name
        assert case["ops"][-1] == "read" and len(case["ops"]) == index + 2, name
        assert set(case["ops"]) == {"getModuleByName", "read"}, name
        assert "guardCompleted" not in case["ops"], name
    truncated = cases["truncated-read"]
    assert truncated["error"] == {"message": GUARD_ERROR}
    assert set(truncated["ops"]) == {"getModuleByName", "read"}
    assert len(truncated["ops"]) == 7


def test_generated_script_reads_the_reviewed_prefixes_before_the_first_native_call(node_runs):
    entry = {case["name"]: case for case in node_runs["execute"]}["native453"]
    assert entry["error"] is None
    ops = entry["ops"]
    assert ops[0] == "getModuleByName"
    assert ops[1:33] == ["read"] * 32
    assert ops[33] == "getModuleByName"
    assert ops.index("NativeFunction") == 34
    assert "Interceptor.attach" in ops


def test_corrupt_live_code_stops_the_composed_script_before_native_calls(node_runs):
    entry = {case["name"]: case for case in node_runs["execute"]}["native453-corrupt"]
    assert entry["error"] == {"name": "Error", "message": GUARD_ERROR}
    assert entry["ops"] == ["getModuleByName"] + ["read"] * 8
    assert "NativeFunction" not in entry["ops"] and "Interceptor.attach" not in entry["ops"]
    assert entry["delegated"] == []


def test_trailing_gate_only_forwards_observation_and_verified_pause(node_runs):
    entry = {case["name"]: case for case in node_runs["execute"]}["native453"]
    assert entry["has_body_enqueue"] is True and entry["exports_assigned"] is True
    assert entry["delegated"] == OBSERVATION_CALLS
    results = entry["gate"]
    assert len(results) == len(OBSERVATION_CALLS) + len(REFUSED_CALLS)
    for item in results[:len(OBSERVATION_CALLS)]:
        assert item["result"] == {"status": "delegated", "kind": item["payload"]["kind"]}
    for item in results[len(OBSERVATION_CALLS):]:
        assert item["error"] == GATE_ERROR, item["payload"]
    assert entry["lookup"] == [{"key": "tech_shields_2", "error": LOOKUP_ERROR}]


def test_legacy_script_keeps_its_full_native_capability(node_runs):
    entry = {case["name"]: case for case in node_runs["execute"]}["native452"]
    assert entry["error"] is None
    assert entry["has_body_enqueue"] is True
    assert len(entry["delegated"]) == 3
    assert all("error" not in item for item in entry["gate"])
    assert entry["gate"][0]["payload"]["kind"] == "select_research"
    assert entry["lookup"] == [{"key": "tech_shields_2",
                                "result": {"found": False, "technology": "tech_shields_2"}}]


# --------------------------------------------------------------------------- bridge


WRITE_SELECTIONS = [
    ResearchSelection("physics", "tech_shields_2"),
    DistrictConstruction("1", "district_city", "2", "3"),
    BuildingConstruction("1", "4", "building_farm", "2", "3"),
    ShipConstruction("1", "2", "3", "4"),
]


@pytest.mark.parametrize("selection", WRITE_SELECTIONS, ids=["research", "district", "building", "ship"])
def test_observation_only_bridge_refuses_gameplay_writes_without_rpc(monkeypatch, selection):
    bridge, sent = _recording_bridge(monkeypatch, observation_only=True)
    with pytest.raises(ValueError, match="observation, verified pause and checkpoints only"):
        bridge.submit(selection, TOKEN)
    assert sent == []


def test_observation_only_bridge_refuses_unpausing_without_rpc(monkeypatch):
    bridge, sent = _recording_bridge(monkeypatch, observation_only=True)
    with pytest.raises(ValueError, match="verified pause and checkpoints only"):
        bridge.submit(PauseRequest(False), TOKEN)
    assert sent == []
    with pytest.raises(ValueError, match="verified pause and checkpoints only"):
        bridge.submit(PauseRequest(False), "not-a-token")
    assert sent == []


def test_observation_only_bridge_keeps_observation_channels(monkeypatch):
    bridge, sent = _recording_bridge(monkeypatch, observation_only=True)
    bridge.request_dump(TOKEN)
    bridge.run_inbox(TOKEN)
    assert [entry[0] for entry in sent] == ["enqueue", "enqueue"]
    assert [entry[1][0]["kind"] for entry in sent] == ["request_observation", "run_inbox"]


def test_observation_only_bridge_sends_verified_pause_as_the_original_native_kind(monkeypatch):
    bridge, sent = _recording_bridge(monkeypatch, observation_only=True)
    assert bridge.submit(PauseRequest(True), TOKEN) == {"status": "submitted"}
    assert len(sent) == 1
    method, arguments = sent[0]
    assert method == "enqueue"
    assert arguments == ({"kind": "set_paused", "token": TOKEN, "country_id": "0", "paused": True},)
    assert type(arguments[0]["paused"]) is bool


def test_legacy_bridge_keeps_its_full_native_capability(monkeypatch):
    bridge, sent = _recording_bridge(monkeypatch, observation_only=False)
    bridge.submit(ResearchSelection("physics", "tech_shields_2"), TOKEN)
    bridge.submit(PauseRequest(False), TOKEN)
    bridge.submit(DistrictConstruction("1", "district_city", "2", "3"), TOKEN)
    assert [entry[1][0]["kind"] for entry in sent] == ["select_research", "set_paused", "queue_district"]
    assert sent[1][1][0]["paused"] is False


def test_native_bridge_accepts_no_profile_or_raw_address(monkeypatch):
    assert set(inspect.signature(NativeResearchBridge.__init__).parameters) == {
        "self", "process", "executable", "state_dir", "initialization_deadline", "cancel_event"}
    assert list(inspect.signature(NativeResearchBridge.submit).parameters) == ["self", "selection", "token"]
    with pytest.raises(TypeError):
        PauseRequest(True, "0x961930")
    with pytest.raises(TypeError):
        ResearchSelection("physics", "tech_shields_2", "0x6d1b90")
    bridge, sent = _recording_bridge(monkeypatch, observation_only=False)
    bridge.submit(ResearchSelection("physics", "tech_shields_2"), TOKEN)
    payload = sent[0][1][0]
    assert set(payload) == {"kind", "token", "country_id", "area", "technology"}
    assert not {"rva", "address", "profile", "sha256", "script", "js"} & set(payload)
    with pytest.raises(ValueError, match="Invalid native operation token"):
        bridge.submit(ResearchSelection("physics", "tech_shields_2"), "0x6d1b90")


def _initialization_bridge(monkeypatch, *, attached, reply=None, deadline=None):
    bridge = object.__new__(NativeResearchBridge)
    bridge._deadline = deadline
    bridge._cancel_event = None
    bridge.game = None
    if attached:
        bridge._attached_initialization = True
    calls = []

    def _call(self, method, *arguments):
        calls.append((method, arguments))
        if reply is not None:
            return reply
        return {"status": "attachment_initialized",
                "runtime": {"paused": True, "protected_pause": False}}

    monkeypatch.setattr(NativeResearchBridge, "_call", _call)
    return bridge, calls


def test_finish_initialization_sends_one_finish_attachment_rpc(monkeypatch):
    bridge, calls = _initialization_bridge(monkeypatch, attached=True, deadline=time.monotonic() + 30)
    bridge.finish_initialization()
    assert len(calls) == 1
    method, arguments = calls[0]
    assert method == "finish_attachment"
    assert len(arguments) == 1 and re.fullmatch(r"[0-9a-f]{32}", arguments[0])
    assert bridge._attached_initialization is False
    assert bridge._deadline is None and bridge._cancel_event is None


@pytest.mark.parametrize("reply", [
    {}, {"status": "attachment_initialized"}, {"status": "attachment_initialized", "runtime": {}},
    {"status": "attachment_initialized", "runtime": {"paused": False}},
    {"status": "attachment_initialized", "runtime": {"paused": 1}},
    {"status": "window_bound", "runtime": {"paused": True}},
], ids=["empty", "no-runtime", "runtime-empty", "unpaused", "non-boolean", "wrong-status"])
def test_finish_initialization_refuses_an_unverified_attachment_reply(monkeypatch, reply):
    deadline = time.monotonic() + 30
    bridge, calls = _initialization_bridge(monkeypatch, attached=True, reply=reply, deadline=deadline)
    with pytest.raises(ValueError, match="Attached initialization did not finish with verified pause"):
        bridge.finish_initialization()
    assert len(calls) == 1
    assert bridge._attached_initialization is True
    assert bridge._deadline == deadline


def test_finish_initialization_is_idle_for_a_launcher_owned_game(monkeypatch):
    bridge, calls = _initialization_bridge(monkeypatch, attached=False, deadline=time.monotonic() + 30)
    bridge.finish_initialization()
    assert calls == []
    assert bridge._deadline is None and bridge._cancel_event is None


def test_only_an_attached_game_marks_the_initialization_pending(monkeypatch):
    from stellaris_autopilot.attached_process import AttachedGameProcess

    bridge, calls = _initialization_bridge(monkeypatch, attached=False)
    with pytest.raises(ValueError, match="Window discovery is only for a verified attached game"):
        bridge.bind_existing_window(TOKEN)
    assert calls == [] and getattr(bridge, "_attached_initialization", False) is False
    bridge.game = object.__new__(AttachedGameProcess)
    bridge.bind_existing_window(TOKEN)
    assert calls == [("bind_window", (TOKEN,))]
    assert bridge._attached_initialization is True


# --------------------------------------------------------------------------- direct


def _direct_config(tmp_path: Path, **overrides) -> Path:
    user_dir = tmp_path / "user directory"
    state_dir = tmp_path / "state"
    user_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)
    raw = {
        "user_dir": str(user_dir), "state_dir": str(state_dir), "country_id": "0",
        "campaign_id": "campaign", "verified_game_version": "Cygnus v4.5.3",
        "session_token": "b" * 32, "allowed_actions": ["probe_connection"], "allowed_orders": [],
        "allowed_native_actions": [], "single_player_confirmed": True,
        "non_ironman_confirmed": True,
    }
    raw.update(overrides)
    path = tmp_path / "direct.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


@pytest.fixture
def direct():
    return pytest.importorskip("stellaris_autopilot.direct_mcp")


@pytest.fixture
def spy(monkeypatch, direct):
    """Record dispatch into _execute without changing the production gate."""
    executed = []

    def _execute(self, action_id, *, order=None, selection=None):
        executed.append({"action": action_id, "order": order, "selection": selection})
        return {"status": "executed"}

    monkeypatch.setattr(direct.DirectConnection, "_execute", _execute)
    return direct, executed


def test_set_paused_false_is_blocked_before_execute_on_observation_only(tmp_path, spy):
    direct_mcp, executed = spy
    bridge = SimpleNamespace(observation_only=True)
    connection = direct_mcp.DirectConnection(_direct_config(tmp_path), native_bridge=bridge)
    result = connection.set_paused(False)
    assert result["status"] == "blocked" and "observation" in result["reason"]
    assert executed == []
    assert connection.set_paused(True) == {"status": "executed"}
    assert [entry["action"] for entry in executed] == ["set_paused"]
    assert executed[0]["selection"] == PauseRequest(True)


def test_set_paused_false_still_executes_on_a_legacy_build(tmp_path, spy):
    direct_mcp, executed = spy
    connection = direct_mcp.DirectConnection(_direct_config(tmp_path),
                                             native_bridge=SimpleNamespace(observation_only=False))
    assert connection.set_paused(False) == {"status": "executed"}
    assert executed[0]["selection"] == PauseRequest(False)


@pytest.mark.parametrize("action,selection", [
    ("select_research", ResearchSelection("physics", "tech_shields_2")),
    ("queue_district", DistrictRequest("1", "district_city")),
    ("queue_building", BuildingRequest("1", "4", "building_farm")),
    ("set_paused", PauseRequest(False)),
], ids=["research", "district", "building", "pause-false"])
def test_execute_gate_refuses_observation_only_gameplay_without_reading_config(
        tmp_path, direct, action, selection):
    path = _direct_config(tmp_path)
    connection = direct.DirectConnection(path, native_bridge=SimpleNamespace(observation_only=True))
    path.unlink()
    assert connection._execute(action, selection=selection) == {
        "status": "blocked",
        "reason": "This build permits observation, pause and checkpoints; "
                  "gameplay actions need separate live verification"}


def test_execute_gate_refuses_orders_and_holding_on_observation_only(tmp_path, direct):
    path = _direct_config(tmp_path)
    connection = direct.DirectConnection(path, native_bridge=SimpleNamespace(observation_only=True))
    path.unlink()
    assert connection._execute("hold_military_fleets")["status"] == "blocked"
    assert connection._execute("fleet_wait", order=object())["status"] == "blocked"


def test_execute_gate_lets_probe_and_verified_pause_reach_the_local_allowlist(tmp_path, direct):
    connection = direct.DirectConnection(_direct_config(tmp_path, allowed_actions=[]),
                                         native_bridge=SimpleNamespace(observation_only=True))
    for result in (connection._execute("probe_connection"),
                   connection._execute("set_paused", selection=PauseRequest(True))):
        assert result == {"status": "blocked", "reason": "Direct action is not locally authorized"}


@pytest.mark.parametrize("method", ["select_research", "queue_district", "queue_building"])
def test_public_gameplay_methods_are_blocked_on_observation_only(tmp_path, direct, method):
    connection = direct.DirectConnection(_direct_config(tmp_path),
                                         native_bridge=SimpleNamespace(observation_only=True))
    arguments = {"select_research": ("physics", "tech_shields_2"), "queue_district": ("1", "district_city"),
                 "queue_building": ("1", "4", "building_farm")}[method]
    result = getattr(connection, method)(*arguments)
    assert result["status"] == "blocked" and "observation" in result["reason"]


# --------------------------------------------------------------------------- attached


class _FakeProcess:
    def __init__(self):
        self.pid = 4242
        self.closed = False

    def poll(self):
        return None

    def close(self):
        self.closed = True


class _FakeNative:
    game_version = "Cygnus v4.5.3"
    observation_only = True

    def __init__(self, binding):
        self.binding = binding
        self.calls = []
        self.closed = False

    def bind_existing_window(self, token):
        self.calls.append(("bind_existing_window", token))
        return self.binding

    def request_dump(self, token):
        self.calls.append(("request_dump", token))
        return {"status": "dump_requested"}

    def result(self, token):
        self.calls.append(("result", token))
        return {"status": "dump_requested", "kind": "request_observation", "token": token,
                "country_id": "0", "execution_source": "window_message"}

    def run_inbox(self, token):
        self.calls.append(("run_inbox", token))
        return {"status": "inbox_executed"}

    def load_attached_events(self, token):
        self.calls.append(("load_attached_events", token))
        return {"status": "events_load_returned"}

    def close(self):
        self.closed = True


def _attached_runtime(tmp_path: Path, binding, *, seconds=None):
    user_dir = tmp_path / "user"
    user_dir.mkdir(exist_ok=True)
    process, native = _FakeProcess(), _FakeNative(binding)
    deadline = None if seconds is None else time.monotonic() + seconds
    return AttachedRuntime(process, native, user_dir, tmp_path / "state", "c" * 32, deadline=deadline)


@pytest.mark.parametrize("binding", [
    None, {}, {"runtime": {}}, {"runtime": {"paused": False}}, {"runtime": {"paused": 1}},
    {"runtime": {"paused": "yes"}}, {"runtime": {"protected_pause": True}}, "window_bound",
], ids=["none", "empty", "no-runtime", "false", "int", "string", "missing", "not-a-mapping"])
def test_window_binding_must_prove_pause_before_any_dump(tmp_path, binding):
    runtime = _attached_runtime(tmp_path, binding)
    with pytest.raises(CampaignError, match="Pause the loaded game before connecting"):
        runtime.initialize()
    assert [entry[0] for entry in runtime.native.calls] == ["bind_existing_window"]
    assert not (runtime.user_dir / "save games" / "dump_qs_direct.sav.txt").exists()
    assert not (runtime.state_dir / "before-attachment.sav").exists()
    assert not (runtime.state_dir / "direct.json").exists()
    assert runtime.connection is None
    runtime.close()
    assert runtime.native.closed is True and runtime.process.closed is True
    assert not (runtime.user_dir / ".qs_existing_controller.lock").exists()


def test_window_binding_with_verified_pause_allows_the_first_dump(tmp_path):
    runtime = _attached_runtime(tmp_path, {"runtime": {"paused": True, "protected_pause": False}},
                                seconds=0.5)
    with pytest.raises(CampaignError, match="No fresh matching dump"):
        runtime.initialize()
    names = [entry[0] for entry in runtime.native.calls]
    assert names[0] == "bind_existing_window"
    assert names.count("request_dump") == 1
    assert "run_inbox" not in names and "load_attached_events" not in names
    assert runtime.connection is None
    runtime.close()
    assert not (runtime.user_dir / ".qs_existing_controller.lock").exists()


# --------------------------------------------------------------------------- diagnostics


@pytest.mark.parametrize("sha256,version,scope", [
    (native_builds.CYGNUS_453_SHA256, "Cygnus v4.5.3", "observation_pause_checkpoint_only"),
    (native_builds.LEGACY_SHA256, "Cygnus v4.5.2", "locally_authorized_actions"),
])
def test_diagnostics_report_the_reviewed_control_scope(tmp_path, monkeypatch, sha256, version, scope):
    executable = tmp_path / "stellaris.exe"
    executable.write_bytes(b"synthetic executable bytes")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    reviewed = native_builds.build_for_hash(sha256)
    monkeypatch.setattr(connection_diagnostics, "build_for_hash",
                        lambda value: reviewed if value == digest else None)
    evidence = connection_diagnostics.executable_evidence(executable)
    assert evidence["supported"] is True
    assert evidence["requires_native_adapter"] is False
    assert evidence["supported_game_version"] == version
    assert evidence["supported_sha256"] == sha256
    assert evidence["control_scope"] == scope
