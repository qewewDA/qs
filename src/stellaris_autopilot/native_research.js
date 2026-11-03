/* Exact-build research command experiment. Host pins the EXE hash, a verified
 * process, campaign/session and fresh visible alternatives. This fixed script
 * accepts observed candidate IDs; submitted never means gameplay verified.
 */
'use strict';
const base = Process.getModuleByName('stellaris.exe').base;
const create = new NativeFunction(base.add(0x6d1b90), 'pointer', [], 'win64');
const validate = new NativeFunction(base.add(0x6bb4f0), 'uint8', ['pointer', 'pointer'], 'win64');
const createDistrict = new NativeFunction(base.add(0x6bacf0), 'pointer', [], 'win64');
const createBuilding = new NativeFunction(base.add(0x6bb1d0), 'pointer', [], 'win64');
const createQueue = new NativeFunction(base.add(0xacd7d0), 'pointer', [], 'win64');
const validateQueue = new NativeFunction(base.add(0xacb850), 'uint8', ['pointer', 'pointer'], 'win64');
const dumpState = new NativeFunction(base.add(0xebc230), 'pointer', ['pointer', 'pointer'], 'win64');
const cleanResult = new NativeFunction(base.add(0x160ff0), 'void', ['pointer'], 'win64');
const createPause = new NativeFunction(base.add(0x961930), 'pointer', [], 'win64');
const validatePause = new NativeFunction(base.add(0x1ae1f0), 'uint8', ['pointer','pointer'], 'win64');
const runInbox = new NativeFunction(base.add(0xeba020), 'pointer', ['pointer','pointer'], 'win64');
const assignString = new NativeFunction(base.add(0x15bcf0), 'pointer', ['pointer','pointer','uint64'], 'win64');
const cleanString = new NativeFunction(base.add(0x15be90), 'void', ['pointer'], 'win64');
const dispatch = new NativeFunction(base.add(0x5f88a0), 'void', ['pointer', 'uint8'], 'win64');
const states = new Map();
const requests = new Map();
let pending = null;
let inFlight = false;
let gameThread = null;
let wakeTransport = null;

// Only explicit checks made before allocation use this class. A native fault,
// failed destructor or possibly transferred command remains ambiguous.
class NativePreconditionError extends Error {}

function stateFor(token) {
    return {...(requests.get(token) || {}), ...(states.get(token) || {status: 'unknown'}), token};
}

function keyAt(address, bufferOffset, lengthOffset, capacityOffset) {
    const length = address.add(lengthOffset).readU64().toNumber();
    const capacity = address.add(capacityOffset).readU64().toNumber();
    if (!Number.isSafeInteger(length) || !Number.isSafeInteger(capacity) ||
        length < 1 || length > 128 || capacity < length || capacity > 1048576) return null;
    const buffer = capacity >= 16 ? address.add(bufferOffset).readPointer() : address.add(bufferOffset);
    if (buffer.isNull()) return null;
    return buffer.readUtf8String(length);
}

function technology(key) {
    if (typeof key !== 'string' || !/^tech_[A-Za-z0-9_]{1,120}$/.test(key)) {
        throw new Error('Malformed technology key');
    }
    const database = base.add(0x31146e0).readPointer();
    if (database.isNull()) return null;
    const buckets = database.add(0x68).readPointer();
    const mask = database.add(0x74).readU32();
    const tail = database.add(0x78).readU8();
    if (buckets.isNull() || mask > 16383 || tail > 16) throw new Error('Unrecognized technology DB layout');
    let found = null;
    for (let index = 0; index < mask + 1 + tail; index++) {
        const bucket = buckets.add(index * 0x40);
        const control = bucket.add(4).readU8();
        if (control === 0 || control === 255) continue;
        const bucketKey = keyAt(bucket, 0x18, 0x28, 0x30);
        if (bucketKey !== key) continue;
        const value = bucket.add(0x38).readPointer();
        if (value.isNull() || keyAt(value, 0x20, 0x30, 0x38) !== key) {
            throw new Error('Technology bucket/object identity mismatch');
        }
        if (found !== null) throw new Error('Duplicate technology identity');
        found = value;
    }
    return found;
}

function destroyUnsubmitted(command) {
    const address = command.readPointer().readPointer();
    const destroy = new NativeFunction(address, 'void', ['pointer', 'uint32'], 'win64');
    destroy(command, 1);
}

