"""E02 dataset construction service: fitness observations → replay dataset.

Wraps :class:`ExperienceDatasetBuilder` with the quant defaults: an ACTIVE
baseline discovery, temporal 60/20/20 splitting, and a manifest summary for
the CLI. Datasets are the fuel for sandbox replay (E01) and therefore for
every governed candidate comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from active_agent_platform.foundation import SystemClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.experience_dataset import (
    ExperienceDatasetBuilder,
    ExperienceDatasetSpec,
)


class DatasetServiceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DatasetBuildResult:
    dataset_id: str
    version: str
    manifest_digest: str
    window_id: str
    sample_count: int
    train_count: int
    validation_count: int
    test_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id, "version": self.version,
            "manifest_digest": self.manifest_digest, "window_id": self.window_id,
            "sample_count": self.sample_count, "train": self.train_count,
            "validation": self.validation_count, "test": self.test_count,
        }


async def build_experience_dataset(
    database: SQLiteDatabase, *, dataset_id: str, window_id: str,
    baseline_content_digest: str, candidate_content_digests: tuple[str, ...],
    starts_at: datetime, ends_at: datetime, version: str = "1.0.0",
    builder_version: str = "dataset-service/1.0", minimum_samples_per_dna: int = 1,
) -> DatasetBuildResult:
    """Build (or idempotently reload) a replay dataset from fitness observations.

    Splits the window temporally into 60% train, 20% validation and 20%
    test. Sandbox replay never sees the training split.
    """
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise DatasetServiceError("dataset window bounds must be timezone-aware")
    if not candidate_content_digests:
        raise DatasetServiceError(
            "at least one candidate content digest is required; seed a shadow "
            "variant first (see evolution_seed.seed_market_days)",
        )
    span = ends_at - starts_at
    spec = ExperienceDatasetSpec(
        dataset_id=dataset_id, version=version, builder_version=builder_version,
        window_id=window_id, starts_at=starts_at,
        train_until=starts_at + span * 60 // 100,
        validation_until=starts_at + span * 80 // 100, ends_at=ends_at,
        baseline_content_digest=baseline_content_digest,
        candidate_content_digests=candidate_content_digests,
        minimum_samples_per_dna=minimum_samples_per_dna,
    )
    builder = ExperienceDatasetBuilder(database, SystemClock())
    dataset = await builder.build(spec)
    manifest = dataset.manifest
    return DatasetBuildResult(
        dataset_id=manifest.dataset_id, version=manifest.version,
        manifest_digest=manifest.manifest_digest, window_id=spec.window_id,
        sample_count=manifest.sample_count, train_count=manifest.train_count,
        validation_count=manifest.validation_count, test_count=manifest.test_count,
    )


def split_window(starts_at: datetime, ends_at: datetime) -> tuple[datetime, datetime]:
    """60/20/20 temporal split boundaries used by :func:`build_experience_dataset`."""
    span = ends_at - starts_at
    return (starts_at + span * 60 // 100, starts_at + span * 80 // 100)
