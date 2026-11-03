"""Exercise the actual fixed native fleet preparation with bounded memory.

The independent fixture includes separate owner/controller references and the
recorded native factory defaults. No game process is started or attached.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
SOURCE = Path(__file__).resolve().parents[1] / "src" / "stellaris_autopilot" / "native_fleet.js"
HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const kind = process.argv[2];
const scenario = process.argv[3];
const memory = new Map();
class Pointer {
    constructor(value) { this.value = value; }
    add(offset) { return new Pointer(this.value + offset); }
    isNull() { return this.value === 0; }
    readNumber() {
        if (!memory.has(this.value)) throw Error('Unmapped native memory');
        return memory.get(this.value);
    }
    readPointer() { return new Pointer(this.readNumber()); }
    readU32() { return this.readNumber(); }
    writeU32(value) { memory.set(this.value, value); }
    writeU8(value) { memory.set(this.value, value); }
}
const base = new Pointer(0x140000000);
const put = (address, value) => memory.set(address, value);
let allocations = 0;
let destructions = 0;
function pool(rva, address, handle, object, identityOffset) {
    put(base.value + rva, address);
    put(address + 0x20, (handle & 0xffffff) + 1);
    put(address + 0x18, address + 0x100);
    put(address + 0x100 + (handle & 0xffffff) * 16 + 8, object);
    put(object + identityOffset, handle);
}
pool(0x31129d8, 0x10000, 0x01000001, 0x30000, 0x30);
pool(0x3114e10, 0x20000, 0x02000003, 0x40000, 0x18);
put(0x30000 + 0x448, 0); // Controller: native legality and payment country.
put(0x30000 + 0x44c, 0); // Owner: distinct from temporary fleet control.
put(0x40000 + 0x20, 0x60000);
put(0x60000 + 8, base.value + 0x1000);
put(0x50000 + 0x20, 92);
function factory() {
    allocations++;
    const address = 0x70000;
    put(address + 0x20, 0xffffffff);
    if (kind === 'colonize_planet') {
        put(address + 0x24, 0xffffffff);
        put(address + 0x28, 0); put(address + 0x29, 0);
        put(address + 0x30, 0xabcdef);
    } else {
        put(address + 0x28, 0xabcdef);
        put(address + 0x30, 3); put(address + 0x34, 0xffffffff); put(address + 0x38, 0xffffffff);
        put(address + 0x3c, kind === 'build_station' ? 5 : 0);
        put(address + 0x3d, 0); put(address + 0x3e, 0);
    }
    return new Pointer(address);
}
function NativeFunction(address) {
    const rva = address.value - base.value;
    if (rva === 0x1000) return () => new Pointer(0x50000);
    if ([0xa709b0, 0xa70830, 0xa708a0].includes(rva)) return factory;
    if ([0xa5b9a0, 0xa590c0, 0xa5a430].includes(rva)) return () => 1;
    throw Error('Unexpected native function');
}
class NativePreconditionError extends Error {}
const request = {kind, country_id: '0', fleet_id: '16777217', planet_id: '33554435', system_id: '92',
    token: '0123456789abcdef0123456789abcdef'};
if (kind === 'build_station') request.station_type = 'research';
if (scenario === 'foreign_owner') put(0x30000 + 0x44c, 1);
if (scenario === 'foreign_controller') put(0x30000 + 0x448, 1);
if (scenario === 'stale_fleet') put(0x30000 + 0x30, 1);
if (scenario === 'stale_planet') put(0x40000 + 0x18, 3);
if (scenario === 'wrong_system') put(0x50000 + 0x20, 247);
const context = {base, NativeFunction, NativePreconditionError, request,
    validId: value => typeof value === 'string' && /^(0|[1-9][0-9]*)$/.test(value),
    destroyUnsubmitted: () => { destructions++; }};
try {
    const prepared = vm.runInNewContext(source + '\nprepareFleetCommand(request);', context);
    const fields = {};
    for (const offset of [0x20, 0x24, 0x28, 0x29, 0x30, 0x34, 0x38, 0x3c, 0x3d, 0x3e]) {
        if (memory.has(prepared.command.value + offset)) fields[offset] = memory.get(prepared.command.value + offset);
    }
    process.stdout.write(JSON.stringify({accepted: true, allocations, destructions, fields}));
} catch (error) {
    process.stdout.write(JSON.stringify({accepted: false, allocations, destructions,
        precondition: error instanceof NativePreconditionError, reason: String(error)}));
}
"""


@pytest.mark.skipif(NODE is None, reason="Native JavaScript identity guard checks require Node.js")
@pytest.mark.parametrize("kind", ["survey_planet", "build_station", "colonize_planet"])
@pytest.mark.parametrize("scenario", ["valid", "foreign_owner", "foreign_controller", "stale_fleet",
                                      "stale_planet", "wrong_system"])
def test_native_fleet_guard_binds_owner_controller_full_handles_and_physical_system(kind, scenario):
    result = subprocess.run([NODE, "-e", HARNESS, str(SOURCE), kind, scenario], capture_output=True,
                            text=True, timeout=10, check=True)
    actual = json.loads(result.stdout)
    assert actual["accepted"] is (scenario == "valid")
    assert actual["destructions"] == 0
    if scenario != "valid":
        assert actual["precondition"] is True
        assert actual["allocations"] == 0
        return
    assert actual["allocations"] == 1
    fields = actual["fields"]
    assert fields[str(0x20)] == 16777217
    if kind == "colonize_planet":
        assert fields[str(0x24)] == 33554435
        assert fields[str(0x28)] == fields[str(0x29)] == 0
        assert fields[str(0x30)] == 0xabcdef  # Preserve the factory CName.
    else:
        assert fields[str(0x28)] == 0xabcdef  # Preserve the reference interface.
        assert fields[str(0x30)] == 0 and fields[str(0x34)] == 33554435
        assert fields[str(0x38)] == 4294967295  # Exact body, no system expansion.
        assert fields[str(0x3d)] == fields[str(0x3e)] == 0
        assert fields[str(0x3c)] == (6 if kind == "build_station" else 0)