function districtType(key) {
    if (typeof key !== 'string' || !/^district_[A-Za-z0-9_]{1,120}$/.test(key)) throw new Error('Invalid district key');
    const database = base.add(0x3112900).readPointer();
    if (database.isNull()) return null;
    const values = database.add(0x50).readPointer();
    const count = database.add(0x5c).readU32();
    if (values.isNull() || count < 1 || count > 2048) throw new Error('Unrecognized district DB layout');
    let found = null;
    for (let index = 0; index < count; index++) {
        const value = values.add(index * 8).readPointer();
        if (value.isNull() || keyAt(value, 0x20, 0x30, 0x38) !== key) continue;
        if (found !== null) throw new Error('Duplicate district identity');
        found = value;
    }
    return found;
}

function buildingType(key) {
    if (typeof key !== 'string' || !/^building_[A-Za-z0-9_]{1,120}$/.test(key)) throw new Error('Invalid building key');
    const database = base.add(0x31128e0).readPointer();
    if (database.isNull()) return null;
    const values = database.add(0x50).readPointer();
    const count = database.add(0x5c).readU32();
    if (values.isNull() || count < 1 || count > 16384) throw new Error('Unrecognized building DB layout');
    let found = null;
    for (let index = 0; index < count; index++) {
        const value = values.add(index * 8).readPointer();
        if (value.isNull() || keyAt(value, 0x20, 0x30, 0x38) !== key) continue;
        if (found !== null) throw new Error('Duplicate building identity');
        found = value;
    }
    return found;
}

function validId(value) {
    return typeof value === 'string' && /^(0|[1-9][0-9]{0,9})$/.test(value) && Number(value) < 4294967295;
}

function colonyForQueue(queueId, colonyId, physicalId = null) {
    const pool = base.add(0x3114e28).readPointer();
    if (pool.isNull()) return null;
    const count = pool.add(0x20).readU32();
    const entries = pool.add(0x18).readPointer();
    if (entries.isNull() || count > 100000) throw new Error('Unrecognized colony pool');
    const resolveOwner = new NativeFunction(base.add(0xccd000), 'pointer', ['uint64'], 'win64');
    let found = null;
    for (let index = 0; index < count; index++) {
        const planet = entries.add(index * 16 + 8).readPointer();
        if (planet.isNull()) continue;
        const handle = planet.add(0x10).readU32();
        if ((handle & 0xffffff) !== index) continue;
        if (handle !== Number(colonyId)) continue;
        if (physicalId !== null && (planet.add(0xf7c).readS8() !== 0 ||
            planet.add(0xf78).readU32() !== Number(physicalId))) continue;
        const owner = resolveOwner(planet.add(0xf78).readU64());
        if (owner.isNull() || owner.add(0xc4).readU32() !== Number(queueId)) continue;
        const output = Memory.alloc(0x40);
        output.writeByteArray(new Uint8Array(0x40));
        const getOwner = new NativeFunction(owner.readPointer().add(0x58).readPointer(), 'pointer', ['pointer','pointer'], 'win64');
        getOwner(owner, output);
        if (output.readU32() !== 0) continue;
        if (found !== null) throw new Error('Queue has multiple native planet identities');
        found = {handle};
    }
    return found;
}

function validateBuilding(command) {
    const reason = Memory.alloc(0x30);
    reason.writeByteArray(new Uint8Array(0x30));
    reason.add(0x28).writeU64(15);
    const allowed = validateQueue(command, reason) !== 0;
    const length = reason.add(0x20).readU64().toNumber();
    const capacity = reason.add(0x28).readU64().toNumber();
    let message = 'Native validation rejected';
    if (Number.isSafeInteger(length) && Number.isSafeInteger(capacity) && length > 0 && length <= 8192 && capacity >= length) {
        const buffer = capacity >= 16 ? reason.add(0x10).readPointer() : reason.add(0x10);
        if (!buffer.isNull()) message = buffer.readUtf8String(length);
    }
    cleanString(reason);
    return {allowed, reason: message};
}

function pauseState() {
    const state = base.add(0x3114e68).readPointer();
    if (state.isNull()) return {paused: null, protected_pause: null};
    const paused = state.add(0x594).readU8();
    const protectedPause = state.add(0x595).readU8();
    return {paused: paused === 0 || paused === 1 ? paused === 1 : null,
        protected_pause: protectedPause === 0 || protectedPause === 1 ? protectedPause === 1 : null};
}

