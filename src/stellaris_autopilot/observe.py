"""Stable, bounded observations; saves are read-only and never uploaded raw."""
from __future__ import annotations

import hashlib
import copy
import os
import stat
import sys
from functools import lru_cache
import time
from dataclasses import dataclass
from pathlib import Path

from .saves import SaveError, read_save


@lru_cache(maxsize=1)
def _windows_file_info_reader():
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    reader = kernel.GetFileInformationByHandleEx
    reader.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    reader.restype = ctypes.c_int
    return reader


def _windows_change_time(stream) -> int:
    """Windows stat().st_ctime is creation time; read the real change time."""
    import ctypes
    import msvcrt

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation_time", ctypes.c_int64), ("last_access_time", ctypes.c_int64),
            ("last_write_time", ctypes.c_int64), ("change_time", ctypes.c_int64),
            ("attributes", ctypes.c_uint32),
        ]

    info = FileBasicInfo()
    handle = msvcrt.get_osfhandle(stream.fileno())
    if not _windows_file_info_reader()(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise SaveError("Cannot inspect the save file change time")
    return info.change_time


@dataclass(frozen=True)
class Observation:
    state: dict
    digest: str
    modified_at: float


class SaveObserver:
    def __init__(self, path: Path, country_id: str | None = None, *, max_bytes: int = 128 * 1024 * 1024):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.path, self.country_id, self.max_bytes = Path(path), country_id, max_bytes
        self._cached_signature: tuple | None = None
        self._cached: Observation | None = None

    @staticmethod
    def _signature(info: os.stat_result) -> tuple:
        # ctime also catches in-place writes whose author restores mtime. The
        # device differentiates inode numbers on separate mounted volumes.
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _generation(self, info: os.stat_result, stream) -> tuple:
        signature = self._signature(info)
        return (*signature, _windows_change_time(stream)) if sys.platform == "win32" else signature

    def read(self) -> Observation:
        # A save may be replaced while the game writes it. Reject mixed reads.
        initial = self.path.stat()
        if not stat.S_ISREG(initial.st_mode):
            raise SaveError("Save must be a regular file")
        if initial.st_size > self.max_bytes:
            raise SaveError(f"Save exceeds the {self.max_bytes}-byte input limit")
        with self.path.open("rb") as probe:
            before = os.fstat(probe.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SaveError("Save must be a regular file")
            if before.st_size > self.max_bytes:
                raise SaveError(f"Save exceeds the {self.max_bytes}-byte input limit")
            before_generation = self._generation(before, probe)
        signature = (*before_generation, self.country_id)
        if signature == self._cached_signature and self._cached is not None:
            return Observation(copy.deepcopy(self._cached.state), self._cached.digest, self._cached.modified_at)
        state = read_save(self.path, country_id=self.country_id, max_bytes=self.max_bytes)
        digest = hashlib.sha256()
        with self.path.open("rb") as stream:
            if self._generation(os.fstat(stream.fileno()), stream) != before_generation:
                raise SaveError("Save changed during observation; wait for the next stable save")
            remaining = self.max_bytes + 1
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if remaining == 0:
                raise SaveError(f"Save exceeds the {self.max_bytes}-byte input limit")
            handle_after = self._generation(os.fstat(stream.fileno()), stream)
        with self.path.open("rb") as probe:
            after = os.fstat(probe.fileno())
            after_generation = self._generation(after, probe)
        if before_generation != after_generation or before_generation != handle_after:
            raise SaveError("Save changed during observation; wait for the next stable save")
        observation = Observation(state, digest.hexdigest(), after.st_mtime)
        # Keep a separate normalized state: callers may edit their returned
        # dictionaries without contaminating the next observation of the file.
        self._cached_signature = signature
        self._cached = Observation(copy.deepcopy(state), observation.digest, observation.modified_at)
        return observation


class LogTail:
    """Only new complete SAA lines; handles truncation, rotation and partial writes."""
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            info = self.path.stat()
        except FileNotFoundError:
            info = None
        self.offset = info.st_size if info else 0
        self.identity = (info.st_dev, info.st_ino) if info else None
        self.pending = b""

    def read(self) -> list[str]:
        try:
            with self.path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                identity = (info.st_dev, info.st_ino)
                if identity != self.identity or info.st_size < self.offset:
                    self.offset, self.pending = 0, b""
                self.identity = identity
                stream.seek(self.offset)
                data = stream.read(1024 * 1024)
                self.offset = stream.tell()
        except FileNotFoundError:
            return []
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()[-8192:]
        return [line.decode("utf-8", errors="replace") for line in lines if b"SAA|" in line or b"saa_DEFEND_" in line]


def fresh(observation: Observation, max_age: float) -> bool:
    age = time.time() - observation.modified_at
    return -5 <= age <= max_age
