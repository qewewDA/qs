"""MCP for a manually opened, paused Stellaris save. Never launches a game."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from mcp.types import ToolAnnotations
from pydantic import StrictBool

from stellaris_autopilot.direct_mcp import build_direct_server
from stellaris_autopilot.existing_session import ExistingGameSession


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, default=Path(r"D:\steam\steamapps\common\Stellaris"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--user-dir", type=Path)
    parser.add_argument("--pid", type=int, help="Optional local PID selection when multiple games exist")
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--knowledge-bundle", type=Path)
    parser.add_argument("--connect-timeout", type=float, default=45,
                        help="Initialization budget in seconds; file work and cleanup can take extra time")
    args = parser.parse_args()
    session = ExistingGameSession(args.game_dir, args.state_dir, user_dir=args.user_dir,
                                  pid=args.pid, extended=args.extended, knowledge_bundle=args.knowledge_bundle,
                                  timeout_seconds=args.connect_timeout)
    try:
        server = build_direct_server(args.state_dir / "not-connected.json", connection=session,
                                     context_provider=session.context,
                                     task_store_path=args.state_dir / "tasks.sqlite",
                                     knowledge_provider=session.knowledge if args.knowledge_bundle is not None else None,
                                     existing_game=True)
        readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
        writing = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

        @server.tool(annotations=readonly)
        def attached_status() -> dict:
            """Read connection progress; an idle connection also checks its held process handles.

            No process discovery, attachment or save parsing. A stale connection requires an explicit retry.
            """
            return session.status()

        @server.tool(annotations=writing)
        async def connect_game(retry: StrictBool = False) -> dict:
            """Connect once to the manually opened paused save. Report failures; never retry automatically.

            MCP discovery is already ready before this call. Success includes a verified compact initial
            observation. Use retry=true only after the user corrects a previously reported failure.
            """
            return await session.connect_game_async(retry=retry)

        @server.tool(annotations=writing)
        async def checkpoint_game() -> dict:
            """Pause, verify and write a new save under save games/qs_mcp; keep the game running."""
            return await asyncio.to_thread(session.checkpoint_game)

        @server.tool(annotations=writing)
        async def disconnect_game(save: StrictBool = True) -> dict:
            """Optionally checkpoint, then detach MCP. The manually opened game remains running."""
            return await asyncio.to_thread(session.disconnect_game, save=save)

        server.run(transport="stdio")
    finally:
        session.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        print("Existing-game connection failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from None
