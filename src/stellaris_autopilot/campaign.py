"""Persistent, immutable campaign checkpoints and exclusive local sessions.

A parsed checkpoint is not proof that the game can reload it. That separate
claim is recorded only after a new process observes the same campaign and date.
Unresolved gameplay operations are never replayed by this module.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid
import zipfile

from .direct_observe import DirectObserver
from .engine import game_date
from .observe import Observation
from .pdx import PDXNode, loads
from .saves import SaveError, normalize_gamestate


MAX_BYTES = 128 * 1024 * 1024
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_COUNTRY = re.compile(r"[0-9]{1,10}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class CampaignError(ValueError):
    """The persistent campaign cannot safely advance to another lifecycle step."""


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CampaignError("Repeated campaign manifest field")
        result[key] = value
    return result


def _regular(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise CampaignError("Campaign input must be an ordinary file")


def _atomic(path: Path, data: bytes) -> None:
    """Publish complete bytes in the same directory; retain old bytes on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Windows does not support fsync on directory handles. The complete
        # file is flushed before replacement on every supported platform.
        if sys.platform != "win32":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _single(node: PDXNode, key: str):
    values = node.get_all(key)
    if len(values) != 1:
        raise CampaignError(f"Campaign identity field {key!r} is missing or repeated")
    return values[0]


def _decode(data: bytes, path: Path, country_id: str) -> tuple[PDXNode, dict]:
    try:
        root = loads(data.decode("utf-8-sig"))
        for key in ("version", "date", "galaxy", "country"):
            _single(root, key)
        galaxy = _single(root, "galaxy")
        countries = _single(root, "country")
        if not isinstance(galaxy, PDXNode) or not isinstance(countries, PDXNode):
            raise CampaignError("Campaign identity containers are malformed")
        campaign_id = _single(galaxy, "name")
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise CampaignError("Campaign identity is unknown")
        if not isinstance(_single(countries, country_id), PDXNode):
            raise CampaignError("Selected country is absent or malformed")
        state = normalize_gamestate(root, path, country_id=country_id)
        game_date(state["date"])
        metadata = state["metadata"]
        if metadata["player_country_ids"] != [country_id]:
            raise CampaignError("Exactly the selected human country must be identified")
        if metadata["ironman"] in (True, "yes") or metadata["multiplayer"] in (True, "yes"):
            raise CampaignError("Persistent control requires non-Ironman single-player")
        if metadata["game_version"] in (None, "", "unknown"):
            raise CampaignError("Game version is unknown")
        return root, state
    except (UnicodeError, SaveError) as exc:
        raise CampaignError("Cannot parse the campaign checkpoint") from exc


def _gamestate(path: Path) -> bytes:
    """Copy a stable regular file, then parse ZIP members from immutable bytes."""
    import io

    _regular(path)
    data, _, _ = DirectObserver(path, max_bytes=MAX_BYTES)._copy_snapshot()
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            matches = [member for member in archive.infolist() if member.filename == "gamestate"]
            if len(matches) != 1 or matches[0].file_size > MAX_BYTES:
                raise CampaignError("Checkpoint must contain one bounded gamestate")
            with archive.open(matches[0]) as stream:
                result = stream.read(MAX_BYTES + 1)
            if len(result) > MAX_BYTES:
                raise CampaignError("Checkpoint gamestate exceeds the input limit")
            return result
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise CampaignError("Cannot read checkpoint ZIP") from exc


def _pdx(value) -> str:
    if isinstance(value, PDXNode):
        return "{\n" + "\n".join(
            ((key if re.fullmatch(r"[A-Za-z0-9_.-]+", key) else json.dumps(key, ensure_ascii=False))
             + "=" if key is not None else "") + _pdx(item)
            for key, item in value.entries
        ) + "\n}"
    if type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            raise CampaignError("Non-finite checkpoint metadata")
        return str(value)
    if not isinstance(value, str):
        raise CampaignError("Unsupported checkpoint metadata")
    return json.dumps(value, ensure_ascii=False)


