/* Fixed typed planet commands for pinned Cygnus 4.5.2 (9776).
 * Concatenated after native_research.js: no hooks, RPC service, or input path.
 * The shared consumer serializes tokens and owns a successfully returned
 * command. Only its normal validator and dispatcher may admit that command.
 */
'use strict';

const planetCommandFields = {
    upgrade_building: ['zone_id', 'district_id', 'slot_index', 'source_zone',
        'building_id', 'source_building', 'target_building'],
    demolish_building: ['zone_id', 'district_id', 'slot_index', 'source_zone',
        'building_id', 'source_building'],
    convert_zone: ['zone_id', 'district_id', 'slot_index', 'source_zone', 'target_zone'],
    cancel_construction: ['item_id'],
};

function canonicalPlanetRequest(request) {
    if (request === null || typeof request !== 'object' || Array.isArray(request) ||
        !Object.hasOwn(planetCommandFields, request.kind)) return null;
    const fields = ['kind', 'country_id', 'token', 'planet_id', 'colony_id', 'queue_id',
        ...planetCommandFields[request.kind]];
    if (Object.keys(request).length !== fields.length || fields.some(key => !Object.hasOwn(request, key)) ||
        request.country_id !== '0' || typeof request.token !== 'string' || !/^[0-9a-f]{32}$/.test(request.token)) {
        throw new Error('Malformed typed planet command');
    }
    const canonical = {};
    for (const field of fields) {
        const value = request[field];
        if (field.endsWith('_id') && field !== 'country_id') {
            if (typeof value !== 'string' || !/^(0|[1-9][0-9]{0,9})$/.test(value) || Number(value) >= 4294967295) {
                throw new Error('Planet command requires canonical observed handles');
            }
        } else if (field === 'slot_index') {
            if (!Number.isInteger(value) || value < 0 || value >= 32) throw new Error('Invalid district zone slot');
        } else if (['source_building', 'target_building'].includes(field)) {
            if (typeof value !== 'string' || !/^building_[A-Za-z0-9_]{1,120}$/.test(value)) {
                throw new Error('Invalid building data key');
            }
        } else if (['source_zone', 'target_zone'].includes(field)) {
            if (typeof value !== 'string' || !/^zone_[A-Za-z0-9_]{1,120}$/.test(value)) {
                throw new Error('Invalid zone data key');
            }
        }
        canonical[field] = value;
    }
    if ((request.kind === 'upgrade_building' && request.source_building === request.target_building) ||
        (request.kind === 'convert_zone' && request.source_zone === request.target_zone)) {
        throw new Error('Planet command must change its selected type');
    }
    return canonical;
}

let planetNativeBindings = null;
function planetBindings() {
    if (planetNativeBindings === null) {
        const factory = rva => new NativeFunction(base.add(rva), 'pointer', [], 'win64');
        const validator = rva => new NativeFunction(base.add(rva), 'uint8', ['pointer', 'pointer'], 'win64');
        planetNativeBindings = {
            upgrade: factory(0x6baa80), destroy: factory(0x6b9820),
            zone: factory(0x6baf60), cancel: factory(0xacd820),
            validateDestroy: validator(0x6b6620), validateCancel: validator(0xacc060),
        };
    }
    return planetNativeBindings;
}

function planetPoolObject(globalRva, handleText, handleOffset, label) {
    const pool = base.add(globalRva).readPointer();
    if (pool.isNull()) throw new Error(label + ' pool unavailable');
    const count = pool.add(0x20).readU32();
    const entries = pool.add(0x18).readPointer();
    const handle = Number(handleText);
    const index = handle & 0xffffff;
    if (entries.isNull() || count > 1048576 || index >= count) throw new Error(label + ' handle unavailable');
    const value = entries.add(index * 16 + 8).readPointer();
    if (value.isNull() || value.add(handleOffset).readU32() !== handle) {
        throw new Error(label + ' full handle changed');
    }
    return value;
}

function planetHandleArray(object, arrayOffset, countOffset, limit, label, inactive = false) {
    const count = object.add(countOffset).readS32();
    const array = object.add(arrayOffset).readPointer();
    if (count < 0 || count > limit || (count !== 0 && array.isNull())) throw new Error(label + ' list unavailable');
    const handles = [];
    const seen = new Set();
    for (let index = 0; index < count; index++) {
        const handle = array.add(index * 4).readU32();
        if (handle === 4294967295 && inactive) {
            handles.push(handle); // Empty slots retain their physical array position.
            continue;
        }
        if (handle === 4294967295 || seen.has(handle)) throw new Error(label + ' list has ambiguous identity');
        seen.add(handle);
        handles.push(handle);
    }
    return handles;
}

function planetZoneType(key) {
    const database = base.add(0x3112910).readPointer();
    if (database.isNull()) throw new Error('Zone database unavailable');
    const count = database.add(0x5c).readU32();
    const values = database.add(0x50).readPointer();
    if (values.isNull() || count < 1 || count > 4096) throw new Error('Unrecognized zone database');
    let found = null;
    for (let index = 0; index < count; index++) {
        const value = values.add(index * 8).readPointer();
        if (value.isNull() || keyAt(value, 0x20, 0x30, 0x38) !== key) continue;
        if (found !== null) throw new Error('Duplicate zone type identity');
        found = value;
    }
    if (found === null) throw new Error('Selected zone type unavailable');
    return found;
}

