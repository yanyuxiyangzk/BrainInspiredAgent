"""Non-destructive filesystem validation for local quant startup."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class StartupPathError(OSError):
    """A stable startup error raised before SQLite creates any files."""


def prepare_runtime_paths(database_path: Path) -> None:
    if database_path.exists() and not database_path.is_file():
        raise StartupPathError(f"database path is not a regular file: {database_path}")
    parent = database_path.parent
    _prepare_directory(parent, "database parent")
    if database_path.exists() and not _has_write_bit(database_path):
        raise StartupPathError(f"database file is not writable: {database_path}")
    artifact_path = parent / f"{database_path.stem}-artifacts"
    if artifact_path.exists() and not artifact_path.is_dir():
        raise StartupPathError(f"artifact path is not a directory: {artifact_path}")
    if artifact_path.exists() and not _has_write_bit(artifact_path):
        raise StartupPathError(f"artifact directory is not writable: {artifact_path}")


def _prepare_directory(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise StartupPathError(f"cannot create {label}: {path}: {error.strerror}") from error
    if not path.is_dir():
        raise StartupPathError(f"{label} is not a directory: {path}")
    if not _has_write_bit(path):
        raise StartupPathError(f"{label} is not writable: {path}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise StartupPathError(f"{label} is not accessible: {path}")


def _has_write_bit(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