def _browser_metadata(root: PDXNode, country_id: str) -> dict:
    """Recover mandatory save-browser data omitted by debug_dumpgamestate.

    The game requires player_portrait and flag in ZIP meta even though a dump
    omits both at root level. Resolve the selected country's actual founder
    species and flag; never invent an unrelated portrait or flag.
    """
    country = _single(_single(root, "country"), country_id)
    result = {}
    for key in ("player_portrait", "flag"):
        fields = root.get_all(key)
        if len(fields) > 1:
            raise CampaignError("Ambiguous checkpoint save-browser metadata")
        if fields:
            result[key] = fields[0]
    if "flag" not in result:
        result["flag"] = _single(country, "flag")
    if not isinstance(result["flag"], PDXNode):
        raise CampaignError("Selected country flag is unknown")
    if "player_portrait" not in result:
        founder = _single(country, "founder_species_ref")
        species_db = _single(root, "species_db")
        if type(founder) is not int or not isinstance(species_db, PDXNode):
            raise CampaignError("Selected country founder species is unknown")
        species = _single(species_db, str(founder))
        if not isinstance(species, PDXNode):
            raise CampaignError("Selected country founder species is malformed")
        result["player_portrait"] = _single(species, "portrait")
    if not isinstance(result["player_portrait"], str) or not result["player_portrait"].strip():
        raise CampaignError("Selected country portrait is unknown")
    return result


def gameplay_state(state: dict) -> dict:
    """Comparable observed gameplay; bootstrap receipts and source paths differ."""
    result = {key: value for key, value in state.items() if key != "direct_bridge"}
    result["metadata"] = {key: value for key, value in result["metadata"].items() if key != "source"}
    return result