function planetExactReferences(request, colony) {
    const districts = planetHandleArray(colony, 0x88, 0x94, 128, 'Colony district');
    if (!districts.includes(Number(request.district_id))) throw new Error('District no longer belongs to this colony');
    const district = planetPoolObject(0x3114cd8, request.district_id, 8, 'District');
    const zones = planetHandleArray(district, 0x30, 0x3c, 32, 'District zone', true);
    if (zones[request.slot_index] !== Number(request.zone_id)) throw new Error('Exact district slot changed');
    const zone = planetPoolObject(0x3114d18, request.zone_id, 8, 'Zone');
    // Native ZoneType::CanBuild at 0x480190..0x4801ae resolves this same
    // slot via 0x482700, then compares Zone+0x20 with the requested type.
    const sourceType = zone.add(0x20).readPointer();
    if (sourceType.isNull() || keyAt(sourceType, 0x20, 0x30, 0x38) !== request.source_zone) {
        throw new Error('Source zone type changed');
    }
    if (request.kind === 'convert_zone') return;
    // Zone serializer 0x78e230 receives the secondary interface at full+0x10.
    // Its buildings vector at interface+0x190 is full+0x1a0; the native U32
    // vector serializer 0x2a8890 reads pointer+8/count+0x14, stride 4.
    const buildings = planetHandleArray(zone, 0x1a8, 0x1b4, 256, 'Zone building');
    if (!buildings.includes(Number(request.building_id))) throw new Error('Building no longer belongs to this exact zone');
    const building = planetPoolObject(0x3114ce0, request.building_id, 8, 'Building');
    const type = building.add(0x20).readPointer();
    if (type.isNull() || keyAt(type, 0x20, 0x30, 0x38) !== request.source_building ||
        type.add(0x1940).readS32() !== 0) throw new Error('Source ordinary building type changed');
}

function planetAssertFactory(object, vtableRva, label) {
    if (object.isNull() || !object.readPointer().equals(base.add(vtableRva))) {
        throw new Error('Unexpected ' + label + ' factory layout');
    }
}

function preparePlanetCommand(input) {
    const request = canonicalPlanetRequest(input);
    if (request === null) throw new Error('Unsupported planet command');
    if (gameThread === null || Process.getCurrentThreadId() !== gameThread) {
        throw new Error('Planet commands require the verified game thread');
    }
    const match = colonyForQueue(request.queue_id, request.colony_id, request.planet_id);
    if (match === null || match.handle !== Number(request.colony_id)) {
        throw new Error('Exact owned colony and queue no longer match');
    }
    const colony = planetPoolObject(0x3114e28, request.colony_id, 0x10, 'Colony');
    planetPoolObject(0x3114e10, request.planet_id, 0x18, 'Physical planet');
    planetPoolObject(0x3114ba0, request.queue_id, 8, 'Construction queue');
    if (request.kind === 'cancel_construction') {
        const item = planetPoolObject(0x3114b90, request.item_id, 8, 'Construction item');
        if (item.add(0x20).readU32() !== Number(request.queue_id) || item.add(0x24).readU32() !== 0) {
            throw new Error('Exact paid item changed queue or payer');
        }
    } else {
        planetExactReferences(request, colony);
    }
    const bindings = planetBindings();
    let command = null;
    let orphan = null;
    try {
        let validator;
        if (request.kind === 'demolish_building') {
            command = bindings.destroy();
            planetAssertFactory(command, 0x23914e8, 'demolition command');
            command.add(0x20).writeU32(Number(request.colony_id));
            command.add(0x24).writeU32(Number(request.building_id));
            command.add(0x28).writeU32(0);
            validator = bindings.validateDestroy;
        } else if (request.kind === 'cancel_construction') {
            command = bindings.cancel();
            planetAssertFactory(command, 0x23c1ef8, 'queue cancellation command');
            command.add(0x20).writeU32(0);
            command.add(0x24).writeU32(Number(request.item_id));
            validator = bindings.validateCancel;
        } else {
            const conversion = request.kind === 'convert_zone';
            const target = conversion ? planetZoneType(request.target_zone) : buildingType(request.target_building);
            if (target === null || (!conversion && target.add(0x1940).readS32() !== 0)) {
                throw new Error('Selected ordinary building type unavailable');
            }
            orphan = conversion ? bindings.zone() : bindings.upgrade();
            planetAssertFactory(orphan, conversion ? 0x2392190 : 0x2391f80, 'planet buildable');
            orphan.add(8).writePointer(target);
            orphan.add(0x10).writeU32(Number(request.colony_id));
            orphan.add(0x14).writeU32(Number(conversion ? request.district_id : request.zone_id));
            if (conversion) orphan.add(0x18).writeS32(request.slot_index);
            else orphan.add(0x18).writeU32(Number(request.building_id));
            command = createQueue();
            planetAssertFactory(command, 0x23c1b60, 'queue command');
            if (!command.add(0x20).readPointer().isNull()) throw new Error('Queue factory has an unexpected owned payload');
            command.add(0x28).writeU32(0);
            command.add(0x2c).writeU32(Number(request.queue_id));
            const nested = orphan;
            orphan = null; // Ownership transfer is attempted once; ambiguous writes are never retried.
            command.add(0x20).writePointer(nested);
            validator = validateQueue;
        }
        return {command, validate: validator, meta: {normal_command: request.kind,
            planet_id: request.planet_id, colony_id: request.colony_id, queue_id: request.queue_id}};
    } catch (error) {
        // A failed prepare never exposes its pointers to the shared consumer.
        // Clear each local owner before invoking its one cleanup attempt.
        let cleanupFailed = false;
        const owners = [command, orphan];
        command = null;
        orphan = null;
        for (const owned of owners) {
            if (owned === null || owned.isNull()) continue;
            try { destroyUnsubmitted(owned); } catch (_) { cleanupFailed = true; }
        }
        if (cleanupFailed) throw new Error('Planet command preparation or native cleanup was unresolved');
        throw error;
    }
}
