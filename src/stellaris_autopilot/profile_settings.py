"""Seed campaign profile files from the user's normal Stellaris profile.

The campaign launcher must never invent display settings. Existing files are
kept byte-for-byte; missing files are copied from the user's own profile.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from shutil import copyfileobj


PROFILE_FILES = ("settings.txt", "pdx_settings.txt")
_IS_WINDOWS = sys.platform == "win32"


def _documents_directory() -> Path | None:
    """Return the Windows Documents known folder, or None when unavailable."""
    if not _IS_WINDOWS:
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer)
    if result != 0 or not buffer.value:
        return None
    return Path(buffer.value)


def default_source_user_dir() -> Path | None:
    """Return the normal Stellaris user directory for this Windows user."""
    documents = _documents_directory()
    return None if documents is None else documents / "Paradox Interactive" / "Stellaris"


def _copy_new_file(source: Path, target: Path) -> None:
    """Copy exact bytes to a new target; never replace an existing target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with target.open("xb") as output:
            created = True
            with source.open("rb") as input_file:
                copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def seed_profile_settings(user_dir: Path, *, source_user_dir: Path | None = None) -> list[Path]:
    """Copy missing campaign profile files from the user's normal profile.

    Only settings.txt and pdx_settings.txt are considered. Existing
    destination files are never opened or modified. Missing source files are
    ignored, so the game can create its own defaults.
    """
    user_dir = Path(user_dir)
    source = Path(source_user_dir) if source_user_dir is not None else default_source_user_dir()
    copied: list[Path] = []
    if source is None:
        return copied
    for name in PROFILE_FILES:
        target = user_dir / name
        if target.exists():
            continue
        source_file = source / name
        if not source_file.is_file():
            continue
        _copy_new_file(source_file, target)
        copied.append(target)
    return copied
