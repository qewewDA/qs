"""Generate an offline Chinese prompt entry from a local Stellaris MCP config."""
from __future__ import annotations

import argparse
from pathlib import Path

from stellaris_autopilot.assistant_entry import export_entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-config", type=Path, required=True, help="Local mcpServers JSON configuration")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory for the offline entry")
    parser.add_argument("--server-name", help="Select one server when the input contains several")
    args = parser.parse_args()
    try:
        result = export_entry(args.mcp_config, args.output_dir, server_name=args.server_name)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot export AI entry: {exc}\n")
    print(result)


if __name__ == "__main__":
    main()
