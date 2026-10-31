"""Read-only MCP context and action previews; live execution stays in the CLI.

Install ``.[mcp]`` to use this optional integration. All tools use current local
authorization configuration, never expose raw saves or credentials, and send no
game input. MCP is deliberately not a remote execution capability in this MVP.
"""

from __future__ import annotations

import copy
from pathlib import Path
import sqlite3
import stat
import tempfile

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import Config, load_config
from .engine import Engine, Proposal
from .observe import SaveObserver


class _PreviewJournal:
    """Minimal read-only journal interface used by ``Engine.run(live=False)``.

    A missing journal is empty. Query a stable private copy of the database and
    its WAL: SQLite's ``mode=ro`` alone may create WAL/SHM in the source directory.
    Original journal files are never opened by SQLite or changed by this reader.
    """

    MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, path: Path):
        self.path = path.resolve()

    def _signatures(self) -> dict[str, tuple]:
        result = {}
        for suffix in ("", "-wal"):
            try:
                info = Path(str(self.path) + suffix).stat()
            except FileNotFoundError:
                if suffix:
                    continue
                raise ValueError("Action journal disappeared while reading; retry after local writes finish") from None
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Action journal and WAL must be regular files")
            result[suffix] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if sum(signature[2] for signature in result.values()) > self.MAX_BYTES:
            raise ValueError("Action journal snapshot exceeds 64 MiB; archive locally before MCP preview")
        return result

    def _copy(self, destination: Path, signatures: dict[str, tuple]) -> None:
        copied = 0
        for suffix in signatures:
            with Path(str(self.path) + suffix).open("rb") as source:
                with Path(str(destination) + suffix).open("xb") as target:
                    while data := source.read(1024 * 1024):
                        copied += len(data)
                        if copied > self.MAX_BYTES:
                            raise ValueError("Action journal snapshot exceeds 64 MiB; archive locally before MCP preview")
                        target.write(data)

    def _read(self, *, unresolved: bool = False) -> list[dict]:
        query = "SELECT id,snapshot,action,country,status,created_at FROM operations"
        if unresolved:
            query += " WHERE status IN ('pending','dispatched','unverified')"
        query += " ORDER BY id DESC LIMIT 100"
        try:
            if not self.path.exists():
                return []
            for _ in range(3):
                before = self._signatures()
                # TemporaryDirectory is private (0700 on POSIX) and is removed
                # even if SQLite rejects an incomplete/corrupt local journal.
                with tempfile.TemporaryDirectory(prefix="saa-mcp-journal-") as temporary:
                    copy_path = Path(temporary) / "journal.sqlite"
                    self._copy(copy_path, before)
                    if before != self._signatures():
                        continue
                    # Never immutable=1: it silently ignores committed WAL
                    # entries, including unresolved game-input dispatches.
                    db = sqlite3.connect(copy_path.as_uri() + "?mode=ro", uri=True, timeout=5)
                    try:
                        db.row_factory = sqlite3.Row
                        return [dict(row) for row in db.execute(query)]
                    finally:
                        db.close()
            raise ValueError("Action journal changed during each snapshot attempt; retry after local writes finish")
        except (sqlite3.Error, OSError):
            raise ValueError("Cannot read the action journal; inspect it locally before planning") from None

    def unresolved(self) -> list[dict]:
        return self._read(unresolved=True)

    def records(self) -> list[dict]:
        return self._read()


def _configuration(path: Path) -> Config:
    try:
        return load_config(path)
    except (ValueError, TypeError, OSError):
        # Filesystem exception messages often contain full local paths. Keep
        # those on the user's machine rather than returning them to an MCP peer.
        raise ValueError("Cannot load local configuration; validate it with the CLI") from None


def _observer(config: Config) -> SaveObserver:
    return SaveObserver(config.save_path, config.country_id)


def _observation(observer: SaveObserver):
    try:
        return observer.read()
    except (ValueError, OSError):
        raise ValueError("Cannot read a stable supported save; inspect the save with the local CLI") from None


def _public_state(state: dict) -> dict:
    public = copy.deepcopy(state)
    public.get("metadata", {}).pop("source", None)
    return public


def build_server(config_path: Path) -> FastMCP:
    """Build a stdio server with observation, authorization and preview tools.

    Configuration is reloaded for every call so editing ``allowed_actions``
    revokes previews immediately. No tool accepts live flags, raw commands,
    coordinates, event IDs, save paths or environment variables from the peer.
    """
    config_path = Path(config_path)
    _configuration(config_path)
    server = FastMCP(
        "Stellaris AI Autopilot",
        instructions=(
            "Read the human player's normalized empire state and preview locally authorized actions. "
            "All tools are read-only: plan_action never executes. Live execution requires the local CLI --live. "
            "A dry-run preview is not a successful game action; unknown save fields are not zero."
        ),
        log_level="WARNING",
    )
    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @server.tool(annotations=annotations)
    def observe_empire() -> dict:
        """Read a stable save; return normalized player state and SHA-256 without local paths."""
        config = _configuration(config_path)
        observation = _observation(_observer(config))
        return {"snapshot": observation.digest, "modified_at": observation.modified_at,
                "state": _public_state(observation.state)}

    @server.tool(annotations=annotations)
    def list_actions() -> list[dict]:
        """List only actions currently authorized by the user's local configuration."""
        config = _configuration(config_path)
        return [{"id": action.id, "description": action.description, "category": action.category,
                 "preconditions": action.preconditions, "expected": action.expected}
                for action in config.actions.values() if action.id in config.allowed_actions]

    @server.tool(annotations=annotations)
    def plan_action(action_id: str, reason: str) -> dict:
        """Preview one locally allowed action; never send input or create a journal entry."""
        if not action_id or len(action_id) > 80 or not reason.strip() or len(reason) > 2000:
            raise ValueError("Provide an action ID (up to 80 characters) and reason (1–2000 characters)")
        config = _configuration(config_path)
        observer = _observer(config)
        observation = _observation(observer)
        journal = _PreviewJournal(config.state_dir / "journal.sqlite")
        # Do not construct a WindowsDriver. The literal false is intentional:
        # neither input arguments nor configuration can upgrade this to live.
        engine = Engine(config, journal, observer)
        return engine.run(Proposal(action_id, reason), observation, live=False)

    @server.tool(annotations=annotations)
    def status() -> dict:
        """Read up to 100 recent dispatch summaries without snapshots, paths or error details."""
        config = _configuration(config_path)
        journal = _PreviewJournal(config.state_dir / "journal.sqlite")
        return {"execution": "MCP is read-only; use the local CLI --live for execution",
                "operations": journal.records(), "unresolved": journal.unresolved()}

    return server


def main(config_path: Path) -> None:
    """Run MCP on stdio. Never print application messages to stdout."""
    build_server(config_path).run(transport="stdio")
