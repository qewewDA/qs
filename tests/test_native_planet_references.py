"""Run the native identity guard against bounded, independently built memory.

This exercises target joins and stale-handle rejection without Frida or a game
process. The actual native layout is supported by recorded PE evidence and must
also pass a separate live trial.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
SOURCE = Path(__file__).resolve().parents[1] / "src" / "stellaris_autopilot" / "native_planet.js"
HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
const memory = new Map();
const keys = new Map([[0x60000, 'zone_industrial'], [0x90000, 'building_foundry_1']]);
class Pointer {
    constructor(value) { this.value = value; }
    add(offset) { return new Pointer(this.value + offset); }
    isNull() { return this.value === 0; }
    readPointer() { return new Pointer(this.readNumber()); }
    readU32() {
        const value = this.readNumber();
        if (!Number.isInteger(value) || value < 0 || value > 0xffffffff) throw Error('Invalid U32');
        return value;
    }
    readS32() {
        const value = this.readNumber();
        if (!Number.isInteger(value) || value < -0x80000000 || value > 0x7fffffff) throw Error('Invalid S32');
        return value;
    }
    readNumber() {
        if (!memory.has(this.value)) throw Error('Unmapped native memory');
        return memory.get(this.value);
    }
}
const put = (address, value) => memory.set(address, value);
const base = new Pointer(0x140000000);
let poolAddress = 0xa0000;
function pool(rva, count, objects) {
    const address = poolAddress;
    poolAddress += 0x1000;
    put(base.value + rva, address);
    put(address + 0x20, count);
    put(address + 0x18, address + 0x100);
    for (const [handle, object] of objects) {
        put(address + 0x100 + (handle & 0xffffff) * 16 + 8, object);
    }
}
pool(0x3114cd8, 1, [[0, 0x30000]]);
pool(0x3114d18, 3, [[2, 0x50000]]);
pool(0x3114ce0, 2, [[1, 0x80000]]);
// A real colony, district and zone are separate persistent object handles.
put(0x10000 + 0x88, 0x20000); put(0x10000 + 0x94, 1); put(0x20000, 0);
put(0x30000 + 8, 0); put(0x30000 + 0x30, 0x40000); put(0x30000 + 0x3c, 1); put(0x40000, 2);
put(0x50000 + 8, 2); put(0x50000 + 0x20, 0x60000);
// Zone+0x30/+0x3c are another native structure, not a building vector.
put(0x50000 + 0x30, 0xdeadbeef); put(0x50000 + 0x3c, 0x7fffffff);
put(0x50000 + 0x1a8, 0x70000); put(0x50000 + 0x1b4, 1); put(0x70000, 1);
put(0x80000 + 8, 1); put(0x80000 + 0x20, 0x90000); put(0x90000 + 0x1940, 0);
const request = {kind: 'upgrade_building', district_id: '0', slot_index: 0,
    zone_id: '2', source_zone: 'zone_industrial', building_id: '1', source_building: 'building_foundry_1'};
if (scenario === 'other_building') put(0x70000, 99);
if (scenario === 'stale_building_handle') put(0x80000 + 8, 0x01000001);
if (scenario === 'wrong_slot') put(0x40000, 3);
if (scenario === 'duplicate_building') { put(0x50000 + 0x1b4, 2); put(0x70000 + 4, 1); }
if (scenario === 'wrong_zone_type') keys.set(0x60000, 'zone_research');
if (scenario === 'wrong_building_type') keys.set(0x90000, 'building_foundry_2');
if (scenario === 'nonordinary_building') put(0x90000 + 0x1940, 1);
if (scenario === 'missing_buildings') memory.delete(0x50000 + 0x1b4);
if (scenario === 'old_vector_decoy') {
    put(0x50000 + 0x30, 0x71000); put(0x50000 + 0x3c, 1); put(0x71000, 1); put(0x70000, 99);
}
if (scenario === 'conversion_without_buildings') {
    request.kind = 'convert_zone'; memory.delete(0x50000 + 0x1b4);
}
const context = {base, request, colony: new Pointer(0x10000), keyAt: pointer => keys.get(pointer.value)};
try {
    vm.runInNewContext(source + '\nplanetExactReferences(request, colony);', context);
    process.stdout.write(JSON.stringify({accepted: true}));
} catch (error) {
    process.stdout.write(JSON.stringify({accepted: false, reason: String(error)}));
}
"""


@pytest.mark.skipif(NODE is None, reason="Native JavaScript identity guard checks require Node.js")
@pytest.mark.parametrize("scenario,accepted", [
    ("valid", True), ("other_building", False), ("stale_building_handle", False),
    ("wrong_slot", False), ("duplicate_building", False), ("wrong_zone_type", False),
    ("wrong_building_type", False), ("nonordinary_building", False), ("missing_buildings", False),
    ("old_vector_decoy", False), ("conversion_without_buildings", True),
])
def test_native_zone_building_guard_uses_exact_live_membership(scenario, accepted):
    result = subprocess.run([NODE, "-e", HARNESS, str(SOURCE), scenario], capture_output=True, text=True,
                            timeout=10, check=True)
    assert json.loads(result.stdout)["accepted"] is accepted