def imported_queue_initialization(expected: dict, observed: dict) -> dict | None:
    """Recognize only the measured 4.5.2 first-load planet queue caches.

    Original date-one saves can omit every uncolonized body's queue reference.
    Postload creates those references and exposes empty queues for existing
    owned orbital stations. These contain no jobs. All actual gameplay fields,
    existing queues and owned colony queue references must still be equal.
    Callers restrict this exception to an imported_save checkpoint.
    """
    before, after = copy.deepcopy(gameplay_state(expected)), copy.deepcopy(gameplay_state(observed))
    if before == after:
        return {"uncolonized_body_queues": [], "selected_empty_queue_ids": []}
    try:
        old_bodies = before["space"]["planets_by_id"]
        new_bodies = after["space"]["planets_by_id"]
        own_planets = before["planets_by_id"]
        country = before["country_id"]
        old_construction, new_construction = before["construction"], after["construction"]
        old_queues, new_queues = old_construction["queues_by_id"], new_construction["queues_by_id"]
        if (not all(type(value) is dict for value in (old_bodies, new_bodies, own_planets, old_queues, new_queues))
                or set(old_bodies) != set(new_bodies) or not set(old_queues) <= set(new_queues)):
            return None
        references = {}
        changes = []
        old_references = {body.get("build_queue") for body in old_bodies.values() if isinstance(body, dict)}
        for body_id, old in old_bodies.items():
            new = new_bodies[body_id]
            if old == new:
                continue
            queue_id = new.get("build_queue")
            if (body_id in own_planets or "owner" in old or "colony" in old or "build_queue" in old
                    or not isinstance(queue_id, str) or not re.fullmatch(r"0|[1-9][0-9]{0,9}", queue_id)
                    or int(queue_id) >= 4294967295 or queue_id in references or queue_id in old_queues
                    or queue_id in old_references):
                return None
            candidate = dict(new)
            del candidate["build_queue"]
            if candidate != old:
                return None
            references[queue_id] = body_id
            changes.append({"planet_id": body_id, "queue_id": queue_id})
            new_bodies[body_id] = candidate
        added = set(new_queues) - set(old_queues)
        for queue_id in added:
            body_id = references.get(queue_id)
            body = old_bodies.get(body_id, {})
            queue = new_queues[queue_id]
            expected_queue = {"id": queue_id, "owner": country, "type": "planet",
                              "location": {"type": 2, "id": body_id}, "simultaneous": 1,
                              "disabled": None, "known": True, "items_known": True,
                              "item_count": 0, "items": [], "order_known": True}
            if (body_id is None or body.get("controller") != country
                    or not isinstance(body.get("shipclass_orbital_station"), str)
                    or any(queue.get(key) is not True for key in ("known", "items_known", "order_known"))
                    or any(type(queue.get(key)) is not int for key in ("simultaneous", "item_count"))
                    or type(queue.get("location", {}).get("type")) is not int
                    or queue != expected_queue):
                return None
        # Check list/map/count agreement before filtering any initialization.
        for construction in (old_construction, new_construction):
            queues = construction["queues"]
            mapping = construction["queues_by_id"]
            if (not isinstance(queues, list) or any(not isinstance(queue, dict) for queue in queues)
                    or len({queue["id"] for queue in queues}) != len(queues)
                    or {queue["id"]: queue for queue in queues} != mapping
                    or type(construction["queue_count"]) is not int or construction["queue_count"] != len(queues)
                    or any(construction.get(key) is not True for key in ("known", "queues_known", "items_known"))):
                return None
        new_construction["queues"] = [queue for queue in new_construction["queues"] if queue["id"] not in added]
        for queue_id in added:
            del new_queues[queue_id]
        new_construction["queue_count"] -= len(added)
        if before != after:
            return None
        return {"uncolonized_body_queues": sorted(changes, key=lambda item: int(item["planet_id"])),
                "selected_empty_queue_ids": sorted(added, key=int)}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def package_dump(dump_path: Path, destination: Path, country_id: str, *, expected_digest: str | None = None) -> dict:
    """Package one exact dump as ZIP meta+gamestate without modifying dump bytes.

    The destination must be a new local path chosen by the launcher. This only
    proves byte integrity and parseability; a real reload needs separate proof.
    """
    dump_path, destination = Path(dump_path), Path(destination)
    _regular(dump_path)
    data, _, _ = DirectObserver(dump_path, country_id)._copy_snapshot()
    digest = hashlib.sha256(data).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise CampaignError("The dump changed after the verified observation")
    root, state = _decode(data, dump_path, country_id)
    browser = _browser_metadata(root, country_id)
    if destination.exists():
        raise CampaignError("A checkpoint destination may never be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        # These are the observed save-browser metadata fields. Values come
        # exclusively from this snapshot; no obsolete date from an older save.
        metadata = []
        for key in ("version", "version_control_revision", "name", "date", "required_dlcs",
                    "player_portrait", "flag", "meta_fleets", "meta_planets"):
            fields = root.get_all(key)
            if len(fields) > 1:
                raise CampaignError("Ambiguous checkpoint metadata")
            if key in browser:
                metadata.append(key + "=" + _pdx(browser[key]))
            elif fields:
                metadata.append(key + "=" + _pdx(fields[0]))
        with temporary.open("xb") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("gamestate", data)
                archive.writestr("meta", "\n".join(metadata) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        check = _gamestate(temporary)
        # These exact bytes were already parsed above. Byte equality proves
        # that decompression yields that same parsed state without reparsing
        # a potentially large galaxy for a second time.
        if check != data:
            raise CampaignError("The packaged checkpoint did not verify")
        # link() atomically creates a new name and refuses an existing target.
        # The temporary file and destination are on the same local filesystem.
        os.link(temporary, destination)
        return {"date": state["date"], "campaign_id": state["metadata"]["campaign_id"],
                "country_id": country_id, "game_version": state["metadata"]["game_version"],
                "gamestate_sha256": digest, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "bytes": destination.stat().st_size, "parse_verified": True, "reload_verified": False}
    finally:
        temporary.unlink(missing_ok=True)


class CampaignStore:
    """A local campaign directory held under a crash-releasing operating-system lock."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if self.root.exists() and self.root.is_symlink():
            raise CampaignError("Campaign directory cannot be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self._locked = False
        self._latest_identity_cache = None

    def _path(self, relative: str) -> Path:
        path = self.root / relative
        if not isinstance(relative, str) or Path(relative).is_absolute() or path.resolve().parent == path.resolve():
            raise CampaignError("Invalid campaign-owned relative path")
        if not path.resolve().is_relative_to(self.root):
            raise CampaignError("Campaign path escapes its owned directory")
        for part in (path, *path.parents):
            if part == self.root:
                break
            if part.is_symlink():
                raise CampaignError("Campaign-owned paths cannot be symbolic links")
        return path

    @contextmanager
    def locked(self):
        """Serialize the whole launcher lifetime; process death releases the lock."""
        if self._locked:
            raise CampaignError("Campaign lock is already held by this controller")
        path = self._path("controller.lock")
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CampaignError("Another controller holds the persistent campaign lock") from None
            self._locked = True
            try:
                yield self
            finally:
                self._locked = False
                stream.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _require_lock(self):
        if not self._locked:
            raise CampaignError("Acquire the campaign lock before mutation")

    def _write(self, manifest: dict):
        self._require_lock()
        data = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        _atomic(self._path("manifest.json"), data)

    def load(self) -> dict:
        path = self._path("manifest.json")
        _regular(path)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise CampaignError("Campaign manifest exceeds the input limit")
        try:
            result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CampaignError("Campaign manifest is unreadable") from exc
        if (not isinstance(result, dict) or result.get("schema") != 1
                or not isinstance(result.get("checkpoints"), list) or not result["checkpoints"]
                or not isinstance(result.get("sessions"), list)):
            raise CampaignError("Unsupported campaign manifest")
        return result

    def initialize(self, source: Path, *, country_id: str, single_player_confirmed: bool,
                   non_ironman_confirmed: bool) -> dict:
        """Import once. An existing campaign is never reset from the source."""
        self._require_lock()
        if self._path("manifest.json").exists() or self._path("original.sav").exists():
            raise CampaignError("Campaign already exists or initial import was interrupted")
        if not isinstance(country_id, str) or not _COUNTRY.fullmatch(country_id):
            raise CampaignError("country_id must be an explicit decimal identifier")
        if single_player_confirmed is not True or non_ironman_confirmed is not True:
            raise CampaignError("Single-player and non-Ironman must be locally confirmed")
        source = Path(source).resolve()
        _regular(source)
        data, _, _ = DirectObserver(source)._copy_snapshot()
        # Snapshot original bytes before reading inside a ZIP to avoid a
        # different generation being retained from the one whose identity passed.
        original = self._path("original.sav")
        _atomic(original, data)
        try:
            gamestate = _gamestate(original)
            _, state = _decode(gamestate, original, country_id)
        except BaseException:
            original.unlink(missing_ok=True)
            raise
        digest = hashlib.sha256(data).hexdigest()
        seed = {"id": uuid.uuid4().hex, "path": "original.sav", "kind": "imported_save",
                "session_token": None, "date": state["date"], "sha256": digest,
                "gamestate_sha256": hashlib.sha256(gamestate).hexdigest(), "bytes": len(data),
                "parse_verified": True, "reload_verified": False, "created_at": time.time()}
        manifest = {"schema": 1, "campaign_id": state["metadata"]["campaign_id"],
                    "country_id": country_id, "game_version": state["metadata"]["game_version"],
                    "source": {"path": str(source), "sha256": digest},
                    "single_player_confirmed": True, "non_ironman_confirmed": True,
                    "latest_checkpoint": seed["id"], "checkpoints": [seed], "sessions": [],
                    "created_at": time.time()}
        self._write(manifest)
        return manifest

    def _verify_identity(self, manifest: dict, state: dict):
        metadata = state["metadata"]
        if (state["country_id"] != manifest["country_id"]
                or metadata["player_country_ids"] != [manifest["country_id"]]
                or metadata["campaign_id"] != manifest["campaign_id"]
                or metadata["game_version"] != manifest["game_version"]):
            raise CampaignError("Checkpoint campaign, country or game build does not match")

    def latest(self) -> tuple[Path, dict]:
        manifest = self.load()
        selected = [item for item in manifest["checkpoints"] if item.get("id") == manifest["latest_checkpoint"]]
        if len(selected) != 1:
            raise CampaignError("Latest checkpoint is missing or ambiguous")
        checkpoint = selected[0]
        path = self._path(checkpoint["path"])
        _regular(path)
        if path.stat().st_size > MAX_BYTES:
            raise CampaignError("Checkpoint exceeds the input limit")
        if not _HASH.fullmatch(checkpoint.get("sha256", "")):
            raise CampaignError("Invalid checkpoint digest")
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if file_digest != checkpoint["sha256"]:
            raise CampaignError("Latest checkpoint bytes changed")
        cache_key = (str(path), file_digest, checkpoint.get("gamestate_sha256"), checkpoint.get("date"),
                     manifest.get("campaign_id"), manifest.get("country_id"), manifest.get("game_version"))
        if self._latest_identity_cache == cache_key:
            return path, checkpoint
        data = _gamestate(path)
        _, state = _decode(data, path, manifest["country_id"])
        self._verify_identity(manifest, state)
        if (state["date"] != checkpoint["date"]
                or hashlib.sha256(data).hexdigest() != checkpoint["gamestate_sha256"]):
            raise CampaignError("Latest checkpoint content changed")
        self._latest_identity_cache = cache_key
        return path, checkpoint

    def unresolved(self) -> list[dict]:
        """Inspect existing journals read-only; never create or replay operations."""
        result = []
        for session in self.load()["sessions"]:
            token = session.get("token")
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise CampaignError("Malformed persistent session token")
            journal = self._path(f"sessions/{token}/journal.sqlite")
            if not journal.exists():
                continue
            _regular(journal)
            try:
                with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as database:
                    rows = database.execute("SELECT id,action,status FROM operations "
                                            "WHERE status IN ('pending','dispatched','unverified') ORDER BY id")
                    result.extend({"session_token": token, "id": row[0], "action": row[1], "status": row[2]}
                                  for row in rows)
            except sqlite3.DatabaseError as exc:
                raise CampaignError("Cannot inspect an earlier operation journal") from exc
        return result

    def begin_session(self, *, recover_last_checkpoint: bool = False) -> dict:
        self._require_lock()
        manifest = self.load()
        self.latest()
        if self.unresolved():
            raise CampaignError("Earlier commands remain unresolved; inspect them without replay")
        interrupted = [item for item in manifest["sessions"] if item.get("status") not in {"closed", "interrupted"}]
        if interrupted and not recover_last_checkpoint:
            raise CampaignError("Interrupted session: explicitly recover the last checkpoint before launching")
        for session in interrupted:
            session["status"] = "interrupted"
        token = uuid.uuid4().hex
        session = {"token": token, "status": "starting", "started_at": time.time(),
                   "loaded_checkpoint": manifest["latest_checkpoint"], "checkpoint": None,
                   "recovered_interrupted_session": bool(interrupted)}
        manifest["sessions"].append(session)
        self._path(f"sessions/{token}").mkdir(parents=True, exist_ok=False)
        self._write(manifest)
        return session

    def update_session(self, token: str, *, status: str, owned_pid: int | None = None, error: str | None = None):
        self._require_lock()
        if status not in {"connected", "save_failed", "interrupted"}:
            raise CampaignError("Unsupported session lifecycle status")
        manifest = self.load()
        session = self._session(manifest, token)
        if session["status"] == "closed":
            raise CampaignError("A closed session cannot be reopened")
        session["status"] = status
        if owned_pid is not None:
            if type(owned_pid) is not int or owned_pid <= 0:
                raise CampaignError("Invalid owned process identifier")
            session["owned_pid"] = owned_pid
        if error is not None:
            session["error"] = str(error)[:1000]
        self._write(manifest)

    @staticmethod
    def _session(manifest, token):
        found = [item for item in manifest["sessions"] if item.get("token") == token]
        if len(found) != 1 or not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise CampaignError("Unknown persistent session")
        return found[0]

    def checkpoint_dump(self, dump_path: Path, *, session_token: str, expected_digest: str,
                        expected_date: str) -> dict:
        self._require_lock()
        manifest = self.load()
        session = self._session(manifest, session_token)
        if session["status"] not in {"connected", "checkpointed"}:
            raise CampaignError("Only a connected session can publish a checkpoint")
        if self.unresolved():
            raise CampaignError("Cannot checkpoint with unresolved gameplay operations")
        _, previous = self.latest()
        if game_date(expected_date) < game_date(previous["date"]):
            raise CampaignError("A checkpoint cannot silently rewind the campaign date")
        if not isinstance(expected_digest, str) or not _HASH.fullmatch(expected_digest):
            raise CampaignError("Checkpoint requires the verified observation digest")
        identifier = uuid.uuid4().hex
        relative = f"checkpoints/{identifier}.sav"
        destination = self._path(relative)
        checkpoint = package_dump(dump_path, destination, manifest["country_id"], expected_digest=expected_digest)
        # A failed identity gate leaves an unreferenced immutable file. It is
        # never adopted automatically during recovery or used by the launcher.
        if (checkpoint["campaign_id"] != manifest["campaign_id"]
                or checkpoint["game_version"] != manifest["game_version"]
                or checkpoint["date"] != expected_date):
            raise CampaignError("Fresh checkpoint identity/date does not match this campaign")
        checkpoint.update({"id": identifier, "path": relative, "kind": "packaged_dump",
                           "session_token": session_token, "created_at": time.time()})
        manifest["checkpoints"].append(checkpoint)
        manifest["latest_checkpoint"] = identifier
        session["checkpoint"] = identifier
        session["status"] = "checkpointed"
        self._write(manifest)
        return checkpoint

    def verify_reload(self, checkpoint_id: str, observation: Observation) -> dict:
        """Verify observed gameplay after a distinct session reloads a checkpoint.

        Bootstrap changes bytes (session flags), so identical dump hashes are
        not required. The new session must prove its unique bootstrap flag in
        DirectConnection before supplying this observation.
        """
        self._require_lock()
        manifest = self.load()
        found = [item for item in manifest["checkpoints"] if item["id"] == checkpoint_id]
        if len(found) != 1:
            raise CampaignError("Unknown checkpoint to verify")
        checkpoint = found[0]
        current = manifest["sessions"][-1] if manifest["sessions"] else None
        if (current is None or current["status"] not in {"starting", "connected"}
                or current["loaded_checkpoint"] != checkpoint_id
                or current["token"] == checkpoint.get("session_token")):
            raise CampaignError("Reload proof requires a distinct active loading session")
        self._verify_identity(manifest, observation.state)
        if (observation.state["date"] != checkpoint["date"]
                or observation.modified_at < current["started_at"]
                or "qs_direct_session_" + current["token"] not in observation.state.get("direct_bridge", {}).get("session_flags", [])):
            raise CampaignError("Reload observation has no fresh matching date/session")
        path, latest = self.latest()
        if latest["id"] != checkpoint_id:
            raise CampaignError("Reload must confirm the current checkpoint")
        _, expected = _decode(_gamestate(path), path, manifest["country_id"])
        initialization = None
        if gameplay_state(expected) != gameplay_state(observation.state):
            if checkpoint.get("kind") == "imported_save" and manifest["game_version"] == "Cygnus v4.5.2":
                initialization = imported_queue_initialization(expected, observation.state)
            if initialization is None:
                raise CampaignError("Reloaded gameplay state differs from the current checkpoint")
        gameplay_digest = hashlib.sha256(json.dumps(
            gameplay_state(observation.state), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        checkpoint.update({"reload_verified": True, "reload_verified_at": time.time(),
                           "reload_session_token": current["token"], "reload_snapshot": observation.digest,
                           "reload_gameplay_sha256": gameplay_digest,
                           "reload_initialization": initialization})
        self._write(manifest)
        return checkpoint

    def finish_session(self, token: str) -> dict:
        """Mark saved/closed only after the launcher actually closes its owned handle."""
        self._require_lock()
        manifest = self.load()
        session = self._session(manifest, token)
        _, latest = self.latest()
        if (self.unresolved() or session["status"] != "checkpointed"
                or latest.get("session_token") != token or session["checkpoint"] != latest["id"]):
            raise CampaignError("Session cannot close without its own verified checkpoint")
        session["status"] = "closed"
        session["closed_at"] = time.time()
        self._write(manifest)
        return session

    def status(self) -> dict:
        manifest = self.load()
        _, latest = self.latest()
        return {"campaign_id": manifest["campaign_id"], "country_id": manifest["country_id"],
                "game_version": manifest["game_version"], "latest_checkpoint": latest,
                "session_count": len(manifest["sessions"]),
                "current_session": manifest["sessions"][-1] if manifest["sessions"] else None,
                "unresolved": self.unresolved(), "automatic_command_replay": False}
