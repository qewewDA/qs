/* Pinned 4.5.2 normal fleet commands. Appended to native_research.js by the
 * isolated worker; callers never supply JavaScript, addresses or coordinates.
 * These factories/validators follow the normal game buttons. Submission must
 * still be verified against a fresh dump by fleet_native.py.
 */
'use strict';

const qsFleetKinds = new Set(['survey_planet', 'build_station', 'colonize_planet']);
const qsStationClasses = Object.freeze({mining: 5, research: 6, outpost: 10});
const qsFleetFactories = Object.freeze({
    survey_planet: new NativeFunction(base.add(0xa709b0), 'pointer', [], 'win64'),
    build_station: new NativeFunction(base.add(0xa70830), 'pointer', [], 'win64'),
    colonize_planet: new NativeFunction(base.add(0xa708a0), 'pointer', [], 'win64')
});
const qsFleetValidators = Object.freeze({
    survey_planet: new NativeFunction(base.add(0xa5b9a0), 'uint8', ['pointer', 'pointer'], 'win64'),
    build_station: new NativeFunction(base.add(0xa590c0), 'uint8', ['pointer', 'pointer'], 'win64'),
    colonize_planet: new NativeFunction(base.add(0xa5a430), 'uint8', ['pointer', 'pointer'], 'win64')
});

function canonicalFleetRequest(request) {
    if (request === null || typeof request !== 'object' || !qsFleetKinds.has(request.kind)) return null;
    const expected = request.kind === 'build_station'
        ? 'country_id,fleet_id,kind,planet_id,station_type,system_id,token'
        : 'country_id,fleet_id,kind,planet_id,system_id,token';
    if (Array.isArray(request) || Object.keys(request).sort().join(',') !== expected || request.country_id !== '0' ||
        typeof request.token !== 'string' || !/^[0-9a-f]{32}$/.test(request.token) ||
        !validId(request.fleet_id) || !validId(request.planet_id) || !validId(request.system_id) ||
        (request.kind === 'build_station' && !Object.hasOwn(qsStationClasses, request.station_type))) {
        throw new Error('Malformed typed fleet request');
    }
    return {kind: request.kind, country_id: '0', fleet_id: request.fleet_id,
        planet_id: request.planet_id, system_id: request.system_id,
        ...(request.kind === 'build_station' ? {station_type: request.station_type} : {}), token: request.token};
}

function qsFleetPoolObject(globalRva, handle, identityOffset) {
    const pool = base.add(globalRva).readPointer();
    if (pool.isNull()) return null;
    const index = Number(handle) & 0xffffff;
    const count = pool.add(0x20).readU32();
    const entries = pool.add(0x18).readPointer();
    if (entries.isNull() || count > 1000000) throw new Error('Unrecognized native object pool');
    if (index >= count) return null;
    const object = entries.add(index * 16 + 8).readPointer();
    return object.isNull() || object.add(identityOffset).readU32() !== Number(handle) ? null : object;
}

function qsPlanetSystem(planet) {
    const receiver = planet.add(0x20);
    const getter = receiver.readPointer().add(8).readPointer();
    const coordinate = new NativeFunction(getter, 'pointer', ['pointer'], 'win64')(receiver);
    if (coordinate.isNull()) throw new Error('Planet coordinate is unavailable');
    return coordinate.add(0x20).readU32();
}

function prepareFleetCommand(request) {
    const canonical = canonicalFleetRequest(request);
    if (canonical === null) throw new Error('Unknown fleet command kind');
    const fleet = qsFleetPoolObject(0x31129d8, request.fleet_id, 0x30);
    const planet = qsFleetPoolObject(0x3114e10, request.planet_id, 0x18);
    // Normal command legality/payment uses controller (+0x448). Ownership
    // (+0x44c) is separate: borrowed/controlled foreign fleets stay excluded.
    if (fleet === null || planet === null || fleet.add(0x448).readU32() !== 0 ||
        fleet.add(0x44c).readU32() !== 0 ||
        qsPlanetSystem(planet) !== Number(request.system_id)) {
        throw new NativePreconditionError('No matching owned/controlled fleet and exact physical planet/system identity');
    }
    let command = null;
    try {
        command = qsFleetFactories[request.kind]();
        if (command.isNull()) throw new Error('Fleet command allocation failed');
        command.add(0x20).writeU32(Number(request.fleet_id));
        if (request.kind === 'colonize_planet') {
            command.add(0x24).writeU32(Number(request.planet_id));
            // Factory CName at +0x30 stays empty, preserving game's naming.
            // Both normal queue modifiers stay zero: replace, queue at back.
        } else {
            command.add(0x30).writeU32(0); // Deposit holder type: physical planet.
            command.add(0x34).writeU32(Number(request.planet_id));
            // +0x38 stays unset: one exact body, no all-system expansion.
            if (request.kind === 'build_station') command.add(0x3c).writeU8(qsStationClasses[request.station_type]);
        }
        return {command, validate: qsFleetValidators[request.kind],
            meta: {native_fleet_handle: request.fleet_id, native_planet_handle: request.planet_id,
                native_system_handle: request.system_id}};
    } catch (error) {
        if (command !== null && !command.isNull()) {
            const abandoned = command;
            command = null;
            try { destroyUnsubmitted(abandoned); } catch (_) { /* one cleanup attempt only */ }
        }
        throw error;
    }
}
