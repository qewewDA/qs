"""Pinned-build normal command transport for verified Windows games.

Uses the game's normal research command factory, validator and dispatcher.
The model supplies an observed candidate ID, never addresses or JavaScript.
The instrumentation helper is a child process so its failure cannot bypass
the launcher's cleanup. Submission alone never proves gameplay success.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from .owned_process import OwnedGameProcess
from .attached_process import AttachedGameProcess
from .connection_cancel import check_cancelled


PINNED_EXE_SHA256 = "400df27c82ddc845aa9dce79bd468d81f18f060d299cd263e3afa93aef4f7a83"
FRIDA_VERSION = "17.9.4"
_TECH = re.compile(r"tech_[A-Za-z0-9_]{1,120}\Z", re.ASCII)
_TOKEN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
AREAS = frozenset({"physics", "society", "engineering"})


@dataclass(frozen=True)
class ResearchSelection:
    area: str
    technology: str

    @property
    def kind(self) -> str:
        return "select_research"

    def to_dict(self) -> dict:
        return {"area": self.area, "technology": self.technology}


def _object_id(value) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,9}", value, re.ASCII)
            or int(value) >= 4294967295):
        raise ValueError("Object ID must be a canonical ASCII handle")
    return value


def _state_id(value) -> str:
    return _object_id(str(value) if type(value) is int else value)


@dataclass(frozen=True)
class DistrictRequest:
    planet_id: str
    district: str

    @property
    def kind(self) -> str:
        return "queue_district"

    def to_dict(self) -> dict:
        return {"planet_id": self.planet_id, "district": self.district}


@dataclass(frozen=True)
class DistrictConstruction(DistrictRequest):
    queue_id: str
    colony_id: str

    def to_dict(self) -> dict:
        return {**super().to_dict(), "queue_id": self.queue_id, "colony_id": self.colony_id}


@dataclass(frozen=True)
class BuildingRequest:
    planet_id: str
    zone_id: str
    building: str

    @property
    def kind(self) -> str:
        return "queue_building"

    def to_dict(self) -> dict:
        return {"planet_id": self.planet_id, "zone_id": self.zone_id, "building": self.building}


@dataclass(frozen=True)
class BuildingConstruction(BuildingRequest):
    queue_id: str
    colony_id: str

    def to_dict(self) -> dict:
        return {**super().to_dict(), "queue_id": self.queue_id, "colony_id": self.colony_id}


def building_request(planet_id, zone_id, building) -> BuildingRequest:
    planet_id, zone_id = _object_id(planet_id), _object_id(zone_id)
    if not isinstance(building, str) or not re.fullmatch(r"building_[A-Za-z0-9_]{1,120}", building, re.ASCII):
        raise ValueError("Building must be a bounded ASCII building ID")
    return BuildingRequest(planet_id, zone_id, building)


@dataclass(frozen=True)
class PauseRequest:
    paused: bool

    @property
    def kind(self) -> str:
        return "set_paused"

    def to_dict(self) -> dict:
        return {"paused": self.paused}


def pause_request(paused) -> PauseRequest:
    if type(paused) is not bool:
        raise ValueError("paused must be a boolean")
    return PauseRequest(paused)


def district_request(planet_id, district) -> DistrictRequest:
    planet_id = _object_id(planet_id)
    if not isinstance(district, str) or not re.fullmatch(r"district_[A-Za-z0-9_]{1,120}", district, re.ASCII):
        raise ValueError("District must be a bounded ASCII district ID")
    return DistrictRequest(planet_id, district)


def resolve_district(state: dict, request: DistrictRequest) -> DistrictConstruction:
    request = district_request(request.planet_id, request.district)
    if state.get("country_id") != "0":
        raise ValueError("Native construction has only been verified for country 0")
    planet = state.get("planets_by_id", {}).get(request.planet_id)
    construction = state.get("construction", {})
    if (not isinstance(planet, dict) or _state_id(planet.get("owner")) != "0"
            or _state_id(planet.get("controller")) != "0"
            or planet.get("districts_known") is not True or not isinstance(planet.get("district_levels"), dict)):
        raise ValueError("Construction requires an owned, controlled planet with observable district levels")
    queue_id = _state_id(planet.get("build_queue"))
    queue = construction.get("queues_by_id", {}).get(queue_id)
    if (construction.get("known") is not True or not isinstance(queue, dict)
            or queue.get("known") is not True or queue.get("items_known") is not True
            or queue.get("owner") != "0" or queue.get("type") != "planet"
            or queue.get("location") != {"type": 2, "id": request.planet_id}):
        raise ValueError("The exact owned planet construction queue is unobservable")
    return DistrictConstruction(request.planet_id, request.district, queue_id, _state_id(planet.get("colony")))


def resolve_building(state: dict, request: BuildingRequest) -> BuildingConstruction:
    request = building_request(request.planet_id, request.zone_id, request.building)
    # Reuse the exact owned-planet/queue gate, including known district levels.
    planet = state.get("planets_by_id", {}).get(request.planet_id)
    if not isinstance(planet, dict) or planet.get("zones_known") is not True:
        raise ValueError("The owned planet's complete zone references are unobservable")
    zone = planet.get("zones_by_id", {}).get(request.zone_id)
    if (not isinstance(zone, dict) or zone.get("buildings_known") is not True
            or not isinstance(zone.get("buildings_by_id"), dict) or not isinstance(zone.get("district_type"), str)):
        raise ValueError("The exact planet zone and its buildings must be observable")
    queue = resolve_district(state, DistrictRequest(request.planet_id, zone["district_type"]))
    return BuildingConstruction(request.planet_id, request.zone_id, request.building, queue.queue_id, queue.colony_id)


def verify_building(before: dict, after: dict, request: BuildingConstruction) -> bool:
    """Require one new unfinished exact zone job and its actual same-month payment."""
    try:
        if resolve_building(before, request) != request or resolve_building(after, request) != request:
            return False
        old_zone = before["planets_by_id"][request.planet_id]["zones_by_id"][request.zone_id]
        new_zone = after["planets_by_id"][request.planet_id]["zones_by_id"][request.zone_id]
        if old_zone != new_zone:
            return False
        old_ids = set(before["construction"]["items_by_id"])
        items = [item for key, item in after["construction"]["items_by_id"].items()
                 if key not in old_ids and item.get("queue_id") == request.queue_id]
        if len(items) != 1:
            return False
        item = items[0]
        if (item.get("paying_country") != "0" or item.get("buildable_type") != "buildable_planet_building"
                or item.get("buildable") != {"building": request.building, "planet": int(request.colony_id),
                                             "zone": int(request.zone_id)}
                or item.get("target") != {"planet_id": request.planet_id, "colony_id": request.colony_id}):
            return False
        progress, needed = item.get("progress"), item.get("progress_needed")
        if (type(progress) not in (int, float) or type(needed) not in (int, float)
                or not math.isfinite(progress) or not math.isfinite(needed) or not 0 <= progress < needed):
            return False
        if tuple(map(int, before["date"].split(".")[:2])) != tuple(map(int, after["date"].split(".")[:2])):
            return False
        costs = item.get("resources")
        if not isinstance(costs, dict) or not any(type(cost) in (int, float) and cost > 0 for cost in costs.values()):
            return False
        for resource, cost in costs.items():
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                return False
            if cost:
                old, new = before["resources"].get(resource), after["resources"].get(resource)
                if (type(old) not in (int, float) or type(new) not in (int, float)
                        or not math.isfinite(old) or not math.isfinite(new)
                        or not math.isclose(old - new, cost, rel_tol=1e-8, abs_tol=0.0001)):
                    return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def verify_district(before: dict, after: dict, request: DistrictConstruction) -> bool:
    """Require a new pending exact job, unchanged levels and same-month payment."""
    try:
        if resolve_district(before, request) != request:
            return False
        resolved = resolve_district(after, request)
        if resolved != request:
            return False
        old_ids = set(before["construction"]["items_by_id"])
        items = [item for key, item in after["construction"]["items_by_id"].items()
                 if key not in old_ids and item.get("queue_id") == request.queue_id]
        if len(items) != 1:
            return False
        item = items[0]
        if (item.get("paying_country") != "0" or item.get("buildable_type") != "buildable_district"
                or item.get("buildable") != {"district": request.district, "planet": int(request.colony_id)}
                or item.get("target") != {"planet_id": request.planet_id, "colony_id": request.colony_id}):
            return False
        progress, needed = item.get("progress"), item.get("progress_needed")
        if (type(progress) not in (int, float) or type(needed) not in (int, float)
                or not math.isfinite(progress) or not math.isfinite(needed) or not 0 <= progress < needed):
            return False
        if (before["planets_by_id"][request.planet_id]["district_levels"]
                != after["planets_by_id"][request.planet_id]["district_levels"]):
            return False
        if tuple(map(int, before["date"].split(".")[:2])) != tuple(map(int, after["date"].split(".")[:2])):
            return False
        costs = item.get("resources")
        if not isinstance(costs, dict) or not any(type(cost) in (int, float) and cost > 0 for cost in costs.values()):
            return False
        for resource, cost in costs.items():
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                return False
            if cost:
                old, new = before["resources"].get(resource), after["resources"].get(resource)
                if (type(old) not in (int, float) or type(new) not in (int, float)
                        or not math.isfinite(old) or not math.isfinite(new)
                        or not math.isclose(old - new, cost, rel_tol=1e-8, abs_tol=0.0001)):
                    return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def research_selection(area, technology) -> ResearchSelection:
    if not isinstance(area, str) or area not in AREAS:
        raise ValueError("Research area must be physics, society or engineering")
    if not isinstance(technology, str) or not _TECH.fullmatch(technology):
        raise ValueError("Technology must be a bounded ASCII tech ID")
    return ResearchSelection(area, technology)


def validate_research(state: dict, selection: ResearchSelection) -> None:
    selection = research_selection(selection.area, selection.technology)
    if state.get("country_id") != "0":
        raise ValueError("Native research has only been verified for country 0")
    research = state.get("research_status", {})
    area = research.get("areas", {}).get(selection.area, {})
    choices = area.get("alternatives")
    if (area.get("alternatives_known") is not True or not isinstance(choices, list)
            or selection.technology not in choices):
        raise ValueError("Technology must be a visible candidate in the requested area of a fresh snapshot")
    if area.get("auto_researching") is not False:
        raise ValueError("Automatic research must be explicitly off before an exact selection")
    completed = research.get("completed_technologies")
    if research.get("completed_known") is not True or not isinstance(completed, list):
        raise ValueError("Completed technologies are unobservable")
    if selection.technology in completed:
        raise ValueError("Selecting already completed/repeatable technologies is not verified by this backend")


def verify_research(state: dict, selection: ResearchSelection) -> bool:
    try:
        research = state["research_status"]
        area = research["areas"][selection.area]
        items = area.get("queue")
        return (research.get("completed_known") is True
                and selection.technology not in research["completed_technologies"]
                and area.get("current_known") is True and area.get("queue_known") is True
                and area.get("current") == selection.technology
                and isinstance(items, list) and len(items) == 1
                and items[0].get("technology") == selection.technology)
    except (KeyError, TypeError, AttributeError):
        return False


class NativeResearchBridge:
    """A private, serialized worker for an owned or independently verified attached game."""

    def __init__(self, process: subprocess.Popen | OwnedGameProcess | AttachedGameProcess,
                 executable: Path, state_dir: Path, *, initialization_deadline: float | None = None,
                 cancel_event: threading.Event | None = None):
        self._deadline = initialization_deadline
        self._cancel_event = cancel_event
        self._remaining_timeout()
        if (sys.platform != "win32"
                or not isinstance(process, (subprocess.Popen, OwnedGameProcess, AttachedGameProcess))
                or process.poll() is not None):
            raise ValueError("Native research requires a live verified Windows game")
        if isinstance(process, (OwnedGameProcess, AttachedGameProcess)) and process.executable != Path(executable).resolve():
            raise ValueError("The owned native game executable identity does not match")
        if hashlib.sha256(Path(executable).read_bytes()).hexdigest() != PINNED_EXE_SHA256:
            raise ValueError("Unsupported executable hash; native calls are disabled")
        self.game = process
        self.lock = threading.Lock()
        self.request_id = 0
        self.poisoned = False
        self.wakeup_confirmed = False
        self.last_runtime = None
        self.messages = queue.Queue()
        state_dir.mkdir(parents=True, exist_ok=True)
        self.event_log = state_dir / "native-events.jsonl"
        self.error_log = (state_dir / "native-worker-stderr.log").open("w", encoding="utf-8")
        try:
            self._remaining_timeout()
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "stellaris_autopilot.native_worker", str(process.pid), str(executable)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.error_log,
                text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except BaseException:
            self.error_log.close()
            raise
        def pump():
            for line in self.process.stdout:
                self.messages.put(line)
            self.messages.put(None)
        threading.Thread(target=pump, daemon=True).start()
        try:
            ready = self._read()
            if ready != {"ready": True, "version": FRIDA_VERSION}:
                raise ValueError("Native worker did not validate its instrumentation version")
        except BaseException:
            self.close()
            raise

    def _remaining_timeout(self) -> float:
        check_cancelled(getattr(self, "_cancel_event", None), "native_rpc")
        deadline = getattr(self, "_deadline", None)
        seconds = 25 if deadline is None else min(25, deadline - time.monotonic())
        if seconds <= 0:
            raise ValueError("Connection deadline exceeded before native RPC; no further request was sent")
        return seconds

    def finish_initialization(self) -> None:
        """Clear the connection budget only after the verified runtime is ready."""
        self._remaining_timeout()
        self._deadline = None
        self._cancel_event = None

    def _read(self) -> dict:
        try:
            timeout = self._remaining_timeout()
            event = getattr(self, "_cancel_event", None)
            if event is None:
                message = self.messages.get(timeout=timeout)
            else:
                until = time.monotonic() + timeout
                while True:
                    self._remaining_timeout()
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise queue.Empty
                    try:
                        message = self.messages.get(timeout=min(remaining, 0.1))
                        break
                    except queue.Empty:
                        if time.monotonic() >= until:
                            raise
        except queue.Empty:
            raise ValueError("Native worker timed out; the operation may be unresolved") from None
        check_cancelled(event, "native_rpc")
        if message is None:
            raise ValueError("Native worker exited; inspect its local log")
        reply = json.loads(message)
        if "error" in reply:
            raise ValueError("Native worker rejected the request; inspect its local log")
        return reply

    def _call(self, method: str, *arguments) -> dict:
        with self.lock:
            if self.poisoned or self.game.poll() is not None or self.process.poll() is not None:
                raise ValueError("The owned game or native worker exited")
            self.request_id += 1
            expected = self.request_id
            try:
                self._remaining_timeout()
                self.process.stdin.write(json.dumps({"request_id": expected, "method": method, "arguments": arguments}) + "\n")
                self.process.stdin.flush()
                reply = self._read()
                result = reply.get("result")
                token = arguments[0]["token"] if method == "enqueue" else arguments[0]
                if (type(reply.get("request_id")) is not int or reply["request_id"] != expected
                        or not isinstance(result, dict) or result.get("token") != token):
                    raise ValueError("Native RPC correlation failed; refusing further requests")
                event_log = getattr(self, "event_log", None)
                if event_log is not None:
                    with event_log.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({"request_id": expected, "method": method, "result": result}) + "\n")
                if result.get("wake_posted") is False:
                    self.wakeup_confirmed = False
                    self.last_runtime = None
                if result.get("execution_source") == "window_message":
                    self.wakeup_confirmed = True
                    runtime = result.get("runtime")
                    if (isinstance(runtime, dict) and type(runtime.get("paused")) is bool
                            and type(runtime.get("protected_pause")) is bool):
                        self.last_runtime = dict(runtime)
                return result
            except BaseException:
                self.poisoned = True
                self.close()
                raise

    def submit(self, selection: ResearchSelection | DistrictConstruction | BuildingConstruction | PauseRequest, token: str) -> dict:
        from .extended_native import RESOLVED_TYPES, extended_payload
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid native operation token")
        if isinstance(selection, RESOLVED_TYPES):
            return self._call("enqueue", extended_payload(selection, token))
        if isinstance(selection, ResearchSelection):
            selection = research_selection(selection.area, selection.technology)
        elif isinstance(selection, DistrictConstruction):
            district_request(selection.planet_id, selection.district)
            _object_id(selection.queue_id)
            _object_id(selection.colony_id)
        elif isinstance(selection, BuildingConstruction):
            building_request(selection.planet_id, selection.zone_id, selection.building)
            _object_id(selection.queue_id)
            _object_id(selection.colony_id)
        elif isinstance(selection, PauseRequest):
            pause_request(selection.paused)
        else:
            raise ValueError("Unsupported native action")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid native operation token")
        return self._call("enqueue", {"kind": selection.kind, "token": token, "country_id": "0",
                                      **selection.to_dict()})

    def result(self, token: str) -> dict:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid native operation token")
        return self._call("result", token)

    def cancel(self, token: str) -> dict:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid native operation token")
        return self._call("cancel", token)

    def request_dump(self, token: str) -> dict:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid native observation token")
        return self._call("enqueue", {"kind": "request_observation", "country_id": "0", "token": token})

    def bind_existing_window(self, token: str) -> dict:
        if not isinstance(self.game, AttachedGameProcess):
            raise ValueError("Window discovery is only for a verified attached game")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid attachment token")
        return self._call("bind_window", token)

    def load_attached_events(self, token: str) -> dict:
        if not isinstance(self.game, AttachedGameProcess):
            raise ValueError("Runtime event loading is only for a verified attached game")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid attachment token")
        return self._call("enqueue", {"kind": "load_attached_events", "country_id": "0", "token": token})

    def run_inbox(self, token: str) -> dict:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Invalid native inbox token")
        return self._call("enqueue", {"kind": "run_inbox", "country_id": "0", "token": token})

    @property
    def supports_wakeup(self) -> bool:
        return (self.wakeup_confirmed and not self.poisoned
                and self.game.poll() is None and self.process.poll() is None)

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.stdin.close()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    self.process.wait(timeout=10)
        finally:
            self.error_log.close()
