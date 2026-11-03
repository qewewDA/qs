"""Keep the experimental instrumentation library outside the game owner's process."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import frida

from .native import FRIDA_VERSION, PINNED_EXE_SHA256
import hashlib

root = Path(__file__).resolve().parent

if frida.__version__ != FRIDA_VERSION:
    raise SystemExit("Unverified instrumentation version")
if hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest() != PINNED_EXE_SHA256:
    raise SystemExit("Unverified executable hash")


session = script = None
try:
    session = frida.attach(int(sys.argv[1]))
    sources = ("native_research.js", "native_ship.js", "native_ship_colony.js", "native_planet.js", "native_fleet.js")
    script = session.create_script("\n".join((root / name).read_text(encoding="utf-8") for name in sources))
    script.load()
    print(json.dumps({"ready": True, "version": frida.__version__}), flush=True)
    for line in sys.stdin:
        message = json.loads(line)
        try:
            method = message["method"]
            if method == "lookup":
                result = script.exports_sync.lookup(*message["arguments"])
            elif method == "enqueue":
                result = script.exports_sync.enqueue(*message["arguments"])
            elif method == "result":
                result = script.exports_sync.result(*message["arguments"])
            elif method == "cancel":
                result = script.exports_sync.cancel(*message["arguments"])
            elif method == "bind_window":
                result = script.exports_sync.bindwindow(*message["arguments"])
            elif method == "close":
                print(json.dumps({"request_id": message["request_id"], "result": {"closed": True}}), flush=True)
                break
            else:
                raise ValueError("Unsupported worker method")
            print(json.dumps({"request_id": message["request_id"], "result": result}), flush=True)
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
            print(json.dumps({"request_id": message.get("request_id"), "error": str(exc)}), flush=True)
finally:
    if script is not None:
        try:
            script.unload()
        except Exception:
            pass
    if session is not None:
        try:
            session.detach()
        except Exception:
            pass
