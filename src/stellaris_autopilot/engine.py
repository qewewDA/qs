"""Observe -> authorize -> execute -> observe again -> verify, with durable delivery."""
from __future__ import annotations

import time
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from .config import Config
from .journal import Journal
from .observe import Observation, SaveObserver, fresh
from .predicates import MISSING, evaluate, lookup


def game_date(value: str) -> tuple[int, int, int]:
    try:
        year, month, day = (int(part) for part in value.split("."))
        if year < 0 or not 1 <= month <= 12 or not 1 <= day <= 31:
            raise ValueError
        return year, month, day
    except (ValueError, AttributeError):
        raise ValueError("Unsupported game date; expected YYYY.MM.DD") from None


class Driver(Protocol):
    def execute(self, steps: list[dict]) -> None: ...
    def execute_event(self, event_id: str, country_id: str, console_key: str) -> None: ...


@dataclass(frozen=True)
class Proposal:
    action_id: str | None
    reason: str


def validate_live(config: Config, observation: Observation) -> None:
    state = observation.state
    meta = state.get("metadata", {})
    campaign = meta.get("campaign_id")
    if not isinstance(campaign, str) or not campaign.strip() or campaign != config.campaign_id:
        raise ValueError("Campaign identity missing or not pinned; set campaign_id from an inspected save")
    if meta.get("ironman") in (True, "yes"):
        raise ValueError("Ironman saves cannot use this execution bridge")
    if meta.get("multiplayer") in (True, "yes"):
        raise ValueError("Multiplayer is unsupported")
    if meta.get("ironman") not in (False, "no") and not config.non_ironman_confirmed:
        raise ValueError("Ironman status unknown; confirm non_ironman_confirmed locally")
    if meta.get("multiplayer") not in (False, "no") and not config.single_player_confirmed:
        raise ValueError("Single-player status unknown; confirm single_player_confirmed locally")
    player_ids = meta.get("player_country_ids", [])
    if not player_ids or state["country_id"] not in player_ids:
        raise ValueError("Selected country is not an identified human player empire")
    version = str(meta.get("game_version", ""))
    if (not config.verified_game_version or version.lower() in {"", "unknown", "none"}
            or version != config.verified_game_version):
        raise ValueError(f"Unconfirmed game version {version!r}; calibrate and set verified_game_version")
    if not fresh(observation, config.max_save_age_seconds):
        raise ValueError("Save is stale; create a fresh observation before acting")
    game_date(state["date"])


class Engine:
    def __init__(self, config: Config, journal: Journal, observer: SaveObserver, driver: Driver | None = None):
        self.config, self.journal, self.observer, self.driver = config, journal, observer, driver

    def run(self, proposal: Proposal, observation: Observation, *, live: bool = False) -> dict:
        if not proposal.action_id:
            return {"status": "waiting", "reason": proposal.reason}
        action = self.config.actions.get(proposal.action_id)
        if action is None or action.id not in self.config.allowed_actions:
            return {"status": "blocked", "reason": "Action is not locally authorized"}
        if self.journal.unresolved():
            return {"status": "blocked", "reason": "Unresolved dispatch; inspect and reconcile the journal"}
        missing = [p["path"] for p in action.preconditions
                   if lookup(observation.state, p["path"]) is MISSING]
        # Only locally opted-in exact postconditions may first appear after input.
        # Preconditions and change predicates still require an observed baseline.
        missing.extend(p["path"] for p in action.expected
                       if lookup(observation.state, p["path"]) is MISSING
                       and not (p["op"] == "eq" and p["path"] in action.allow_new_expected_paths))
        if missing:
            return {"status": "blocked", "reason": f"Unobservable predicate paths: {missing}"}
        if not all(evaluate(p, observation.state) for p in action.preconditions):
            return {"status": "blocked", "reason": "Action preconditions do not hold"}
        if all(evaluate(p, observation.state, observation.state) for p in action.expected):
            return {"status": "waiting", "reason": "Desired postcondition already holds"}
        if not live:
            return {"status": "dry_run", "action": action.id, "reason": proposal.reason,
                    "expected": action.expected, "execution": "No game input sent"}
        validate_live(self.config, observation)
        if self.driver is None:
            raise ValueError("Live operation requires a Windows driver")
        if self.config.resolution is None:
            raise ValueError("Live execution requires a calibrated client resolution")
        if (self.config.state_dir / "STOP").exists():
            return {"status": "stopped", "reason": "STOP file exists"}
        current = self.observer.read()
        if current.digest != observation.digest:
            return {"status": "blocked", "reason": "Snapshot changed while planning; replan"}
        # Parsing a large save can take time. Recheck freshness after reading,
        # immediately before reserving and sending the operation.
        validate_live(self.config, current)
        if (self.config.state_dir / "STOP").exists():
            return {"status": "stopped", "reason": "STOP file exists"}
        operation_id = self.journal.claim(observation.digest, action.id, observation.state)
        if operation_id is None:
            return {"status": "blocked", "reason": "Duplicate or unresolved operation"}
        try:
            if action.event:
                self.driver.execute_event(action.event, observation.state["country_id"], self.config.console_key)
            else:
                self.driver.execute(action.steps)
            self.journal.update(operation_id, "dispatched", "Input sent; awaiting a new save")
            if self.config.save_steps:
                self.driver.execute(self.config.save_steps)
            deadline = time.monotonic() + self.config.verification_timeout_seconds
            last_error = "No new matching save before deadline"
            while time.monotonic() < deadline:
                if (self.config.state_dir / "STOP").exists():
                    last_error = "Stopped during verification; input may already have executed"
                    break
                try:
                    after = self.observer.read()
                    if after.digest != observation.digest and after.modified_at >= observation.modified_at:
                        validate_live(self.config, after)
                        if after.state["country_id"] != observation.state["country_id"]:
                            raise ValueError("Player empire changed during verification")
                        if after.state.get("metadata", {}).get("campaign_id") != observation.state.get("metadata", {}).get("campaign_id"):
                            raise ValueError("Campaign changed during verification")
                        if game_date(after.state["date"]) < game_date(observation.state["date"]):
                            raise ValueError("Game date moved backwards; campaign may have changed")
                        if all(evaluate(p, after.state, observation.state) for p in action.expected):
                            self.journal.update(operation_id, "verified", f"Observed postconditions in {after.digest}")
                            return {"status": "verified", "id": operation_id, "action": action.id}
                except (OSError, ValueError) as exc:
                    last_error = str(exc)
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(self.config.poll_seconds, remaining))
            self.journal.update(operation_id, "unverified", last_error)
            return {"status": "unverified", "id": operation_id, "reason": last_error}
        except Exception as exc:
            # Even a driver error may follow partially delivered input: never auto-retry.
            try:
                self.journal.update(operation_id, "unverified", f"Dispatch interrupted: {type(exc).__name__}")
            except (ValueError, sqlite3.Error):
                # A concurrently reconciled/terminal record must not be
                # downgraded, and a secondary journal error must not hide the
                # original failure. Pending/dispatched rows still block retries.
                pass
            raise
