"""Durable at-most-once dispatch. Ambiguous delivery requires reconciliation."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL, action TEXT NOT NULL,
            country TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(snapshot, action))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS operations_status ON operations(status)")
        self.db.commit()

    def close(self):
        self.db.close()

    def records(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM operations ORDER BY id")]

    def recent_records(self, limit: int = 100) -> list[dict]:
        """Bounded chronological summaries for planning, without full saved states."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("History limit must be an integer in 1..10000")
        rows = self.db.execute(
            "SELECT id,snapshot,action,country,status,detail,created_at "
            "FROM operations ORDER BY id DESC LIMIT ?", (limit,),
        )
        return list(reversed([dict(row) for row in rows]))

    def unresolved(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT id,snapshot,action,country,status,detail,created_at "
            "FROM operations WHERE status IN ('pending','dispatched','unverified') ORDER BY id"
        )]

    def claim(self, snapshot: str, action: str, state: dict) -> int | None:
        # SQLite's write lock also excludes another autopilot process using this journal.
        # Serialize before taking that lock: malformed state must not leave an
        # open write transaction that blocks every other autopilot process.
        before_json = json.dumps(state, ensure_ascii=False, allow_nan=False)
        country = state["country_id"]
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM operations WHERE status IN ('pending','dispatched','unverified') LIMIT 1"
            ).fetchone():
                self.db.rollback()
                return None
            cursor = self.db.execute(
                "INSERT INTO operations(snapshot,action,country,status,before_json) VALUES (?,?,?,'pending',?)",
                (snapshot, action, country, before_json),
            )
            self.db.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            self.db.rollback()
            if self.db.execute(
                "SELECT 1 FROM operations WHERE snapshot=? AND action=?", (snapshot, action),
            ).fetchone():
                return None
            # A different constraint failure is not evidence of a duplicate.
            raise
        except BaseException:
            self.db.rollback()
            raise

    def update(self, operation_id: int, status: str, detail: str):
        transitions = {
            "dispatched": ("pending",), "verified": ("dispatched",),
            "unverified": ("pending", "dispatched", "unverified"),
            "reconciled": ("pending", "dispatched", "unverified"),
        }
        if status not in transitions:
            raise ValueError("Invalid journal status")
        allowed = transitions[status]
        placeholders = ",".join("?" for _ in allowed)
        with self.db:
            cursor = self.db.execute(
                f"UPDATE operations SET status=?, detail=? WHERE id=? AND status IN ({placeholders})",
                (status, detail, operation_id, *allowed),
            )
            if cursor.rowcount != 1:
                raise ValueError("Operation missing or status changed; inspect the journal before continuing")

    def reconcile(self, operation_id: int, note: str):
        if not note.strip():
            raise ValueError("Reconciliation requires a human inspection note")
        self.update(operation_id, "reconciled", note)
