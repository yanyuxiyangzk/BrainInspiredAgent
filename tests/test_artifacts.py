from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from active_agent_platform.artifacts import (
    ArtifactError,
    ArtifactPayload,
    LocalArtifactStore,
    StagedArtifact,
)
from active_agent_platform.storage import SQLiteDatabase

NOW = datetime(2026, 8, 18, tzinfo=UTC)


@pytest.mark.asyncio
async def test_stage_is_content_addressed_deduplicated_and_verified(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "objects", inline_limit_bytes=4, max_artifact_bytes=100)
    first = await store.stage(b"hello", media_type="text/plain")
    second = await store.stage(b"hello", media_type="text/plain")
    assert first == second
    assert first.digest == "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert first.uri.endswith(first.digest.removeprefix("sha256:"))
    assert await store.read(first) == b"hello"
    assert len(list(store.root.iterdir())) == 1
    assert (await store.write(b"hello")) == first.uri


@pytest.mark.asyncio
async def test_register_and_get_metadata_are_transactional_and_idempotent(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    store = LocalArtifactStore(tmp_path / "objects", inline_limit_bytes=4, max_artifact_bytes=100)
    staged = await store.stage(b"payload", media_type="application/json")
    async with database.transaction() as transaction:
        first = await store.register(
            transaction, staged, artifact_id="artifact-1", created_at=NOW, correlation_id="corr-1"
        )
        duplicate = await store.register(
            transaction, staged, artifact_id="ignored", created_at=NOW, correlation_id="corr-2"
        )
    assert first == duplicate
    assert (await store.get(database, "artifact-1")) == first
    with pytest.raises(ArtifactError) as error:
        await store.get(database, "missing")
    assert error.value.code == "ARTIFACT_NOT_FOUND"
    await database.close()


@pytest.mark.asyncio
async def test_one_mib_boundary_inlines_and_externalizes(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    store = LocalArtifactStore(
        tmp_path / "objects", inline_limit_bytes=1_048_576, max_artifact_bytes=2_000_000
    )
    async with database.transaction() as transaction:
        inline = await store.store_if_large(
            transaction,
            b"a" * 1_048_576,
            artifact_id="inline",
            media_type="application/octet-stream",
            created_at=NOW,
            correlation_id="corr",
        )
        external = await store.store_if_large(
            transaction,
            b"b" * 1_048_577,
            artifact_id="external",
            media_type="application/octet-stream",
            created_at=NOW,
            correlation_id="corr",
        )
    assert inline == ArtifactPayload(b"a" * 1_048_576, None)
    assert external.inline is None and external.artifact is not None
    assert await store.read(external.artifact) == b"b" * 1_048_577
    assert await database.fetch_one("SELECT * FROM artifact WHERE artifact_id = 'inline'") is None
    await database.close()


@pytest.mark.asyncio
async def test_rollback_leaves_collectable_orphan_but_keeps_registered_blob(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    store = LocalArtifactStore(tmp_path / "objects", inline_limit_bytes=1, max_artifact_bytes=100)
    kept = await store.stage(b"kept")
    orphan = await store.stage(b"orphan")
    async with database.transaction() as transaction:
        await store.register(
            transaction, kept, artifact_id="kept", created_at=NOW, correlation_id="corr"
        )
    orphan_path = store.root / orphan.digest.removeprefix("sha256:")
    kept_path = store.root / kept.digest.removeprefix("sha256:")
    old = (NOW - timedelta(hours=2)).timestamp()
    os.utime(orphan_path, (old, old))
    os.utime(kept_path, (old, old))
    report = await store.collect_orphans(
        database, older_than=timedelta(hours=1), now=NOW
    )
    assert report.examined == 2 and report.deleted == 1 and report.retained == 1
    assert not orphan_path.exists() and kept_path.exists()
    await database.close()


@pytest.mark.asyncio
async def test_recent_orphan_and_unmanaged_files_are_not_deleted(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    store = LocalArtifactStore(tmp_path / "objects", inline_limit_bytes=1, max_artifact_bytes=100)
    staged = await store.stage(b"recent")
    path = store.root / staged.digest.removeprefix("sha256:")
    future = NOW.timestamp()
    os.utime(path, (future, future))
    unmanaged = store.root / "README"
    unmanaged.write_text("keep", encoding="utf-8")
    report = await store.collect_orphans(
        database, older_than=timedelta(hours=1), now=NOW
    )
    assert report == type(report)(1, 0, 1)
    assert path.exists() and unmanaged.exists()
    await database.close()


@pytest.mark.asyncio
async def test_corruption_missing_blob_and_invalid_reference_are_rejected(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "objects", inline_limit_bytes=1, max_artifact_bytes=100)
    staged = await store.stage(b"valid")
    path = store.root / staged.digest.removeprefix("sha256:")
    path.write_bytes(b"broken")
    with pytest.raises(ArtifactError) as error:
        await store.read(staged)
    assert error.value.code == "ARTIFACT_CORRUPT"
    path.unlink()
    with pytest.raises(ArtifactError) as error:
        await store.read(staged)
    assert error.value.code == "ARTIFACT_NOT_FOUND"
    invalid = StagedArtifact("artifact://sha256/" + "a" * 64, "sha256:" + "b" * 64, 1, "x")
    with pytest.raises(ArtifactError) as error:
        await store.read(invalid)
    assert error.value.code == "ARTIFACT_INVALID"


@pytest.mark.asyncio
async def test_limits_and_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ArtifactPayload(None, None)
    with pytest.raises(ValueError):
        LocalArtifactStore(tmp_path / "negative", inline_limit_bytes=-1)
    with pytest.raises(ValueError):
        LocalArtifactStore(tmp_path / "bad", inline_limit_bytes=10, max_artifact_bytes=10)
    store = LocalArtifactStore(tmp_path / "objects", inline_limit_bytes=1, max_artifact_bytes=4)
    with pytest.raises(ArtifactError) as error:
        await store.stage(b"12345")
    assert error.value.code == "ARTIFACT_TOO_LARGE"
    with pytest.raises(ArtifactError):
        await store.stage(b"x", media_type="")
    with pytest.raises(TypeError):
        await store.stage("x")  # type: ignore[arg-type]
    database = SQLiteDatabase(tmp_path / "facts.db")
    await database.initialize()
    staged = await store.stage(b"x")
    async with database.transaction() as transaction:
        with pytest.raises(ArtifactError):
            await store.register(
                transaction,
                staged,
                artifact_id="",
                created_at=NOW,
                correlation_id="corr",
            )
        with pytest.raises(ArtifactError):
            await store.register(
                transaction,
                StagedArtifact(staged.uri, staged.digest, -1, staged.media_type),
                artifact_id="bad-size",
                created_at=NOW,
                correlation_id="corr",
            )
        with pytest.raises(ArtifactError):
            await store.register(
                transaction,
                StagedArtifact(staged.uri, staged.digest, 1, ""),
                artifact_id="bad-media",
                created_at=NOW,
                correlation_id="corr",
            )
    (store.root / staged.digest.removeprefix("sha256:")).unlink()
    async with database.transaction() as transaction:
        with pytest.raises(ArtifactError) as error:
            await store.register(
                transaction,
                staged,
                artifact_id="missing",
                created_at=NOW,
                correlation_id="corr",
            )
        assert error.value.code == "ARTIFACT_NOT_FOUND"
    with pytest.raises(ValueError):
        await store.collect_orphans(database, older_than=timedelta(seconds=-1), now=NOW)
    with pytest.raises(ArtifactError):
        await store.collect_orphans(
            database, older_than=timedelta(), now=NOW.replace(tzinfo=None)
        )
    await database.close()


@pytest.mark.asyncio
async def test_same_size_corruption_and_existing_corrupt_blob_are_detected(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "objects", inline_limit_bytes=1, max_artifact_bytes=100)
    staged = await store.stage(b"valid")
    path = store.root / staged.digest.removeprefix("sha256:")
    path.write_bytes(b"other")
    with pytest.raises(ArtifactError) as error:
        await store.read(staged)
    assert error.value.code == "ARTIFACT_CORRUPT"
    with pytest.raises(ArtifactError) as error:
        await store.stage(b"valid")
    assert error.value.code == "ARTIFACT_CORRUPT"
