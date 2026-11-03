"""Worker/JS failure injection without Frida, attachment or a game process."""

import io
import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from stellaris_autopilot import native


TOKEN = "a" * 32
OTHER_TOKEN = "b" * 32
NODE = shutil.which("node")


class ImmediateQueue(queue.Queue):
    """Exercise the real timeout handler without sleeping for 25 seconds."""

    def __init__(self):
        super().__init__()
        self.read_timeouts = []

    def get(self, block=True, timeout=None):
        self.read_timeouts.append(timeout)
        return super().get(block=False)


class MemoryStdin:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.failure = None

    def write(self, value):
        if self.failure is not None:
            raise self.failure
        if self.closed:
            raise AssertionError("The closed worker pipe was written again")
        self.writes.append(value)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeWorker:
    def __init__(self):
        self.stdin = MemoryStdin()
        self.returncode = None
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waits.append(timeout)
        self.returncode = 0
        return 0

    def terminate(self):
        raise AssertionError("This simulated worker should exit on stdin close")


@pytest.fixture
def bridge():
    # Deliberately bypass __init__: it is the only method that hashes/attaches
    # an executable and starts a real process. All transport state is local.
    result = object.__new__(native.NativeResearchBridge)
    result.game = SimpleNamespace(poll=lambda: None)
    result.process = FakeWorker()
    result.lock = threading.Lock()
    result.request_id = 0
    result.poisoned = False
    result.wakeup_confirmed = False
    result.messages = ImmediateQueue()
    result.error_log = io.StringIO()
    return result


def reply(request_id=1, token=TOKEN, **fields):
    return json.dumps({"request_id": request_id, "result": {"token": token, "status": "submitted", **fields}})


def assert_poisoned_without_more_writes(bridge):
    assert bridge.poisoned is True
    assert bridge.process.stdin.closed
    assert bridge.process.waits == [5]
    assert bridge.error_log.closed
    writes = list(bridge.process.stdin.writes)
    reads = list(bridge.messages.read_timeouts)
    # Even a worker that appears alive again, with a plausible late response,
    # cannot restore a transport that has already lost correlation.
    bridge.process.returncode = None
    bridge.messages.put(reply(bridge.request_id, TOKEN))
    with pytest.raises(ValueError):
        bridge.result(TOKEN)
    assert bridge.process.stdin.writes == writes
    assert bridge.messages.read_timeouts == reads
    assert not bridge.messages.empty()


def test_native_rpc_correlates_monotonic_ids_and_full_enqueue_payload(bridge):
    selection = native.research_selection("physics", "tech_shields_2")
    bridge.messages.put(reply(1, status="pending"))
    assert bridge.submit(selection, TOKEN)["status"] == "pending"
    bridge.messages.put(reply(2, technology=selection.technology))
    assert bridge.result(TOKEN)["technology"] == selection.technology
    sent = [json.loads(line) for line in bridge.process.stdin.writes]
    assert sent == [
        {"request_id": 1, "method": "enqueue", "arguments": [{
            "kind": "select_research", "country_id": "0", "area": "physics",
            "technology": "tech_shields_2", "token": TOKEN,
        }]},
        {"request_id": 2, "method": "result", "arguments": [TOKEN]},
    ]
    assert bridge.poisoned is False and not bridge.process.stdin.closed
    assert bridge.messages.read_timeouts == [25, 25]


@pytest.mark.parametrize("request_id", [None, True, 1.0, "1", 0, 2])
def test_wrong_or_noninteger_reply_id_poison_transport(bridge, request_id):
    bridge.messages.put(reply(request_id))
    with pytest.raises(ValueError, match="correlation"):
        bridge.result(TOKEN)
    assert_poisoned_without_more_writes(bridge)


@pytest.mark.parametrize("result", [None, [], {}, {"status": "submitted"}, {"token": OTHER_TOKEN}])
def test_missing_or_wrong_token_and_bad_result_shape_poison_transport(bridge, result):
    bridge.messages.put(json.dumps({"request_id": 1, "result": result}))
    with pytest.raises(ValueError, match="correlation"):
        bridge.result(TOKEN)
    assert_poisoned_without_more_writes(bridge)


@pytest.mark.parametrize("message", ["not JSON", "null", "[]", "42", None,
                                     json.dumps({"request_id": 1, "error": "private worker details"})])
