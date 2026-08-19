"""Local content-addressed Artifact Store with transactional metadata registration."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction

_BLOB_NAME = re.compile(r"^[a-f0-9]{64}$")


class ArtifactError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    uri: str
    digest: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    uri: str
    digest: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    inline: bytes | None
    artifact: ArtifactRef | None

    def __post_init__(self) -> None:
        if (self.inline is None) == (self.artifact is None):
            raise ValueError("payload must contain exactly one of inline or artifact")


@dataclass(frozen=True, slots=True)
class GarbageCollectionReport:
    examined: int
    deleted: int
    retained: int


class LocalArtifactStore:
    """Writes immutable blobs first; caller registers metadata in its fact transaction."""

    def __init__(
        self,
        root: str | Path,
        *,
        inline_limit_bytes: int = 1_048_576,
        max_artifact_bytes: int = 104_857_600,
    ) -> None:
        if inline_limit_bytes < 0 or max_artifact_bytes < 1:
            raise ValueError("artifact limits must be non-negative and max must be positive")
        if inline_limit_bytes >= max_artifact_bytes:
            raise ValueError("inline limit must be smaller than artifact limit")
        self._root = Path(root).resolve()
        self._inline_limit = inline_limit_bytes
        self._max_artifact = max_artifact_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    async def write(self, content: bytes) -> str:
        """ArtifactWriter compatibility; returns a URI for later transaction registration."""
        return (await self.stage(content)).uri

    async def stage(
        self, content: bytes, *, media_type: str = "application/octet-stream"
    ) -> StagedArtifact:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        if not media_type or len(media_type) > 255:
            raise ArtifactError("ARTIFACT_INVALID", "media_type must contain 1 to 255 characters")
        if len(content) > self._max_artifact:
            raise ArtifactError("ARTIFACT_TOO_LARGE", "artifact exceeds configured size limit")
        digest_hex = hashlib.sha256(content).hexdigest()
        await asyncio.to_thread(self._write_atomic, digest_hex, content)
        return StagedArtifact(
            f"artifact://sha256/{digest_hex}",
            f"sha256:{digest_hex}",
            len(content),
            media_type,
        )

    async def register(
        self,
        transaction: SQLiteTransaction,
        staged: StagedArtifact,
        *,
        artifact_id: str,
        created_at: datetime,
        correlation_id: str,
    ) -> ArtifactRef:
        self._validate_staged(staged)
        if not artifact_id or not correlation_id:
            raise ArtifactError("ARTIFACT_INVALID", "artifact and correlation IDs are required")
        existing = await transaction.fetch_one(
            "SELECT * FROM artifact WHERE uri = ? AND digest = ?", (staged.uri, staged.digest)
        )
        if existing is not None:
            return _reference(existing)
        timestamp = _timestamp(created_at)
        await transaction.execute(
            """
            INSERT INTO artifact(
                artifact_id, uri, digest, size_bytes, media_type, created_at, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                staged.uri,
                staged.digest,
                staged.size_bytes,
                staged.media_type,
                timestamp,
                correlation_id,
            ),
        )
        return ArtifactRef(
            artifact_id,
            staged.uri,
            staged.digest,
            staged.size_bytes,
            staged.media_type,
        )

    async def store_if_large(
        self,
        transaction: SQLiteTransaction,
        content: bytes,
        *,
        artifact_id: str,
        media_type: str,
        created_at: datetime,
        correlation_id: str,
    ) -> ArtifactPayload:
        if len(content) <= self._inline_limit:
            return ArtifactPayload(bytes(content), None)
        staged = await self.stage(content, media_type=media_type)
        reference = await self.register(
            transaction,
            staged,
            artifact_id=artifact_id,
            created_at=created_at,
            correlation_id=correlation_id,
        )
        return ArtifactPayload(None, reference)

    async def get(self, database: SQLiteDatabase, artifact_id: str) -> ArtifactRef:
        row = await database.fetch_one("SELECT * FROM artifact WHERE artifact_id = ?", (artifact_id,))
        if row is None:
            raise ArtifactError("ARTIFACT_NOT_FOUND", f"artifact not found: {artifact_id}")
        return _reference(row)

    async def read(self, reference: ArtifactRef | StagedArtifact) -> bytes:
        digest_hex = self._digest_from_reference(reference.uri, reference.digest)
        path = self._blob_path(digest_hex)
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as error:
            raise ArtifactError("ARTIFACT_NOT_FOUND", "artifact blob is missing") from error
        if len(content) != reference.size_bytes:
            raise ArtifactError("ARTIFACT_CORRUPT", "artifact size does not match metadata")
        actual = hashlib.sha256(content).hexdigest()
        if actual != digest_hex:
            raise ArtifactError("ARTIFACT_CORRUPT", "artifact digest verification failed")
        return content

    async def collect_orphans(
        self,
        database: SQLiteDatabase,
        *,
        older_than: timedelta,
        now: datetime,
    ) -> GarbageCollectionReport:
        if older_than.total_seconds() < 0:
            raise ValueError("orphan grace period must be non-negative")
        now_timestamp = _aware(now).timestamp()
        referenced_rows = await database.fetch_all("SELECT uri FROM artifact")
        referenced = {
            digest
            for row in referenced_rows
            if (digest := _uri_digest(str(row["uri"]))) is not None
        }
        return await asyncio.to_thread(
            self._collect_sync,
            referenced,
            now_timestamp - older_than.total_seconds(),
        )

    def _write_atomic(self, digest_hex: str, content: bytes) -> None:
        destination = self._blob_path(digest_hex)
        if destination.exists():
            existing = destination.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest_hex:
                raise ArtifactError("ARTIFACT_CORRUPT", "existing content-addressed blob is corrupt")
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=self._root)
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_staged(self, staged: StagedArtifact) -> None:
        digest_hex = self._digest_from_reference(staged.uri, staged.digest)
        if staged.size_bytes < 0 or staged.size_bytes > self._max_artifact:
            raise ArtifactError("ARTIFACT_INVALID", "artifact size is invalid")
        if not staged.media_type:
            raise ArtifactError("ARTIFACT_INVALID", "artifact media type is required")
        if not self._blob_path(digest_hex).is_file():
            raise ArtifactError("ARTIFACT_NOT_FOUND", "staged artifact blob is missing")

    def _digest_from_reference(self, uri: str, digest: str) -> str:
        digest_hex = _uri_digest(uri)
        if digest_hex is None or digest != f"sha256:{digest_hex}":
            raise ArtifactError("ARTIFACT_INVALID", "artifact URI and digest do not match")
        return digest_hex

    def _blob_path(self, digest_hex: str) -> Path:
        if _BLOB_NAME.fullmatch(digest_hex) is None:
            raise ArtifactError("ARTIFACT_INVALID", "invalid artifact digest")
        return self._root / digest_hex

    def _collect_sync(
        self, referenced: set[str], cutoff_timestamp: float
    ) -> GarbageCollectionReport:
        examined = deleted = retained = 0
        for path in self._root.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            is_blob = _BLOB_NAME.fullmatch(path.name) is not None
            is_temporary = path.name.startswith(".tmp-")
            if not is_blob and not is_temporary:
                continue
            examined += 1
            if is_blob and path.name in referenced or path.stat().st_mtime > cutoff_timestamp:
                retained += 1
                continue
            path.unlink(missing_ok=True)
            deleted += 1
        return GarbageCollectionReport(examined, deleted, retained)


def _uri_digest(uri: str) -> str | None:
    prefix = "artifact://sha256/"
    value = uri.removeprefix(prefix)
    return value if uri.startswith(prefix) and _BLOB_NAME.fullmatch(value) is not None else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArtifactError("ARTIFACT_INVALID", "timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")


def _reference(row: object) -> ArtifactRef:
    return ArtifactRef(
        str(row["artifact_id"]),  # type: ignore[index]
        str(row["uri"]),  # type: ignore[index]
        str(row["digest"]),  # type: ignore[index]
        int(row["size_bytes"]),  # type: ignore[index]
        str(row["media_type"]),  # type: ignore[index]
    )
