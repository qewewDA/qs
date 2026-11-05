"""One bounded natural-completion runner; model polling never drives game time.

The background worker owns the existing writer lease through cleanup. Its
durable request identity is shared with short plans, but its completion scope
is an observed state goal plus verified final pause, not dispatch alone.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time

from .completion_goal import evaluate_goal, game_day, normalize_completion
from .direct_orders import Order, verify_order
from .task_owner import current_owner
from .task_plan import dispatch_step, request_identifier
from .task_store import TaskStore


class _Stop(Exception):
    def __init__(self, status: str, reason: str, *, unsafe: bool = False):
        self.status, self.reason, self.unsafe = status, reason, unsafe
        super().__init__(reason)


def _empty(fleet: dict) -> bool:
    return fleet.get("orders_known") is True and all(
        key not in fleet or isinstance(fleet[key], (dict, list)) and not fleet[key]
        for key in ("actions", "current_order", "order", "orders", "queued_orders"))


def _survey_order(fleet: dict) -> dict | None:
    container = fleet.get("current_order")
    if not isinstance(container, dict) or set(container) != {"survey_planet_order"}:
        return None
    order = container["survey_planet_order"]
    target = order.get("deposit_holder") if isinstance(order, dict) else None
    if (not isinstance(target, dict) or set(target) != {"type", "id"}
            or type(target["type"]) is not int or target["type"] != 0 or type(target["id"]) is not int):
        return None
    return order


class CompletionWorkflow:
    def __init__(self, connection, path: Path, *, poll_seconds: float = 2):
        if not 0 < poll_seconds <= 10:
            raise ValueError("Completion polling must be positive and at most ten seconds")
        self.connection, self.store, self.poll_seconds = connection, TaskStore(path), poll_seconds
        self._lock = threading.Lock()
        self._active: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._closed = False

    def submit(self, request_id: str, intent, time_authorization, expected_snapshot=None) -> dict:
        request_identifier(request_id)
        intent, authorization, digest = normalize_completion(intent, time_authorization, expected_snapshot)
        planets = intent.get("planet_ids", [intent.get("planet_id")])
        steps = [{"kind": "fleet_move" if intent["kind"] == "enter_system" else "survey_planet",
                  "fleet_id": intent["fleet_id"], "planet_id": planet} for planet in planets]
        with self._lock:
            if self._closed:
                return {"status": "blocked", "request_id": request_id, "reason": "The task host is shutting down"}
            record, claimed = self.store.claim(request_id, digest, steps, expected_snapshot, current_owner(), task_metadata={
                "task_kind": "natural_completion", "record_schema_version": 1,
                "completion_scope": "observed_goal_and_final_pause", "intent": intent,
                "time_authorization": authorization, "phase": "claimed", "goal_state": "pending",
                "final_pause": {"status": "not_attempted"}, "observations": 0,
                "note": "State satisfaction is not causal attribution. Cancellation stops future work; submitted game orders remain. Never replay an uncertain request with a new ID.",
            })
            if not claimed:
                return record
            cancel = threading.Event()
            thread = threading.Thread(target=self._execute, args=(record, cancel), daemon=True,
                                      name=f"qs-completion-{request_id}")
            self._active[request_id] = thread, cancel
            try:
                thread.start()
            except BaseException:
                self._active.pop(request_id, None)
                self.store.finish(request_id, "blocked", reason="The background worker could not start; no game operation was submitted")
                raise
        return self.store.get(request_id)

    def _execute(self, record, cancel):
        identifier = record["request_id"]
        try:
            result = self.connection.run_plan(lambda connection, config: _Run(
                self, record, cancel, connection, config).execute())
            if result.get("status") == "busy":
                self.store.finish(identifier, "busy", reason="Another writer owns the game; this task was not queued")
            else:
                # Publish terminal success only after writer/lifecycle cleanup.
                self.store.finish(identifier, result["status"], reason=result["reason"])
        except BaseException:
            # A live server with a dead worker must not leave a fictitious running task.
            state = self.store.get(identifier)
            if state.get("status") == "running":
                self.store.finish(identifier, "unverified", reason="The worker or writer cleanup failed; inspect task and operation journals before any retry")
            elif state.get("status") == "completed":
                self.store.finish(identifier, "unverified", reason="Writer cleanup failed after goal verification; inspect locally")
        finally:
            with self._lock:
                self._active.pop(identifier, None)

    def cancel(self, request_id):
        request_identifier(request_id)
        with self._lock:
            active = self._active.get(request_id)
            if active is not None:
                active[1].set()
        return self.store.cancel(request_id)

    async def result(self, request_id: str, wait_seconds: int = 0) -> dict:
        request_identifier(request_id)
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be an integer in 0..30")
        deadline = time.monotonic() + wait_seconds
        while True:
            record = await asyncio.to_thread(self.store.get, request_id)
            if record.get("status") != "running" or time.monotonic() >= deadline:
                return record
            # Cancelling this wait does not cancel the independently authorized worker.
            await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))

    async def shutdown(self):
        import anyio

        with self._lock:
            self._closed = True
            active = list(self._active.values())
            for _, cancel in active:
                cancel.set()
        cancelled = False
        with anyio.CancelScope(shield=True):
            for thread, _ in active:
                joining = asyncio.create_task(asyncio.to_thread(thread.join))
                while not joining.done():
                    try:
                        await asyncio.shield(joining)
                    except asyncio.CancelledError:
                        cancelled = True
                joining.result()
        if cancelled:
            raise asyncio.CancelledError


@dataclass
class _Run:
    host: CompletionWorkflow
    record: dict
    cancel: threading.Event
    connection: object
    config: object
    started: float = field(default_factory=time.monotonic)
    observation: dict | None = None
    first_day: int | None = None
    last_day: int | None = None
    observations: int = 0
    identity_safe: bool = True
    cleanup_allowed: bool = False

    @property
    def identifier(self):
        return self.record["request_id"]

    @property
    def intent(self):
        return self.record["intent"]

    def progress(self, **fields):
        return self.host.store.completion_progress(self.identifier, **fields)

    def check(self):
        if self.cancel.is_set() or self.host.store.get(self.identifier).get("cancel_requested"):
            raise _Stop("cancelled", "Future work cancelled; game orders already submitted are retained")
        authorization = self.record["time_authorization"]
        if time.monotonic() - self.started >= authorization["max_wall_seconds"]:
            raise _Stop("deadline", "The authorized wall-clock limit was reached")
        if self.first_day is not None and self.last_day - self.first_day >= authorization["max_game_days"]:
            raise _Stop("deadline", "The authorized game-day limit was reached; polling can overshoot a tick")

    def observe(self):
        from .direct_mcp import _configuration

        if _configuration(self.connection.config_path) != self.config:
            raise _Stop("interrupted", "Connection configuration changed", unsafe=True)
        native = self.connection.native_bridge
        if native is None or getattr(native, "supports_wakeup", False) is not True:
            raise _Stop("blocked", "Natural completion requires the pinned native wake transport", unsafe=True)
        try:
            result = self.connection.request_observation()
        except (ValueError, OSError, RuntimeError):
            raise _Stop("unverified", "A fresh correlated observation is unavailable", unsafe=True) from None
        if result.get("status") != "observed":
            raise _Stop("unverified", "The live observation failed; do not replay any submitted order", unsafe=True)
        correlated = result.get("native_observation", {})
        runtime = correlated.get("runtime", {})
        if (correlated.get("execution_source") != "window_message" or type(runtime.get("paused")) is not bool
                or type(runtime.get("protected_pause")) is not bool):
            raise _Stop("needs_observation", "Fresh native pause and protected-pause evidence is required", unsafe=True)
        try:
            day = game_day(result["state"]["date"])
        except (KeyError, ValueError):
            raise _Stop("needs_observation", "The game date cannot establish a bounded time window", unsafe=True) from None
        if self.last_day is not None and day < self.last_day:
            raise _Stop("interrupted", "Game date moved backwards; reconnect after loading a save", unsafe=True)
        self.observations += 1
        self.last_day = day
        self.observation = result
        return result["state"], runtime

    def goal(self, intent=None):
        result = evaluate_goal(self.observation["state"], intent or self.intent)
        evidence = {**result, "snapshot": self.observation["snapshot"], "date": self.observation["state"]["date"]}
        self.progress(last_evidence=evidence, observations=self.observations,
                      time_state={"game_days": self.last_day - self.first_day,
                                  "wall_seconds": time.monotonic() - self.started})
        if result["value"] is None:
            raise _Stop("needs_observation", result.get("reason", "Required goal evidence is unknown"))
        return evidence

    def write(self, step, *, phase, index=None, cleanup=False):
        if not cleanup:
            self.check()
        self.progress(phase=phase)
        guard = self.connection._plan_local.guard
        guard.update(expected_snapshot=self.observation["snapshot"], steps=[step])
        if index is not None and not self.host.store.start_step(self.identifier, index):
            raise _Stop("cancelled", "Cancelled before this order was submitted")
        result = dispatch_step(self.connection, step)
        verified = result.get("status") in {"command_verified", "pause_verified", "order_verified"}
        if index is not None:
            self.host.store.step_result(self.identifier, index, "verified" if verified else "unverified", result)
        if not verified:
            raise _Stop("unverified", "An action was not verified; retain its journal and do not replay it")
        # Preserve the after digest; the next write cannot silently use older context.
        self.observation = {**self.observation, "snapshot": result["snapshot"]}
        return result

    def matching_order(self, step, receipt):
        state = self.observation["state"]
        fleet = state["fleets_by_id"][self.intent["fleet_id"]]
        if fleet.get("orders_known") is not True:
            return False
        before = receipt.get("context", {}).get("objects", {}).get("fleet_id", {})
        if step["kind"] == "survey_planet":
            current, original = _survey_order(fleet), _survey_order(before)
            if current is None or original is None:
                return False
            target = current.get("deposit_holder")
            return (type(original.get("order_id")) is int and type(current.get("order_id")) is int
                    and current["order_id"] == original["order_id"] and current.get("can_reach") == "yes"
                    and target == {"type": 0, "id": int(step["planet_id"])}
                    and original["deposit_holder"] == target
                    and fleet.get("actions", {}) in ({}, [])
                    and all(fleet.get(key, {}) == before.get(key, {}) for key in ("order", "orders", "queued_orders")))
        # Scripted move target stays fixed. Any unfamiliar native sub-layout stops
        # monitoring rather than being guessed to be the user's original order.
        moving = verify_order(state, Order("fleet_move", step["fleet_id"], planet_id=step["planet_id"]))
        remaining_wait = (fleet.get("system_id") == self.intent["system_id"]
                          and verify_order(state, Order("fleet_wait", step["fleet_id"], days=30)))
        return ((moving or remaining_wait)
                and all(fleet.get(key, {}) == before.get(key, {})
                        for key in ("current_order", "order", "orders", "queued_orders")))

    def execute(self):
        status, reason = "unverified", "The runner did not reach a verified terminal state"
        try:
            self.check()
            state, runtime = self.observe()
            self.first_day = self.last_day
            if self.record["expected_snapshot"] is not None and self.record["expected_snapshot"] != self.observation["snapshot"]:
                raise _Stop("stale", "Initial context changed; no order was submitted")
            if runtime["paused"] is not True or runtime["protected_pause"] is not False:
                raise _Stop("blocked", "Begin with an explicitly paused game outside a protected pause")
            self.cleanup_allowed = True
            self.progress(baseline={"date": state["date"], "snapshot": self.observation["snapshot"],
                                    "country_id": self.config.country_id, "campaign_id": self.config.campaign_id,
                                    "session_token": self.config.session_token}, phase="ready")
            evidence = self.goal()
            fleet = state["fleets_by_id"][self.intent["fleet_id"]]
            if not _empty(fleet):
                raise _Stop("blocked", "The fleet has existing or unknown orders; explicitly resolve them first")
            if evidence["value"]:
                for index in range(len(self.record["steps"])):
                    self.host.store.completion_step(self.identifier, index, evidence)
            else:
                self.advance()
            self.progress(goal_state="satisfied")
            status, reason = "completed", "The observed state goal is satisfied; no causal attribution is claimed"
        except _Stop as stop:
            status, reason = stop.status, stop.reason
            if stop.unsafe:
                self.identity_safe = False
        except BaseException:
            status, reason = "unverified", "The runner failed; inspect durable state and journals before retrying"
        finally:
            final = self.final_pause()
            try:
                self.progress(final_pause=final, phase="finished")
                if status == "completed":
                    if final["status"] != "verified":
                        status, reason = "unverified", "The state goal was observed but final pause was not verified"
                    else:
                        evidence = self.goal()
                        if not evidence["value"] or (self.intent["kind"] == "enter_system" and not _empty(
                                self.observation["state"]["fleets_by_id"][self.intent["fleet_id"]])):
                            status, reason = "unverified", "Final paused state no longer proves the requested goal"
            except BaseException:
                status, reason = "unverified", "Final goal evidence or its durable record could not be verified"
        return {"status": status, "reason": reason}

    def advance(self):
        for index, item in enumerate(self.record["steps"]):
            step = item["request"]
            condition = (self.intent if self.intent["kind"] == "enter_system" else {
                "kind": "survey_body", "fleet_id": self.intent["fleet_id"],
                "planet_id": step["planet_id"], "system_id": self.intent["system_id"]})
            self.check()
            self.goal()  # Recheck the entire frozen scope at every dispatch boundary.
            if self.goal(condition)["value"]:
                self.host.store.completion_step(self.identifier, index, self.goal(condition))
                continue
            if not _empty(self.observation["state"]["fleets_by_id"][step["fleet_id"]]):
                raise _Stop("interrupted", "An external or unfinished order prevents the next precise action")
            receipt = self.write(step, phase="dispatching", index=index)
            self.write({"kind": "set_paused", "paused": False}, phase="resuming")
            self.progress(phase="waiting", active_index=index)
            while True:
                self.cancel.wait(min(self.host.poll_seconds, max(0, self.record["time_authorization"]["max_wall_seconds"]
                                                               - (time.monotonic() - self.started))))
                self.check()
                state, runtime = self.observe()
                if runtime["paused"] or runtime["protected_pause"]:
                    raise _Stop("interrupted", "An external or protected pause ended this task; it will not be resumed")
                self.check()
                self.goal()
                evidence = self.goal(condition)
                fleet = state["fleets_by_id"][step["fleet_id"]]
                empty, matching = _empty(fleet), self.matching_order(step, receipt)
                if not empty and not matching:
                    raise _Stop("interrupted", "The tracked order changed or became unobservable; no replacement was sent")
                if evidence["value"]:
                    if self.intent["kind"] == "enter_system":
                        self.write({"kind": "set_paused", "paused": True}, phase="pausing")
                        self.observe()
                        if not self.goal(condition)["value"]:
                            raise _Stop("interrupted", "Fleet left the requested system before the pause was verified")
                        if not _empty(self.observation["state"]["fleets_by_id"][step["fleet_id"]]):
                            if not self.matching_order(step, receipt):
                                raise _Stop("interrupted", "External fleet control changed before stop; no stop was submitted")
                            self.write({"kind": "fleet_stop", "fleet_id": step["fleet_id"]}, phase="stopping_at_entry")
                        self.host.store.completion_step(self.identifier, index, evidence)
                        return
                    # Do not replace a survey still cleaning up its own order.
                    if empty or index == len(self.record["steps"]) - 1:
                        self.host.store.completion_step(self.identifier, index, evidence)
                        self.write({"kind": "set_paused", "paused": True}, phase="pausing")
                        self.observe()
                        break
                elif empty:
                    raise _Stop("interrupted", "The order disappeared without proof that its goal completed")

    def final_pause(self):
        if not self.cleanup_allowed or not self.identity_safe:
            return {"status": "not_attempted", "reason": "No safe live identity is established for cleanup; current pause is unknown"}
        try:
            _, runtime = self.observe()
            if runtime["paused"] is True:
                return {"status": "verified", "already_paused": True, "snapshot": self.observation["snapshot"]}
            self.write({"kind": "set_paused", "paused": True}, phase="final_pausing", cleanup=True)
            _, runtime = self.observe()
            if runtime["paused"] is True:
                return {"status": "verified", "already_paused": False, "snapshot": self.observation["snapshot"]}
        except BaseException:
            pass
        return {"status": "unverified", "reason": "Final pause was not proven; inspect the game and unresolved operation journal"}
