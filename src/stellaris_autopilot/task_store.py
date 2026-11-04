"""Short durable task transactions, independent of game snapshots and writer locks."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from .task_plan import request_identifier


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS tasks (request_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, record TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _read(db, request_id):
        row = db.execute("SELECT record FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    @staticmethod
    def _write(db, record):
        record["updated_at"] = time.time()
        db.execute("UPDATE tasks SET record=? WHERE request_id=?",
                   (json.dumps(record, ensure_ascii=False, allow_nan=False), record["request_id"]))

    @staticmethod
    def _visible(record):
        from .task_owner import owner_alive

        if record is not None and record["status"] == "running" and owner_alive(record["owner"]) is False:
            record["status"] = "interrupted"
            record["reason"] = "The original executor exited; inspect the command journal. This ID is never dispatched again."
            for step in record["steps"]:
                if step["status"] == "executing":
                    step["status"] = "unverified"
                elif step["status"] == "pending":
                    step["status"] = "skipped"
        return record

    def get(self, request_id: str) -> dict:
        request_identifier(request_id)
        if not self.path.exists():
            return {"status": "not_found", "request_id": request_id}
        # A read never loads live configuration, waits for a plan, or creates the database.
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            record = self._visible(self._read(db, request_id))
            return record or {"status": "not_found", "request_id": request_id}
        finally:
            db.close()

    def claim(self, request_id: str, plan_hash: str, steps: list[dict], expected_snapshot, owner: dict) -> tuple[dict, bool]:
        request_identifier(request_id)
        with self._transaction() as db:
            old = self._read(db, request_id)
            if old is not None:
                if old["plan_hash"] != plan_hash:
                    return {"status": "conflict", "request_id": request_id,
                            "reason": "This request_id belongs to a different normalized plan"}, False
                return self._visible(old), False
            record = {"request_id": request_id, "plan_hash": plan_hash, "status": "running",
                      "owner": owner, "cancel_requested": False, "expected_snapshot": expected_snapshot,
                      "completion_scope": "dispatch_verification_only",
                      "created_at": time.time(), "updated_at": time.time(),
                      "steps": [{"index": i, "request": step, "status": "pending"} for i, step in enumerate(steps)],
                      "note": "Dispatch verification is separate from natural completion. Never resubmit with a new ID after a lost reply."}
            db.execute("INSERT INTO tasks VALUES (?,?,?)", (request_id, plan_hash, json.dumps(record)))
            return record, True

    def cancel(self, request_id: str) -> dict:
        request_identifier(request_id)
        if not self.path.exists():
            return {"status": "not_found", "request_id": request_id}
        with self._transaction() as db:
            record = self._visible(self._read(db, request_id))
            if record is None:
                return {"status": "not_found", "request_id": request_id}
            if record["status"] == "running":
                record["cancel_requested"] = True
                self._write(db, record)
                return {**record, "status": "cancel_requested",
                        "note": "Future steps will stop; the current step still needs verification and cleanup. Submitted orders are not undone."}
            return record

    def start_step(self, request_id: str, index: int) -> bool:
        with self._transaction() as db:
            record = self._read(db, request_id)
            if record["status"] != "running" or record["cancel_requested"]:
                return False
            if record["steps"][index]["status"] != "pending":
                raise ValueError("A task step cannot be submitted twice")
            record["steps"][index]["status"] = "executing"
            self._write(db, record)
            return True

    def step_result(self, request_id: str, index: int, status: str, result: dict):
        with self._transaction() as db:
            record = self._read(db, request_id)
            if record["steps"][index]["status"] != "executing":
                raise ValueError("Task step has no active durable claim")
            record["steps"][index].update(status=status, result=result)
            self._write(db, record)

    def finish(self, request_id: str, status: str, *, reason: str | None = None) -> dict:
        with self._transaction() as db:
            record = self._read(db, request_id)
            record["status"] = status
            if reason is not None:
                record["reason"] = reason
            for step in record["steps"]:
                if step["status"] == "pending":
                    step.update(status="skipped", reason="The plan stopped before this step was submitted")
                elif step["status"] == "executing":
                    step.update(status="unverified", reason="Execution was interrupted; inspect the command journal before any retry")
            self._write(db, record)
            return record
