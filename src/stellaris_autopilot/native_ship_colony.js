/* Fixed colony ship commands for pinned Cygnus v4.5.2 (9776).
 * Compose after native_ship.js; the Python host binds an observed owned species
 * and exact shipyard. Static ABI alone is not a gameplay success claim.
 * The shared consumer must call validateQueue, then its normal dispatcher;
 * only fresh paid buildable_colony_ship + colonization_data postconditions
 * can establish gameplay acceptance.
 */
'use strict';

function canonicalColonyShipRequest(request) {
    if (request === null || typeof request !== 'object' || Array.isArray(request) ||
        request.kind !== 'queue_colony_ship') return null;
    const keys = 'country_id,design_id,kind,queue_id,species_id,starbase_id,station_id,token';
    if (Object.keys(request).sort().join(',') !== keys || request.country_id !== '0' ||
        typeof request.token !== 'string' || !/^[0-9a-f]{32}$/.test(request.token) ||
        !['design_id', 'queue_id', 'species_id', 'starbase_id', 'station_id'].every(key => validId(request[key]))) {
        throw new Error('Malformed typed colony ship request');
    }
    return {kind: request.kind, country_id: '0', starbase_id: request.starbase_id,
        design_id: request.design_id, station_id: request.station_id,
        queue_id: request.queue_id, species_id: request.species_id, token: request.token};
}

function prepareColonyShipCommand(input) {
    const request = canonicalColonyShipRequest(input);
    if (request === null || gameThread === null || Process.getCurrentThreadId() !== gameThread) {
        throw new Error('A typed colony ship request on the verified game thread is required');
    }
    const design = shipPoolObject(0x3114670, request.design_id, 0x10);
    const starbase = shipPoolObject(0x3114c60, request.starbase_id, 8);
    const species = shipPoolObject(0x3114c40, request.species_id, 0x10);
    if (design === null || starbase === null || species === null ||
        shipyardOwner(request.starbase_id) !== 0 ||
        starbase.add(0x98).readU32() !== Number(request.station_id) ||
        starbase.add(0x884).readU32() !== Number(request.queue_id)) {
        throw new Error('The exact design, species or owned shipyard identity changed');
    }
    const stageCount = design.add(0x2c).readS32();
    const stages = design.add(0x20).readPointer();
    if (stageCount < 1 || stageCount > 128 || stages.isNull()) {
        throw new Error('The selected design has no bounded stage-zero ship size');
    }
    const shipSize = stages.add(8).readPointer();
    if (shipSize.isNull() || keyAt(shipSize, 0x20, 0x30, 0x38) !== 'colonizer') {
        throw new Error('A colony ship requires an exact colonizer design');
    }

    let command = null;
    let buildable = null;
    try {
        const factory = new NativeFunction(base.add(0xb0e680), 'pointer', [], 'win64');
        const selectDesign = new NativeFunction(base.add(0xcd32b0), 'void', ['pointer', 'uint32'], 'win64');
        buildable = factory();
        if (buildable.isNull() || !buildable.readPointer().equals(base.add(0x23c47e0)) ||
            !buildable.add(0x560).readPointer().equals(base.add(0x23374d0))) {
            throw new Error('Unexpected colony ship factory or colonization-data layout');
        }
        // Keep the factory's default designation pointer +0x568
        // and automation flag +0x574. Ordinary UI construction also uses the
        // same default pointer, an exact species handle, and automation 0.
        if (buildable.add(0x574).readU8() !== 0) {
            throw new Error('Unexpected colony ship automation mode');
        }
        selectDesign(buildable.add(8), Number(request.design_id));
        if (buildable.add(0x538).readU32() !== Number(request.design_id) ||
            buildable.add(0x534).readS32() !== 0) {
            throw new Error('Colony ship design or stage identity mismatch');
        }
        buildable.add(0x540).writeU32(Number(request.starbase_id));
        buildable.add(0x544).writeU8(2); // COrbitableRef::starbase
        buildable.add(0x548).writeU8(0); // No external override.
        buildable.add(0x570).writeU32(Number(request.species_id));

        command = createQueue();
        if (command.isNull() || !command.readPointer().equals(base.add(0x23c1b60)) ||
            !command.add(0x20).readPointer().isNull()) {
            throw new Error('Unexpected normal queue command factory layout');
        }
        command.add(0x28).writeU32(0);
        command.add(0x2c).writeU32(Number(request.queue_id));
        const transferred = buildable;
        buildable = null; // One-way ownership attempt; never destroy aliases.
        command.add(0x20).writePointer(transferred);
        return {command, validate: validateQueue, meta: {
            normal_command: 'queue_colony_ship', native_design_id: Number(request.design_id),
            native_species_id: Number(request.species_id), buildable_type: 'buildable_colony_ship',
        }};
    } catch (error) {
        const owners = [command, buildable];
        command = null;
        buildable = null;
        let cleanupFailed = false;
        for (const owned of owners) {
            if (owned === null || owned.isNull()) continue;
            try { destroyUnsubmitted(owned); } catch (_) { cleanupFailed = true; }
        }
        if (cleanupFailed) throw new Error('Colony ship preparation or cleanup was unresolved');
        throw error;
    }
}