function fixedDump() {
    const state = base.add(0x3114e68).readPointer();
    if (state.isNull() || state.add(0xf90).readPointer().isNull()) {
        throw new Error('Game/save manager is unavailable');
    }
    const text = Memory.alloc(0x30);
    text.writeByteArray(new Uint8Array(0x30));
    text.add(0x10).writeUtf8String('qs_direct');
    text.add(0x20).writeU64(9);
    text.add(0x28).writeU64(15);
    const args = Memory.alloc(0x18);
    args.writeByteArray(new Uint8Array(0x18));
    args.add(8).writePointer(text);
    args.add(0x10).writeS32(1);
    args.add(0x14).writeS32(1);
    const result = Memory.alloc(0x38);
    result.writeByteArray(new Uint8Array(0x38));
    dumpState(result, args);
    cleanResult(result);
}

function fixedInbox() {
    const state = base.add(0x3114e68).readPointer();
    if (state.isNull() || state.add(0xf90).readPointer().isNull()) {
        throw new Error('Game/save manager is unavailable');
    }
    const text = Memory.alloc(0x30);
    text.writeByteArray(new Uint8Array(0x30));
    text.add(0x28).writeU64(15);
    assignString(text, Memory.allocUtf8String('qs_direct_inbox.txt'), 19);
    const args = Memory.alloc(0x18);
    args.writeByteArray(new Uint8Array(0x18));
    args.add(8).writePointer(text);
    args.add(0x10).writeS32(1);
    args.add(0x14).writeS32(1);
    const result = Memory.alloc(0x38);
    result.writeByteArray(new Uint8Array(0x38));
    runInbox(result, args);
    // Each native-owned buffer has one cleanup attempt. An exception remains
    // ambiguous; never retry a possibly completed destructor.
    try { cleanResult(result); }
    finally { cleanString(text); }
}

function loadAttachedEvents(token) {
    if (pauseState().paused !== true) throw new NativePreconditionError('Pause before loading the attached bridge');
    const manager = base.add(0x3152ca8).readPointer();
    if (manager.isNull()) throw new NativePreconditionError('Event manager unavailable');
    const filename = 'qs_attach_events_' + token + '.txt';
    const text = Memory.alloc(0x30);
    text.writeByteArray(new Uint8Array(0x30));
    text.add(0x28).writeU64(15);
    assignString(text, Memory.allocUtf8String(filename), filename.length);
    try {
        const loadFile = new NativeFunction(base.add(0x3f3c50), 'void', ['pointer','pointer','uint8'], 'win64');
        loadFile(manager, text, 0);
    } finally { cleanString(text); }
}

function wakeGame(token) {
    if (gameThread === null) return false; // The load bootstrap will consume it.
    try {
        if (wakeTransport === null) {
            const user32 = Process.getModuleByName('user32.dll');
            if (typeof user32.getExportByName !== 'function') return false;
            const windowThread = new NativeFunction(user32.getExportByName('GetWindowThreadProcessId'), 'uint32', ['pointer','pointer'], 'win64');
            const windowText = new NativeFunction(user32.getExportByName('GetWindowTextW'), 'int32', ['pointer','pointer','int32'], 'win64');
            const enumerate = new NativeFunction(user32.getExportByName('EnumWindows'), 'int32', ['pointer','pointer'], 'win64');
            const windowLong = new NativeFunction(user32.getExportByName('GetWindowLongPtrW'), 'pointer', ['pointer','int32'], 'win64');
            const register = new NativeFunction(user32.getExportByName('RegisterWindowMessageW'), 'uint32', ['pointer'], 'win64');
            const post = new NativeFunction(user32.getExportByName('PostMessageW'), 'int32', ['pointer','uint32','pointer','pointer'], 'win64');
            const candidates = [];
            const enumerateCallback = new NativeCallback((window, _) => {
                const owner = Memory.alloc(4);
                const thread = windowThread(window, owner);
                if (owner.readU32() === Process.id && thread === gameThread) {
                    const title = Memory.alloc(1024);
                    title.writeUtf16String('');
                    const length = windowText(window, title, 512);
                    candidates.push({window, title: length > 0 ? title.readUtf16String(length) : ''});
                }
                return 1;
            }, 'int32', ['pointer','pointer'], 'win64');
            enumerate(enumerateCallback, ptr(0));
            const named = candidates.filter(value => value.title.includes('Stellaris'));
            const selected = candidates.length === 1 ? candidates[0] : named.length === 1 ? named[0] : null;
            if (selected === null) return false;
            const procedure = windowLong(selected.window, -4);
            const range = Process.findRangeByAddress(procedure);
            if (range === null || !range.protection.includes('x')) return false;
            const message = register(Memory.allocUtf16String('QS.Native.' + Process.id + '.' + token));
            if (message < 0xc000 || message > 0xffff) return false;
            const listener = Interceptor.attach(procedure, {
                onEnter(args) {
                    this.isWake = args[0].equals(selected.window) && args[1].toUInt32() === message;
                },
                onLeave() {
                    if (this.isWake && Process.getCurrentThreadId() === gameThread) consumePending('window_message');
                }
            });
            wakeTransport = {post, window: selected.window, message, listener, windowThread, windowLong, procedure};
        }
        const owner = Memory.alloc(4);
        const thread = wakeTransport.windowThread(wakeTransport.window, owner);
        if (owner.readU32() !== Process.id || thread !== gameThread ||
            !wakeTransport.windowLong(wakeTransport.window, -4).equals(wakeTransport.procedure)) {
            wakeTransport.listener.detach();
            wakeTransport = null;
            return false;
        }
        return wakeTransport.post(wakeTransport.window, wakeTransport.message, ptr(0), ptr(0)) !== 0;
    } catch (_) {
        return false; // Daily run remains available; no keyboard/mouse fallback.
    }
}