def test_malformed_reply_eof_or_worker_error_poison_transport(bridge, message):
    bridge.messages.put(message)
    with pytest.raises((ValueError, TypeError, AttributeError)) as caught:
        bridge.result(TOKEN)
    assert "private worker details" not in str(caught.value)
    assert_poisoned_without_more_writes(bridge)


def test_timeout_cannot_be_repaired_by_a_late_submitted_reply(bridge):
    with pytest.raises(ValueError, match="timed out"):
        bridge.result(TOKEN)
    assert len(bridge.process.stdin.writes) == 1
    assert_poisoned_without_more_writes(bridge)


def test_late_previous_reply_is_not_consumed_as_the_next_rpc_result(bridge):
    bridge.messages.put(reply(1))
    assert bridge.result(TOKEN)["status"] == "submitted"
    bridge.messages.put(reply(1))
    with pytest.raises(ValueError, match="correlation"):
        bridge.cancel(TOKEN)
    sent = [json.loads(line) for line in bridge.process.stdin.writes]
    assert [item["request_id"] for item in sent] == [1, 2]
    assert [item["method"] for item in sent] == ["result", "cancel"]
    assert_poisoned_without_more_writes(bridge)


def test_broken_pipe_poisoning_prevents_any_retry_write(bridge):
    bridge.process.stdin.failure = BrokenPipeError("worker died")
    with pytest.raises(BrokenPipeError):
        bridge.result(TOKEN)
    assert bridge.process.stdin.writes == []
    assert_poisoned_without_more_writes(bridge)


@pytest.mark.parametrize("token", [OTHER_TOKEN + "\n", True, "z" * 32])
def test_bad_tokens_fail_before_transport_side_effects(bridge, token):
    with pytest.raises(ValueError, match="token"):
        bridge.result(token)
    assert not bridge.process.stdin.writes and not bridge.messages.read_timeouts
    assert bridge.poisoned is False


