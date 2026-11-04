"""Persistent campaign MCP launcher. All model-facing tools have fixed paths.

Unlike the disposable trial launchers, reconnecting loads the last immutable
checkpoint. A failed save leaves the owned game running for manual recovery.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from mcp.types import ToolAnnotations

from stellaris_autopilot.campaign import CampaignError, CampaignStore, _atomic
from stellaris_autopilot.campaign_runtime import CampaignRuntime, remove_owned_redirect, validate_format_proof
from stellaris_autopilot.direct import NATIVE_ACTIONS, ORDER_KINDS, OWNERSHIP_HEADER, disarm_inbox, load_direct_config
from stellaris_autopilot.direct_mcp import build_direct_server
from stellaris_autopilot.direct_orders import render_target_events
from stellaris_autopilot.knowledge import GameKnowledge
from stellaris_autopilot.native import NativeResearchBridge, PINNED_EXE_SHA256
from stellaris_autopilot.owned_process import OwnedGameProcess, check_steam_session, launch_game
from stellaris_autopilot.profile_settings import seed_profile_settings


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE_BUNDLE = REPO_ROOT / "artifacts" / "qs-game-knowledge.json"
DEFAULT_GAME = Path(r"D:\steam\steamapps\common\Stellaris")


def put(path: Path, text: str):
    _atomic(path, text.encode("utf-8"))


def no_other_game():
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "@(Get-Process -Name stellaris -ErrorAction SilentlyContinue).Count",
    ], check=True, capture_output=True, text=True)
    if result.stdout.strip() != "0":
        raise CampaignError("Stellaris is already running; this launcher never closes an unrelated process")


def prepare_profile(store: CampaignStore, session: dict, *, allowed_native_actions=None, allowed_orders=None) -> Path:
    """Pin the exact latest checkpoint; never copy the initial save over it."""
    manifest = store.load()
    source, checkpoint = store.latest()
    userdir = store.root / "userdir"
    destination = userdir / "save games" / "qs_campaign" / (checkpoint["id"] + ".sav")
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != checkpoint["sha256"]:
            raise CampaignError("The campaign load copy was modified; inspect it locally")
    else:
        _atomic(destination, source.read_bytes())
    put(userdir / "continue_game.json", json.dumps({
        "title": "save games/qs_campaign/" + checkpoint["id"],
        "desc": "QS persistent campaign", "date": checkpoint["date"],
    }))
    seed_profile_settings(userdir)
    descriptor = 'name="QS persistent command bridge"\nversion="0.2"\nsupported_version="4.5.*"\n'
    mod = userdir / "mod" / "qs_campaign_bridge"
    put(mod / "descriptor.mod", descriptor)
    put(userdir / "mod" / "qs_campaign_bridge.mod", descriptor + f'path="{mod.as_posix()}"\n')
    put(userdir / "dlc_load.json", json.dumps({"enabled_mods": ["mod/qs_campaign_bridge.mod"], "disabled_dlcs": []}))
    token = session["token"]
    put(mod / "events" / "qs_campaign.txt", f'''namespace = qs_campaign
country_event = {{
 id = qs_campaign.1 hide_window = yes is_triggered_only = yes
 immediate = {{ if = {{ limit = {{ is_ai = no is_multiplayer = no is_ironman = no }}
  set_country_flag = qs_direct_session_{token}
 }} }}
}}
''')
    put(mod / "events" / "qs_direct_targets.txt", render_target_events(token))
    # Start paused at the loaded date. There is deliberately no dated mailbox
    # scheduler and no reset to 2200.01.01 during reconnection.
    put(userdir / "commands_at_date.txt", "")
    put(userdir / "qs_campaign_bootstrap.txt", '''game_paused true
event qs_campaign.1 0
event qs_direct_targets.3 0
script_docs
debug_dumpgamestate qs_direct
''')
    config_path = store.root / "sessions" / token / "direct.json"
    config = {"user_dir": str(userdir), "state_dir": str(config_path.parent),
              "country_id": manifest["country_id"], "campaign_id": manifest["campaign_id"],
              "verified_game_version": manifest["game_version"], "session_token": token,
              "allowed_actions": ["probe_connection", "hold_military_fleets"],
              "allowed_orders": ["fleet_wait", "fleet_orbit"] if allowed_orders is None else allowed_orders,
              "allowed_native_actions": ["select_research", "queue_district", "queue_building", "set_paused"]
              if allowed_native_actions is None else allowed_native_actions,
              "single_player_confirmed": True, "non_ironman_confirmed": True,
              "verification_timeout_seconds": 120, "poll_seconds": 0.25}
    put(config_path, json.dumps(config, indent=2))
    parsed = load_direct_config(config_path)
    # A previous session's inbox cannot be consumed by this bootstrap. Inspect
    # previous unresolved journals before begin_session; then replace only the
    # old campaign-owned mailbox with the new session's disarmed header.
    inbox = parsed.inbox_path
    if inbox.exists():
        first = inbox.read_text(encoding="utf-8").splitlines()[0]
        if not first.startswith(OWNERSHIP_HEADER + " "):
            raise CampaignError("Mailbox does not belong to the direct bridge")
        inbox.rename(inbox.with_name(f"qs_direct_inbox_{time.time_ns()}.archive"))
    disarm_inbox(parsed)
    return config_path


def launch_owned(store: CampaignStore, game_dir: Path) -> OwnedGameProcess:
    """Create and hold the exact game handle outside a client's kill-on-close job."""
    no_other_game()
    if not check_steam_session():
        raise CampaignError("Steam is not signed in; sign in to the Steam client, then reconnect")
    executable = game_dir / "stellaris.exe"
    if hashlib.sha256(executable.read_bytes()).hexdigest() != PINNED_EXE_SHA256:
        raise CampaignError("Unsupported game executable; persistent native control is disabled")
    userdir = store.root / "userdir"
    redirect = game_dir / "userdir.txt"
    expected = userdir.as_posix() + "/"
    if redirect.exists():
        if redirect.read_text(encoding="utf-8") != expected:
            raise CampaignError("Refusing to replace an unrelated userdir.txt")
    else:
        put(redirect, expected)
    session = store.load()["sessions"][-1]
    return launch_game(executable, userdir, session["token"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--initialize", type=Path)
    parser.add_argument("--country", default="0")
    parser.add_argument("--single-player", action="store_true")
    parser.add_argument("--non-ironman", action="store_true")
    parser.add_argument("--recover-last-checkpoint", action="store_true")
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME)
    parser.add_argument("--knowledge-bundle", type=Path,
                        help="Optional GameKnowledge JSON; default: <repo>/artifacts/qs-game-knowledge.json when present")
    parser.add_argument("--reload-proof", type=Path)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--extended", action="store_true", help="Enable the new precise adapters under experimental validation")
    args = parser.parse_args()
    store = CampaignStore(args.campaign_dir)
    with store.locked():
        if args.initialize is not None:
            store.initialize(args.initialize, country_id=args.country,
                             single_player_confirmed=args.single_player, non_ironman_confirmed=args.non_ironman)
        if args.status:
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
            return
        # The pinned native adapters currently bind country 0. Do not launch a
        # different human empire merely because a valid save can be parsed.
        if store.load()["country_id"] != "0":
            raise CampaignError("The current native backend requires country 0")
        no_other_game()
        if not check_steam_session():
            raise CampaignError("Steam is not signed in; sign in to the Steam client, then reconnect")
        format_verified = validate_format_proof(args.reload_proof, store.load())
        if args.reload_proof is not None and not format_verified:
            raise CampaignError("The supplied reload proof does not verify this checkpoint format and game build")
        session = store.begin_session(recover_last_checkpoint=args.recover_last_checkpoint)
        process = native = runtime = None
        try:
            config_path = prepare_profile(store, session,
                                          allowed_native_actions=sorted(NATIVE_ACTIONS) if args.extended else None,
                                          allowed_orders=sorted(ORDER_KINDS) if args.extended else None)
            process = launch_owned(store, args.game_dir)
            store.update_session(session["token"], status="connected", owned_pid=process.pid)
            native = NativeResearchBridge(process, args.game_dir / "stellaris.exe", config_path.parent)
            runtime = CampaignRuntime(store, session, config_path, process, native,
                                      format_verified=format_verified,
                                      game_dir=args.game_dir)
            bundle = args.knowledge_bundle if args.knowledge_bundle is not None else DEFAULT_KNOWLEDGE_BUNDLE
            if args.knowledge_bundle is not None and not bundle.is_file():
                raise CampaignError("The supplied knowledge bundle does not exist: " + str(bundle))
            knowledge = GameKnowledge.load(bundle) if bundle.is_file() else None
            server = build_direct_server(config_path, native_bridge=native, game_knowledge=knowledge,
                                         connection=runtime.connection, persistent_campaign=True,
                                         task_store_path=store.root / "tasks.sqlite")
            readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
            writing = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

            @server.tool(annotations=readonly)
            def campaign_status() -> dict:
                """Read persistent checkpoint identity, reload proof and unresolved commands."""
                return store.status()

            @server.tool(annotations=writing)
            async def checkpoint_campaign() -> dict:
                """Pause and atomically save the current campaign to a new immutable checkpoint."""
                return await asyncio.to_thread(runtime.checkpoint)

            @server.tool(annotations=writing)
            async def save_and_close() -> dict:
                """Verify a fresh checkpoint, then close only the game owned by this session."""
                return await asyncio.to_thread(runtime.checkpoint, close=True)

            with redirect_stdout(sys.__stdout__):
                server.run(transport="stdio")
        finally:
            if process is not None and process.poll() is None:
                try:
                    if runtime is None:
                        raise CampaignError("Native connection never became ready; cannot verify save")
                    result = runtime.checkpoint(close=True)
                    print(json.dumps(result), file=sys.stderr, flush=True)
                except BaseException as exc:
                    store.update_session(session["token"], status="save_failed", error=str(exc))
                    print("SAVE FAILED: owned game remains running for manual recovery: " + str(exc),
                          file=sys.stderr, flush=True)
            if native is not None:
                native.close()
            if process is not None and process.poll() is not None:
                remove_owned_redirect(store, args.game_dir)
            elif process is None:
                # WMI can create a game before an identity query fails. Keep
                # the redirect if such an unverified process may still exist.
                try:
                    no_other_game()
                except CampaignError:
                    pass
                else:
                    remove_owned_redirect(store, args.game_dir)
                    store.update_session(session["token"], status="interrupted",
                                         error="Startup did not complete; no game process is running")


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        main()