function consumePending(source = 'run') {
        if (gameThread === null || Process.getCurrentThreadId() !== gameThread) return;
        if (pending === null || inFlight) return;
        const request = pending;
        pending = null;
        inFlight = true;
        states.set(request.token, {status: 'processing', consumed: true});
        let command = null;
        let orphan = null;
        let transferred = false;
        try {
            if (request.kind === 'set_paused' && source !== 'window_message') {
                states.set(request.token, {status: 'blocked', reason: 'Pause requires the native window wake transport',
                    consumed: true, submitted: false});
                return;
            }
            if (request.kind === 'request_observation') {
                fixedDump();
                states.set(request.token, {status: 'dump_requested', consumed: true,
                    game_thread_id: Process.getCurrentThreadId(), execution_source: source, runtime: pauseState()});
                return;
            }
            if (request.kind === 'run_inbox') {
                fixedInbox();
                states.set(request.token, {status: 'inbox_executed', consumed: true,
                    game_thread_id: Process.getCurrentThreadId(), execution_source: source, runtime: pauseState()});
                return;
            }
            if (request.kind === 'load_attached_events') {
                loadAttachedEvents(request.token);
                states.set(request.token, {status: 'events_load_returned', consumed: true,
                    game_thread_id: Process.getCurrentThreadId(), execution_source: source, runtime: pauseState()});
                return;
            }
            if (!['select_research', 'set_paused', 'queue_building', 'queue_district'].includes(request.kind)) {
                if (pauseState().paused !== true) {
                    throw new NativePreconditionError('The live game must still be paused before this command');
                }
                const prepared = request.kind === 'queue_ship' ? prepareShipCommand(request)
                    : request.kind === 'queue_colony_ship' ? prepareColonyShipCommand(request)
                    : ['upgrade_building', 'demolish_building', 'convert_zone', 'cancel_construction'].includes(request.kind)
                    ? preparePlanetCommand(request) : prepareFleetCommand(request);
                command = prepared.command;
                if (command === null || command.isNull()) throw new Error('Native adapter returned no command');
                const validation = prepared.validation || {allowed: prepared.validate(command, ptr(0)) !== 0,
                    reason: 'Normal native command validation rejected'};
                if (!validation.allowed) {
                    const rejected = command; command = null;
                    destroyUnsubmitted(rejected);
                    states.set(request.token, {status: 'blocked', reason: validation.reason, consumed: true, submitted: false});
                    return;
                }
                transferred = true;
                dispatch(command, 0);
                command = null;
                states.set(request.token, {status: 'submitted', consumed: true, ...(prepared.meta || {}),
                    game_thread_id: Process.getCurrentThreadId(), execution_source: source});
                return;
            }
            const research = request.kind === 'select_research';
            const pause = request.kind === 'set_paused';
            const building = request.kind === 'queue_building';
            const nativeColony = research || pause ? null : colonyForQueue(request.queue_id, request.colony_id, request.planet_id);
            if (!research && !pause && nativeColony === null) {
                states.set(request.token, {status: 'blocked', reason: 'No unique owned native colony matches the observed queue', consumed: true, submitted: false});
                return;
            }
            const target = pause ? null : research ? technology(request.technology)
                : building ? buildingType(request.building) : districtType(request.district);
            if (!pause && target === null) {
                states.set(request.token, {status: 'blocked', reason: 'Database object unavailable', consumed: true, submitted: false});
                return;
            }
            if (pause) {
                command = createPause();
                if (command.isNull()) throw new Error('Pause command allocation failed');
                command.add(0x50).writeU8(request.paused ? 1 : 0);
                // Factory leaves the native empty source and mode 2 intact.
                // The engine's protected-pause rules are never overridden.
            } else if (research) {
                command = create();
                if (command.isNull()) throw new Error('Command allocation failed');
                command.add(0x20).writeU32(0);
                command.add(0x28).writePointer(target);
            } else {
                orphan = building ? createBuilding() : createDistrict();
                if (orphan.isNull()) throw new Error('Buildable allocation failed');
                orphan.add(8).writePointer(target);
                orphan.add(0x10).writeU32(nativeColony.handle);
                if (building) orphan.add(0x14).writeU32(Number(request.zone_id));
                command = createQueue();
                if (command.isNull()) throw new Error('Queue command allocation failed');
                command.add(0x20).writePointer(orphan);
                orphan = null; // The native command now owns the buildable.
                command.add(0x28).writeU32(0);
                command.add(0x2c).writeU32(Number(request.queue_id));
            }
            const validation = building ? validateBuilding(command)
                : {allowed: (pause ? validatePause : research ? validate : validateQueue)(command, ptr(0)) !== 0,
                    reason: 'Native validation rejected'};
            if (!validation.allowed) {
                const rejected = command;
                command = null;
                destroyUnsubmitted(rejected);
                states.set(request.token, {status: 'blocked', reason: validation.reason, consumed: true, submitted: false});
                return;
            }
            // The dispatcher owns the object even if submission returns an
            // uncertain result. Never read, free or retry it after this call.
            transferred = true;
            dispatch(command, 0);
            command = null;
            states.set(request.token, {status: 'submitted', consumed: true,
                ...(!research && !pause ? {native_colony_handle: nativeColony.handle} : {}),
                game_thread_id: Process.getCurrentThreadId(), execution_source: source});
        } catch (error) {
            if (!transferred && command !== null && !command.isNull()) {
                const abandoned = command;
                command = null;
                // If a field write faulted at ownership transfer, only the
                // command may clean up the nested object; never risk freeing
                // an aliased buildable twice. Ambiguous failures close the run.
                orphan = null;
                try { destroyUnsubmitted(abandoned); } catch (_) { /* retain ambiguous */ }
            } else if (orphan !== null && !orphan.isNull()) {
                const abandoned = orphan;
                orphan = null;
                try { destroyUnsubmitted(abandoned); } catch (_) { /* retain ambiguous */ }
            }
            states.set(request.token, error instanceof NativePreconditionError && !transferred
                ? {status: 'blocked', consumed: true, submitted: false, reason: String(error)}
                : {status: 'ambiguous', consumed: true, reason: String(error)});
        } finally {
            inFlight = false;
        }
}

