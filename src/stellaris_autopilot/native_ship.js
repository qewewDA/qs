/* Pinned normal ship buildable. Composed with native_research.js by the worker. */
function canonicalShipRequest(request) {
    if (request.kind !== 'queue_ship') return null;
    const fields = 'country_id,design_id,kind,queue_id,starbase_id,station_id,token';
    if (Object.keys(request).sort().join(',') !== fields ||
        !['design_id','queue_id','starbase_id','station_id'].every(key => validId(request[key]))) {
        throw new Error('Malformed typed ship construction request');
    }
    return {kind: request.kind, country_id: '0', starbase_id: request.starbase_id,
        design_id: request.design_id, station_id: request.station_id, queue_id: request.queue_id, token: request.token};
}

function shipPoolObject(poolRva, handle, objectHandleOffset) {
    const pool = base.add(poolRva).readPointer();
    if (pool.isNull()) return null;
    const count = pool.add(0x20).readU32();
    const entries = pool.add(0x18).readPointer();
    const index = Number(handle) & 0xffffff;
    if (entries.isNull() || count > 1000000 || index >= count) return null;
    const object = entries.add(index * 16 + 8).readPointer();
    return !object.isNull() && object.add(objectHandleOffset).readU32() === Number(handle) ? object : null;
}

function shipyardOwner(starbaseId) {
    const reference = Memory.alloc(0x18);
    reference.writeByteArray(new Uint8Array(0x18));
    reference.writeU32(Number(starbaseId));
    reference.add(4).writeU8(2); // COrbitableRef::starbase; 3 is a megastructure.
    const output = Memory.alloc(0x20);
    output.writeByteArray(new Uint8Array(0x20));
    output.writeU32(4294967295);
    const owner = new NativeFunction(base.add(0xe2f4c0), 'pointer', ['pointer','pointer'], 'win64');
    owner(output, reference);
    return output.readU32();
}

function prepareShipCommand(request) {
    const design = shipPoolObject(0x3114670, request.design_id, 0x10);
    const starbase = shipPoolObject(0x3114c60, request.starbase_id, 8);
    if (design === null || starbase === null || shipyardOwner(request.starbase_id) !== 0 ||
        starbase.add(0x98).readU32() !== Number(request.station_id) ||
        starbase.add(0x884).readU32() !== Number(request.queue_id)) {
        throw new NativePreconditionError('Ship identity mismatch: ' + JSON.stringify({
            design_found: design !== null, starbase_found: starbase !== null,
            owner: starbase === null ? null : shipyardOwner(request.starbase_id),
            station: starbase === null ? null : starbase.add(0x98).readU32(),
            shipyard_queue: starbase === null ? null : starbase.add(0x884).readU32()
        }));
    }
    let command = null;
    let buildable = null;
    try {
        const factory = new NativeFunction(base.add(0x8cb860), 'pointer', [], 'win64');
        const selectDesign = new NativeFunction(base.add(0xcd32b0), 'void', ['pointer','uint32'], 'win64');
        buildable = factory();
        if (buildable.isNull()) throw new Error('Ship buildable allocation failed');
        // The factory initializes the full owned design snapshot. The normal
        // design setter resolves this exact handle and rebuilds stage-zero data.
        selectDesign(buildable.add(8), Number(request.design_id));
        if (buildable.add(0x538).readU32() !== Number(request.design_id) ||
            buildable.add(0x534).readS32() !== 0) throw new Error('Ship design/stage identity mismatch');
        buildable.add(0x540).writeU32(Number(request.starbase_id));
        buildable.add(0x544).writeU8(2); // COrbitableRef::starbase
        buildable.add(0x548).writeU8(0); // No external native override.
        command = createQueue();
        if (command.isNull()) throw new Error('Ship queue command allocation failed');
        command.add(0x28).writeU32(0);
        command.add(0x2c).writeU32(Number(request.queue_id));
        const nested = buildable;
        buildable = null; // Transfer is attempted once; never destroy aliases.
        command.add(0x20).writePointer(nested);
        return {command, validate: validateQueue, meta: {native_design_id: Number(request.design_id)}};
    } catch (error) {
        if (command !== null && !command.isNull()) {
            const abandoned = command; command = null;
            try { destroyUnsubmitted(abandoned); } catch (_) { /* no second cleanup attempt */ }
        }
        if (buildable !== null && !buildable.isNull()) {
            const abandoned = buildable; buildable = null;
            try { destroyUnsubmitted(abandoned); } catch (_) { /* no second cleanup attempt */ }
        }
        throw error;
    }
}