_VM_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = process.argv[2];
let callback, context;
const counters = {created: 0, validated: 0, dispatched: 0, destroyed: 0, concurrencyRejected: false};
const token = 'a'.repeat(32);
const other = 'b'.repeat(32);
const request = {kind:'select_research', country_id:'0', area:'physics', technology:'tech_shields_2', token};
const command = {
    isNull() { return false; },
    add() { return {writeU32() {}, writePointer() {}}; },
    readPointer() { return {readPointer() { return {offset:'destructor'}; }}; }
};
context = {
    Process: {
        getModuleByName() { return {base:{add(offset) {return {offset};}}}; },
        getCurrentThreadId() { return 77; }
    },
    NativeFunction: function(address) {
        if (address.offset === 0x6d1b90) return () => { counters.created++; return command; };
        if (address.offset === 0x6bb4f0) return () => {
            counters.validated++;
            if (scenario === 'in_flight') {
                try { context.rpc.exports.enqueue({...request, token:other}); }
                catch (error) { counters.concurrencyRejected = /pending/.test(String(error)); }
            }
            return scenario === 'destructor_failure' ? 0 : 1;
        };
        if (address.offset === 0x5f88a0) return () => {
            counters.dispatched++;
            if (scenario === 'dispatch_failure') throw new Error('uncertain native admission');
        };
        if (address.offset === 'destructor') return () => {
            counters.destroyed++;
            throw new Error('destructor failed after freeing the object');
        };
        // New fixed command kinds may register functions at load time. A test
        // fails if research unexpectedly calls any of those native functions.
        return () => {throw new Error('unexpected mock native call');};
    },
    Interceptor: {attach(address, handlers) { callback = handlers.onLeave; }},
    ptr() { return {isNull() {return true;}}; },
    rpc: {exports:{}}
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.technology = () => ({isNull() {return false;}});
const first = context.rpc.exports.enqueue(request);
let rejectedPayload = false;
let originalPreserved = false;
let replayStatus;
if (scenario === 'token_binding') {
    const reordered = {token, technology:request.technology, area:'physics', country_id:'0', kind:'select_research'};
    replayStatus = context.rpc.exports.enqueue(reordered).status;
    try { context.rpc.exports.enqueue({...request, technology:'tech_shields_3'}); }
    catch (error) { rejectedPayload = /different request/.test(String(error)); }
    originalPreserved = context.rpc.exports.result(token).technology === request.technology;
    context.rpc.exports.cancel(token);
    replayStatus = context.rpc.exports.enqueue(request).status;
} else {
    callback();
    callback(); // A second game tick must not replay a consumed token.
    if (scenario === 'in_flight') context.rpc.exports.enqueue({...request, token:other});
}
console.log(JSON.stringify({first, state:context.rpc.exports.result(token), counters,
    rejectedPayload, originalPreserved, replayStatus, real_native_calls:0}));
"""


def vm_result(scenario, harness=_VM_HARNESS):
    if NODE is None:
        pytest.skip("Node.js is unavailable; the transport tests still run without it")
    script = Path(native.__file__).with_name("native_research.js")
    completed = subprocess.run(
        [NODE, "-e", harness, str(script), scenario],
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=True,
    )
    return json.loads(completed.stdout)


def test_actual_native_js_destroys_rejected_command_at_most_once():
    result = vm_result("destructor_failure")
    assert result["real_native_calls"] == 0
    assert result["state"]["status"] == "ambiguous"
    assert result["state"]["token"] == TOKEN
    assert result["state"]["consumed"] is True
    assert result["counters"]["created"] == result["counters"]["validated"] == 1
    assert result["counters"]["destroyed"] == 1
    assert result["counters"]["dispatched"] == 0


def test_actual_native_js_rejects_token_rebinding_and_never_rearms_cancelled_token():
    result = vm_result("token_binding")
    assert result["first"]["status"] == "pending"
    assert result["rejectedPayload"] and result["originalPreserved"]
    assert result["replayStatus"] == "cancelled"
    assert result["state"]["technology"] == "tech_shields_2"
    assert result["counters"]["created"] == result["counters"]["dispatched"] == 0


def test_actual_native_js_never_destroys_or_replays_after_dispatch_transfer():
    result = vm_result("dispatch_failure")
    assert result["state"]["status"] == "ambiguous"
    assert result["state"]["consumed"] is True
    assert result["counters"]["created"] == result["counters"]["dispatched"] == 1
    assert result["counters"]["destroyed"] == 0


def test_actual_native_js_blocks_a_second_request_while_processing():
    result = vm_result("in_flight")
    assert result["counters"]["concurrencyRejected"] is True
    assert result["state"]["status"] == "submitted"
    assert result["counters"]["created"] == result["counters"]["dispatched"] == 1
    assert result["counters"]["destroyed"] == 0


_CONTROL_VM_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const scenario = process.argv[2];
const token = 'a'.repeat(32), other = 'b'.repeat(32);
const counters = {assign:0, run:0, dump:0, resultCleanup:0, stringCleanup:0,
    factory:0, validate:0, dispatch:0, gameStateWrites:0, concurrencyRejected:false};
let callback, context, command, nativeInput;
class Pointer {
    constructor(block, offset=0) { this.block=block; this.offset=offset; }
    add(offset) { return new Pointer(this.block, this.offset+offset); }
    isNull() { return this.block === null; }
    readPointer() {
        if (this.block === image && this.offset === 0x3114e68)
            return scenario.includes('null_state') ? NULL : state;
        if (this.block === state.block && this.offset === 0xf90)
            return scenario.includes('null_manager') ? NULL : manager;
        return this.block.refs.get(this.offset) || NULL;
    }
    writePointer(value) { this.block.refs.set(this.offset, value); }
    writeByteArray(value) { Buffer.from(value).copy(this.block.bytes, this.offset); }
    writeUtf8String(value) { this.block.bytes.write(value+'\0', this.offset, 'utf8'); }
    readUtf8String(length) {
        const end=length === undefined ? this.block.bytes.indexOf(0, this.offset) : this.offset+length;
        return this.block.bytes.toString('utf8', this.offset, end < 0 ? undefined : end);
    }
    writeU64(value) { this.block.bytes.writeBigUInt64LE(BigInt(value),this.offset); }
    readU64() { const value=this.block.bytes.readBigUInt64LE(this.offset); return {toNumber(){return Number(value);}}; }
    writeS32(value) { this.block.bytes.writeInt32LE(value,this.offset); }
    readS32() { return this.block.bytes.readInt32LE(this.offset); }
    readU8() { return this.block.bytes.readUInt8(this.offset); }
    writeU8(value) {
        if (this.block===state.block) counters.gameStateWrites++;
        if (this.block===command?.block) this.block.writes.push({offset:this.offset,value});
        this.block.bytes.writeUInt8(value,this.offset);
    }
}
function allocate(size,tag='') {
    return new Pointer({bytes:Buffer.alloc(size),refs:new Map(),tag,writes:[]});
}
const NULL=new Pointer(null);
const image=allocate(8).block, base=new Pointer(image);
const state=allocate(0x1000), manager=allocate(8);
const byteCase=scenario.match(/^pause_bytes_(\d+)_(\d+)$/);
state.block.bytes[0x594]=byteCase ? Number(byteCase[1]) : 1;
state.block.bytes[0x595]=byteCase ? Number(byteCase[2]) : (scenario==='pause_protected' ? 1 : 0);
function argument(args, expected, long) {
    if(args.add(0x10).readS32()!==1 || args.add(0x14).readS32()!==1) throw new Error('bad args count');
    const text=args.add(8).readPointer();
    const capacity=text.add(0x28).readU64().toNumber();
    const length=text.add(0x20).readU64().toNumber();
    const buffer=capacity>=16 ? text.add(0x10).readPointer() : text.add(0x10);
    if(length!==expected.length || capacity!==(long ? 31 : 15) || buffer.readUtf8String(length)!==expected)
        throw new Error('incorrect fixed CString');
    return text;
}
context={
    Process:{id:999,getModuleByName(){return {base};},getCurrentThreadId(){return 77;}},
    Memory:{alloc:allocate,allocUtf8String(text){const value=allocate(Buffer.byteLength(text)+1);value.writeUtf8String(text);return value;}},
    NativeFunction:function(address){
        switch(address.offset){
        case 0x15bcf0: return (text,source,length)=>{
            counters.assign++;
            if(text.add(0x20).readU64().toNumber()!==0 || text.add(0x28).readU64().toNumber()!==15 || length!==19)
                throw new Error('bad initial CString');
            const heap=allocate(32,'owned input');
            heap.writeUtf8String(source.readUtf8String(length));
            text.add(0x10).writePointer(heap); text.add(0x20).writeU64(length);text.add(0x28).writeU64(31);
            nativeInput=text;
            if(scenario==='inbox_assign_error') throw new Error('uncertain assignment');
            return text;
        };
        case 0xeba020: return (result,args)=>{
            counters.run++;
            argument(args,'qs_direct_inbox.txt',true);
            try { context.rpc.exports.enqueue({kind:'run_inbox',country_id:'0',token:other}); }
            catch(error) { counters.concurrencyRejected=/pending/.test(String(error)); }
            callback(); // The real run callback also triggers the installed hook.
            if(scenario==='inbox_run_error') throw new Error('uncertain run');
            return result;
        };
        case 0xebc230: return (result,args)=>{
            counters.dump++;
            argument(args,'qs_direct',false);
            return result;
        };
        case 0x160ff0: return ()=>{
            counters.resultCleanup++;
            if(scenario==='inbox_result_error') throw new Error('result cleanup failed after freeing');
        };
        case 0x15be90: return text=>{
            counters.stringCleanup++;
            if(text!==nativeInput || text.block.freed) throw new Error('invalid or repeated input cleanup');
            text.block.freed=true;
            if(scenario==='inbox_string_error') throw new Error('input cleanup failed after freeing');
        };
        case 0x961930: return ()=>{
            counters.factory++;
            command=allocate(0x58);
            command.block.bytes[0x50]=1;command.block.bytes[0x51]=2;
            command.add(0x48).writeU64(15); // Native factory's empty source CString.
            return command;
        };
        case 0x1ae1f0: return ()=>{counters.validate++;return 1;};
        case 0x5f88a0: return value=>{
            counters.dispatch++;
            counters.modeAtDispatch=value.add(0x51).readU8();
            counters.sourceLength=value.add(0x40).readU64().toNumber();
            if(state.block.bytes[0x595]===0) state.block.bytes[0x594]=value.add(0x50).readU8();
        };
        default:return ()=>{throw new Error('unused mock native call');};
        }
    },
    Interceptor:{attach(address,handlers){callback=handlers.onLeave;return {detach(){}};}},
    ptr(){return NULL;},rpc:{exports:{}}
};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
let observed, outcome, nextStatus, changedKindRejected=false;
if(byteCase || scenario==='pause_null_state') {
    observed=context.pauseState();
} else {
    const request=scenario.startsWith('pause_') ? {kind:'set_paused',country_id:'0',paused:false,token}
        : {kind:scenario.startsWith('inbox_') ? 'run_inbox' : 'request_observation',country_id:'0',token};
    callback(); // Establish the pinned thread before enqueuing a pause.
    context.rpc.exports.enqueue(request);
    if(scenario==='pause_protected') context.consumePending('window_message');
    callback();callback(); // No request is consumed twice, including a nested run.
    outcome=context.rpc.exports.result(token);
    observed=context.pauseState();
    try { context.rpc.exports.enqueue({...request,kind:request.kind==='run_inbox' ? 'request_observation' : 'run_inbox'}); }
    catch(error) { changedKindRejected=true; }
    nextStatus=context.rpc.exports.enqueue({kind:'request_observation',country_id:'0',token:other}).status;
}
console.log(JSON.stringify({counters,outcome,observed,nextStatus,changedKindRejected,
    commandWrites:command?.block.writes,real_native_calls:0}));
"""


@pytest.mark.parametrize("operation", ["dump", "inbox"])
@pytest.mark.parametrize("missing", ["state", "manager"])
def test_actual_control_callbacks_reject_missing_game_context_before_native_call(operation, missing):
    result = vm_result(f"{operation}_null_{missing}", _CONTROL_VM_HARNESS)
    assert result["outcome"]["status"] in {"ambiguous", "blocked"}
    assert result["counters"]["assign"] == result["counters"]["run"] == result["counters"]["dump"] == 0
    assert result["counters"]["resultCleanup"] == result["counters"]["stringCleanup"] == 0


@pytest.mark.parametrize("scenario,status", [
    ("inbox_normal", "inbox_executed"), ("inbox_result_error", "ambiguous"),
    ("inbox_string_error", "ambiguous"),
])
def test_actual_fixed_inbox_cleans_independent_native_buffers_once(scenario, status):
    result = vm_result(scenario, _CONTROL_VM_HARNESS)
    assert result["outcome"]["status"] == status
    assert result["counters"]["assign"] == result["counters"]["run"] == 1
    assert result["counters"]["resultCleanup"] == result["counters"]["stringCleanup"] == 1
    assert result["counters"]["concurrencyRejected"] is True
    assert result["changedKindRejected"] is True and result["nextStatus"] == "pending"


@pytest.mark.parametrize("scenario,run_calls", [("inbox_assign_error", 0), ("inbox_run_error", 1)])
def test_actual_fixed_inbox_does_not_guess_cleanup_after_ambiguous_native_failure(scenario, run_calls):
    result = vm_result(scenario, _CONTROL_VM_HARNESS)
    assert result["outcome"]["status"] == "ambiguous"
    assert result["counters"]["assign"] == 1 and result["counters"]["run"] == run_calls
    assert result["counters"]["resultCleanup"] == result["counters"]["stringCleanup"] == 0
    assert result["nextStatus"] == "pending"


def test_actual_fixed_dump_uses_only_inline_fixed_suffix_and_result_cleanup():
    result = vm_result("dump_normal", _CONTROL_VM_HARNESS)
    assert result["outcome"]["status"] == "dump_requested"
    assert result["counters"]["dump"] == result["counters"]["resultCleanup"] == 1
    assert result["counters"]["assign"] == result["counters"]["stringCleanup"] == 0


@pytest.mark.parametrize("paused,protected,expected", [
    (0, 0, {"paused": False, "protected_pause": False}),
    (1, 1, {"paused": True, "protected_pause": True}),
    (2, 255, {"paused": None, "protected_pause": None}),
])
def test_actual_pause_reader_preserves_unknown_bytes(paused, protected, expected):
    result = vm_result(f"pause_bytes_{paused}_{protected}", _CONTROL_VM_HARNESS)
    assert result["observed"] == expected
    assert result["counters"]["gameStateWrites"] == 0


def test_actual_pause_reader_preserves_missing_game_context():
    result = vm_result("pause_null_state", _CONTROL_VM_HARNESS)
    assert result["observed"] == {"paused": None, "protected_pause": None}


def test_actual_pause_request_keeps_native_protection_and_does_not_claim_success():
    result = vm_result("pause_protected", _CONTROL_VM_HARNESS)
    assert result["outcome"]["status"] == "submitted"
    assert result["outcome"]["paused"] is False  # This is the request echo, not an observation.
    assert result["observed"] == {"paused": True, "protected_pause": True}
    assert result["counters"]["modeAtDispatch"] == 2 and result["counters"]["sourceLength"] == 0
    assert result["commandWrites"] == [{"offset": 0x50, "value": 0}]
    assert result["counters"]["gameStateWrites"] == 0


def test_pause_never_uses_the_daily_run_fallback():
    result = vm_result("pause_no_wake", _CONTROL_VM_HARNESS)
    assert result["outcome"]["status"] == "blocked"
    assert result["outcome"]["submitted"] is False
    assert result["counters"]["factory"] == result["counters"]["dispatch"] == 0


def test_failed_native_post_revokes_historical_wakeup_confirmation(bridge):
    bridge.wakeup_confirmed = True
    bridge.messages.put(reply(1, status="pending", wake_posted=False))
    bridge.request_dump(TOKEN)
    assert bridge.supports_wakeup is False
    bridge.messages.put(reply(2, execution_source="window_message"))
    bridge.result(TOKEN)
    assert bridge.supports_wakeup is True


def test_native_pause_and_fixed_inbox_rpc_have_no_free_text_or_path(bridge):
    bridge.messages.put(reply(1, status="pending"))
    bridge.submit(native.pause_request(False), TOKEN)
    bridge.messages.put(reply(2, OTHER_TOKEN, status="pending"))
    bridge.run_inbox(OTHER_TOKEN)
    payloads = [json.loads(line)["arguments"][0] for line in bridge.process.stdin.writes]
    assert payloads == [
        {"kind": "set_paused", "token": TOKEN, "country_id": "0", "paused": False},
        {"kind": "run_inbox", "token": OTHER_TOKEN, "country_id": "0"},
    ]


_PLANET_QUEUE_VM = r"""
const fs=require('fs'),vm=require('vm'),scenario=process.argv[2];
class Ref {
    constructor(tag,offset=0){this.tag=tag;this.offset=offset;this.value=0;}
    add(value){return new Ref(this.tag,this.offset+value);}
    isNull(){return this.tag==='null';}
    readPointer(){
        if(this.tag==='base' && this.offset===0x3114e28)return new Ref('pool');
        if(this.tag==='pool' && this.offset===0x18)return new Ref('entries');
        if(this.tag==='entries')return new Ref(this.offset===8?'planetA':'planetB');
        if(this.tag.startsWith('owner') && this.offset===0)return new Ref('vtable');
        if(this.tag==='vtable' && this.offset===0x58)return new Ref('getter');
        throw new Error('unexpected pointer read '+this.tag+this.offset);
    }
    readU32(){
        if(this.tag==='pool' && this.offset===0x20)return 2;
        if(this.tag==='planetA' && this.offset===0x10)return 0;
        if(this.tag==='planetB' && this.offset===0x10)return 1;
        if(this.tag==='ownerA' && this.offset===0xc4)return scenario==='duplicate'?0:7;
        if(this.tag==='ownerB' && this.offset===0xc4)return 0;
        if(this.tag==='output')return this.value;
        throw new Error('unexpected integer read '+this.tag+this.offset);
    }
    readU64(){return this.tag==='planetA'?'A':'B';}
    writeByteArray(){this.value=0;}
}
const context={
    Process:{getModuleByName(){return {base:new Ref('base')};}},
    Memory:{alloc(){return new Ref('output');}},
    NativeFunction:function(address){
        if(address.offset===0xccd000 && address.tag==='base')return tag=>new Ref('owner'+tag);
        if(address.tag==='getter')return (owner,output)=>{output.value=scenario==='foreign' && owner.tag==='ownerB'?5:0;return output;};
        return ()=>{throw new Error('unexpected native call');};
    },Interceptor:{attach(){}},rpc:{exports:{}}
};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
let result,error;
try{result=context.colonyForQueue('0','1');}catch(value){error=String(value);}
console.log(JSON.stringify({result,error,real_native_calls:0}));
"""


def test_native_planet_is_resolved_from_exact_owned_queue_not_assumed_equal_to_save_id():
    result = vm_result("mapped", _PLANET_QUEUE_VM)
    assert result["result"] == {"handle": 1} and result["real_native_calls"] == 0


def test_native_colony_id_is_independent_of_other_planets_and_rejects_foreign_ownership():
    assert vm_result("duplicate", _PLANET_QUEUE_VM)["result"] == {"handle": 1}
    assert vm_result("foreign", _PLANET_QUEUE_VM)["result"] is None