Interceptor.attach(base.add(0xeba020), {
    onLeave() {
        const current = Process.getCurrentThreadId();
        if (gameThread === null) gameThread = current;
        if (current !== gameThread) return;
        consumePending();
    }
});

rpc.exports = {
    bindwindow(token) {
        if (typeof token !== 'string' || !/^[0-9a-f]{32}$/.test(token)) throw new Error('Invalid binding token');
        if (pending !== null || inFlight || wakeTransport !== null || gameThread !== null) {
            throw new Error('The native bridge is already bound or busy');
        }
        const user32 = Process.getModuleByName('user32.dll');
        const enumerate = new NativeFunction(user32.getExportByName('EnumWindows'), 'int32', ['pointer','pointer'], 'win64');
        const threadOf = new NativeFunction(user32.getExportByName('GetWindowThreadProcessId'), 'uint32', ['pointer','pointer'], 'win64');
        const textOf = new NativeFunction(user32.getExportByName('GetWindowTextW'), 'int32', ['pointer','pointer','int32'], 'win64');
        const candidates = [];
        const callback = new NativeCallback((window, _) => {
            const owner = Memory.alloc(4);
            const thread = threadOf(window, owner);
            if (owner.readU32() === Process.id) {
                const title = Memory.alloc(1024);
                const length = textOf(window, title, 512);
                if (length > 0 && title.readUtf16String(length).includes('Stellaris')) candidates.push({window, thread});
            }
            return 1;
        }, 'int32', ['pointer','pointer'], 'win64');
        enumerate(callback, ptr(0));
        if (candidates.length !== 1) throw new Error('No unique existing Stellaris window');
        const state = base.add(0x3114e68).readPointer();
        if (state.isNull() || state.add(0xf90).readPointer().isNull()) throw new Error('Load a game before connecting');
        gameThread = candidates[0].thread;
        return {token, status: 'window_bound', game_thread_id: gameThread, runtime: pauseState()};
    },
    lookup(key) {
        const value = technology(key);
        return {found: value !== null, technology: key};
    },
    enqueue(request) {
        if (request === null || typeof request !== 'object' || Array.isArray(request) ||
            typeof request.token !== 'string' || !/^[0-9a-f]{32}$/.test(request.token) ||
            request.country_id !== '0') {
            throw new Error('Malformed or unauthorized native request');
        }
        let canonical;
        if (['request_observation','run_inbox','load_attached_events'].includes(request.kind) &&
            Object.keys(request).sort().join(',') === 'country_id,kind,token') {
            canonical = {kind: request.kind, country_id: '0', token: request.token};
        } else if (request.kind === 'set_paused' &&
            Object.keys(request).sort().join(',') === 'country_id,kind,paused,token' && typeof request.paused === 'boolean') {
            canonical = {kind: request.kind, country_id: '0', paused: request.paused, token: request.token};
        } else if (request.kind === 'select_research' &&
            Object.keys(request).sort().join(',') === 'area,country_id,kind,technology,token' &&
            ['physics','society','engineering'].includes(request.area) && typeof request.technology === 'string' &&
            /^tech_[A-Za-z0-9_]{1,120}$/.test(request.technology)) {
            canonical = {kind: request.kind, country_id: '0', area: request.area,
                technology: request.technology, token: request.token};
        } else if (request.kind === 'queue_district' &&
            Object.keys(request).sort().join(',') === 'colony_id,country_id,district,kind,planet_id,queue_id,token' &&
            validId(request.planet_id) && validId(request.queue_id) && validId(request.colony_id) && typeof request.district === 'string' &&
            /^district_[A-Za-z0-9_]{1,120}$/.test(request.district)) {
            canonical = {kind: request.kind, country_id: '0', planet_id: request.planet_id,
                district: request.district, queue_id: request.queue_id, colony_id: request.colony_id, token: request.token};
        } else if (request.kind === 'queue_building' &&
            Object.keys(request).sort().join(',') === 'building,colony_id,country_id,kind,planet_id,queue_id,token,zone_id' &&
            validId(request.planet_id) && validId(request.queue_id) && validId(request.zone_id) && validId(request.colony_id) &&
            typeof request.building === 'string' && /^building_[A-Za-z0-9_]{1,120}$/.test(request.building)) {
            canonical = {kind: request.kind, country_id: '0', planet_id: request.planet_id,
                zone_id: request.zone_id, building: request.building, queue_id: request.queue_id,
                colony_id: request.colony_id, token: request.token};
        } else {
            canonical = canonicalShipRequest(request) || canonicalColonyShipRequest(request)
                || canonicalPlanetRequest(request) || canonicalFleetRequest(request);
            if (canonical === null) throw new Error('Unknown native kind or malformed typed payload');
        }
        if (states.has(request.token)) {
            if (JSON.stringify(requests.get(request.token)) !== JSON.stringify(canonical)) {
                throw new Error('Operation token is bound to a different request');
            }
            return stateFor(request.token);
        }
        if (pending !== null || inFlight) throw new Error('A native command is already pending');
        requests.set(request.token, canonical);
        states.set(request.token, {status: 'pending', consumed: false});
        pending = {...request};
        const wakePosted = wakeGame(request.token);
        states.set(request.token, {...states.get(request.token), wake_posted: wakePosted});
        return stateFor(request.token);
    },
    result(token) {
        if (typeof token !== 'string' || !/^[0-9a-f]{32}$/.test(token)) throw new Error('Invalid token');
        return stateFor(token);
    },
    cancel(token) {
        if (typeof token !== 'string' || !/^[0-9a-f]{32}$/.test(token)) throw new Error('Invalid token');
        if (pending !== null && pending.token === token) {
            pending = null;
            states.set(token, {status: 'cancelled', consumed: false});
        }
        return stateFor(token);
    }
};
