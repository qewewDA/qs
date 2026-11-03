"""MCP for a manually opened, paused Stellaris save. Never launches a game."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from mcp.types import ToolAnnotations
from pydantic import StrictBool

from stellaris_autopilot.attached_runtime import connect_existing, resolve_user_dir
from stellaris_autopilot.direct_mcp import build_direct_server
from stellaris_autopilot.knowledge import GameKnowledge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=Path(r"D:\steam\steamapps\common\Stellaris"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--user-dir", type=Path)
    parser.add_argument("--pid", type=int, help="Optional local PID selection when multiple games exist")
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--knowledge-bundle", type=Path)
    args = parser.parse_args()
    knowledge = GameKnowledge.load(args.knowledge_bundle) if args.knowledge_bundle is not None else None
    runtime = None
    try:
        runtime = connect_existing(args.game_dir / "stellaris.exe", args.state_dir,
                                   user_dir=resolve_user_dir(args.game_dir, args.user_dir),
                                   pid=args.pid, extended=args.extended)
        server = build_direct_server(runtime.config_path, native_bridge=runtime.native,
                                     game_knowledge=knowledge, connection=runtime.connection,
                                     existing_game=True)
        readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
        writing = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

        @server.tool(annotations=readonly)
        def attached_status() -> dict:
            """Read the existing-game attachment and checkpoint state. This session never owns the game."""
            return runtime.status()

        @server.tool(annotations=writing)
        async def checkpoint_game() -> dict:
            """Pause, verify and write a new save under save games/qs_mcp; keep the game running."""
            return await asyncio.to_thread(runtime.checkpoint_game)

        @server.tool(annotations=writing)
        async def disconnect_game(save: StrictBool = True) -> dict:
            """Optionally checkpoint, then detach MCP. The manually opened game remains running."""
            return await asyncio.to_thread(runtime.disconnect_game, save=save)

        server.run(transport="stdio")
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        print("Existing-game connection failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from None
