"""Immutable, attributable and replayable experience datasets for DNA evolution."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock


class ExperienceDatasetError(ValueError):
    pass


class DatasetSplit(StrEnum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    TEST = "TEST"


class DatasetCohort(StrEnum):
    BASELINE = "BASELINE"
    CANDIDATE = "CANDIDATE"


@dataclass(frozen=True, slots=True)
class ExperienceDatasetSpec:
    dataset_id: str
    version: str
    builder_version: str
    window_id: str
    starts_at: datetime
    train_until: datetime
    validation_until: datetime
    ends_at: datetime
    baseline_content_digest: str
    candidate_content_digests: tuple[str, ...]
    minimum_samples_per_dna: int = 1

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", self.dataset_id) is None:
            raise ExperienceDatasetError("dataset_id is invalid")
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version) is None:
            raise ExperienceDatasetError("dataset version is invalid")
        if not self.builder_version or not self.window_id:
            raise ExperienceDatasetError("dataset builder and window must not be empty")
        boundaries = tuple(map(_utc, (self.starts_at, self.train_until,
                                      self.validation_until, self.ends_at)))
        if not boundaries[0] < boundaries[1] < boundaries[2] < boundaries[3]:
            raise ExperienceDatasetError("dataset temporal split boundaries are invalid")
        if not self.baseline_content_digest.startswith("sha256:"):
            raise ExperienceDatasetError("baseline content digest is invalid")
        if (not self.candidate_content_digests
                or len(set(self.candidate_content_digests)) != len(self.candidate_content_digests)
                or any(not item.startswith("sha256:")
                       for item in self.candidate_content_digests)
                or self.baseline_content_digest in self.candidate_content_digests):
            raise ExperienceDatasetError("candidate content digests are invalid")
        if self.minimum_samples_per_dna < 1:
            raise ExperienceDatasetError("minimum_samples_per_dna must be positive")

    def to_document(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id, "version": self.version,
            "builder_version": self.builder_version, "window_id": self.window_id,
            "starts_at": _time(self.starts_at), "train_until": _time(self.train_until),
            "validation_until": _time(self.validation_until), "ends_at": _time(self.ends_at),
            "baseline_content_digest": self.baseline_content_digest,
            "candidate_content_digests": list(self.candidate_content_digests),
            "minimum_samples_per_dna": self.minimum_samples_per_dna,
        }


@dataclass(frozen=True, slots=True)
class ExperienceSample:
    sample_id: str
    ordinal: int
    split: DatasetSplit
    cohort: DatasetCohort
    dna_id: str
    dna_version: str
    content_digest: str
    evaluation_id: str
    observation_id: str
    observed_at: datetime
    sample_digest: str
    document: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ExperienceDatasetManifest:
    dataset_id: str
    version: str
    builder_version: str
    manifest_digest: str
    sample_count: int
    train_count: int
    validation_count: int
    test_count: int
    created_at: datetime
    sealed_at: datetime


@dataclass(frozen=True, slots=True)
class ExperienceDataset:
    manifest: ExperienceDatasetManifest
    samples: tuple[ExperienceSample, ...]


class ExperienceDatasetBuilder:
    def __init__(self, database: SQLiteDatabase, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def build(self, spec: ExperienceDatasetSpec) -> ExperienceDataset:
        spec_json = _json(spec.to_document())
        async with self._database.transaction() as transaction:
            existing = await transaction.fetch_one(
                "SELECT spec_json FROM dna_experience_dataset WHERE dataset_id=? AND version=?",
                (spec.dataset_id, spec.version),
            )
            if existing is not None:
                if str(existing["spec_json"]) != spec_json:
                    raise ExperienceDatasetError("dataset version already exists with another spec")
                return await self._load(transaction, spec.dataset_id, spec.version)
            rows = await self._source_rows(transaction, spec)
            counts = Counter(str(row["content_digest"]) for row in rows)
            required = (spec.baseline_content_digest, *spec.candidate_content_digests)
            missing = [digest for digest in required
                       if counts[digest] < spec.minimum_samples_per_dna]
            if missing:
                raise ExperienceDatasetError("dataset has insufficient samples for every DNA")
            built: list[ExperienceSample] = []
            for ordinal, row in enumerate(rows):
                built.append(await self._sample(transaction, spec, row, ordinal))
            samples = tuple(built)
            sample_digests = [item.sample_digest for item in samples]
            manifest_digest = _digest({"spec": spec.to_document(),
                                       "sample_digests": sample_digests})
            now = _utc(self._clock.now())
            split_counts = Counter(item.split for item in samples)
            await transaction.execute(
                "INSERT INTO dna_experience_dataset VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (spec.dataset_id, spec.version, spec.builder_version, spec_json,
                 manifest_digest, len(samples), split_counts[DatasetSplit.TRAIN],
                 split_counts[DatasetSplit.VALIDATION], split_counts[DatasetSplit.TEST],
                 _time(now), _time(now)),
            )
            await transaction.executemany(
                "INSERT INTO dna_experience_sample VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(_sample_values(spec, item) for item in samples),
            )
            return await self._load(transaction, spec.dataset_id, spec.version)

    async def get(self, dataset_id: str, version: str) -> ExperienceDataset:
        async with self._database.transaction() as transaction:
            return await self._load(transaction, dataset_id, version)

    async def replay(self, dataset_id: str, version: str) -> ExperienceDataset:
        dataset = await self.get(dataset_id, version)
        for sample in dataset.samples:
            if _digest(sample.document) != sample.sample_digest:
                raise ExperienceDatasetError(f"experience sample digest mismatch: {sample.sample_id}")
        manifest = _digest({
            "spec": await self._spec_document(dataset_id, version),
            "sample_digests": [item.sample_digest for item in dataset.samples],
        })
        if manifest != dataset.manifest.manifest_digest:
            raise ExperienceDatasetError("experience dataset manifest digest mismatch")
        return dataset

    async def _source_rows(
        self, transaction: SQLiteTransaction, spec: ExperienceDatasetSpec,
    ) -> list[sqlite3.Row]:
        digests = (spec.baseline_content_digest, *spec.candidate_content_digests)
        placeholders = ",".join("?" for _ in digests)
        return await transaction.fetch_all(
            f"""SELECT observation.*,outcome.evaluation_json,episode.episode_json
                FROM dna_fitness_observation AS observation
                JOIN outcome_evaluation AS outcome
                  ON outcome.evaluation_id=observation.evaluation_id
                JOIN episode ON episode.episode_id=outcome.episode_id
                WHERE observation.window_id=?
                  AND observation.observed_at>=? AND observation.observed_at<?
                  AND observation.content_digest IN ({placeholders})
                ORDER BY observation.observed_at,observation.evaluation_id""",
            (spec.window_id, _time(spec.starts_at), _time(spec.ends_at), *digests),
        )

    async def _sample(
        self, transaction: SQLiteTransaction, spec: ExperienceDatasetSpec,
        row: sqlite3.Row, ordinal: int,
    ) -> ExperienceSample:
        observed_at = _parse_time(str(row["observed_at"]))
        split = _split(observed_at, spec)
        cohort = (DatasetCohort.BASELINE
                  if str(row["content_digest"]) == spec.baseline_content_digest
                  else DatasetCohort.CANDIDATE)
        trace = await _trace(transaction, str(row["correlation_id"]))
        outcome = json.loads(str(row["evaluation_json"]))
        document: dict[str, object] = {
            "schema_version": "1.0",
            "dataset": {"dataset_id": spec.dataset_id, "version": spec.version,
                        "builder_version": spec.builder_version},
            "dna": {"dna_id": str(row["dna_id"]), "version": str(row["version"]),
                    "content_digest": str(row["content_digest"]), "cohort": cohort.value},
            "split": split.value, "observed_at": _time(observed_at),
            "sources": {"observation_id": str(row["observation_id"]),
                        "observation_digest": str(row["payload_digest"]),
                        "evaluation_id": str(row["evaluation_id"]),
                        "episode_id": outcome.get("episode_id"),
                        "correlation_id": str(row["correlation_id"])},
            "fitness": {"successful": bool(row["successful"]),
                        "evidence_score": float(row["evidence_score"]),
                        "user_value_score": float(row["user_value_score"]),
                        "cost_minor": int(row["cost_minor"]),
                        "latency_ms": int(row["latency_ms"]),
                        "stable": bool(row["stable"]),
                        "risk_violations": json.loads(str(row["risk_violations_json"]))},
            "outcome": outcome, "episode": json.loads(str(row["episode_json"])),
            "trace": trace,
        }
        digest = _digest(document)
        sample_id = _digest({"dataset_id": spec.dataset_id, "version": spec.version,
                             "evaluation_id": str(row["evaluation_id"])})
        return ExperienceSample(
            sample_id, ordinal, split, cohort, str(row["dna_id"]), str(row["version"]),
            str(row["content_digest"]), str(row["evaluation_id"]),
            str(row["observation_id"]), observed_at, digest, document,
        )

    async def _load(
        self, transaction: SQLiteTransaction, dataset_id: str, version: str,
    ) -> ExperienceDataset:
        row = await transaction.fetch_one(
            "SELECT * FROM dna_experience_dataset WHERE dataset_id=? AND version=?",
            (dataset_id, version),
        )
        if row is None:
            raise ExperienceDatasetError(f"experience dataset not found: {dataset_id}@{version}")
        samples = await transaction.fetch_all(
            """SELECT * FROM dna_experience_sample
               WHERE dataset_id=? AND dataset_version=? ORDER BY ordinal""",
            (dataset_id, version),
        )
        manifest = ExperienceDatasetManifest(
            dataset_id, version, str(row["builder_version"]), str(row["manifest_digest"]),
            int(row["sample_count"]), int(row["train_count"]),
            int(row["validation_count"]), int(row["test_count"]),
            _parse_time(str(row["created_at"])), _parse_time(str(row["sealed_at"])),
        )
        return ExperienceDataset(manifest, tuple(_sample_from_row(item) for item in samples))

    async def _spec_document(self, dataset_id: str, version: str) -> Mapping[str, object]:
        row = await self._database.fetch_one(
            "SELECT spec_json FROM dna_experience_dataset WHERE dataset_id=? AND version=?",
            (dataset_id, version),
        )
        if row is None:
            raise ExperienceDatasetError(f"experience dataset not found: {dataset_id}@{version}")
        return cast(Mapping[str, object], json.loads(str(row["spec_json"])))


async def _trace(transaction: SQLiteTransaction, correlation_id: str) -> dict[str, object]:
    documents: dict[str, object] = {}
    for name, table, column in (
        ("plans", "plan", "plan_json"), ("decisions", "plan_decision", "decision_json"),
        ("grants", "execution_grant", "grant_json"),
        ("audits", "audit_record", "record_json"),
    ):
        rows = await transaction.fetch_all(
            f"SELECT {column} FROM {table} WHERE correlation_id=? ORDER BY rowid",
            (correlation_id,),
        )
        documents[name] = [json.loads(str(row[column])) for row in rows]
    for name, table in (("tasks", "task"), ("workflow_runs", "workflow_run"),
                        ("node_runs", "node_run")):
        rows = await transaction.fetch_all(
            f"SELECT * FROM {table} WHERE correlation_id=? ORDER BY rowid", (correlation_id,)
        )
        documents[name] = [dict(row) for row in rows]
    return documents


def _sample_values(
    spec: ExperienceDatasetSpec, sample: ExperienceSample,
) -> tuple[str | int, ...]:
    return (
        spec.dataset_id, spec.version, sample.sample_id, sample.ordinal, sample.split.value,
        sample.cohort.value, sample.dna_id, sample.dna_version, sample.content_digest,
        sample.evaluation_id, sample.observation_id, _time(sample.observed_at),
        sample.sample_digest, _json(sample.document),
    )


def _sample_from_row(row: sqlite3.Row) -> ExperienceSample:
    return ExperienceSample(
        str(row["sample_id"]), int(row["ordinal"]), DatasetSplit(str(row["split"])),
        DatasetCohort(str(row["cohort"])), str(row["dna_id"]), str(row["dna_version"]),
        str(row["content_digest"]), str(row["evaluation_id"]), str(row["observation_id"]),
        _parse_time(str(row["observed_at"])), str(row["sample_digest"]),
        json.loads(str(row["document_json"])),
    )


def _split(value: datetime, spec: ExperienceDatasetSpec) -> DatasetSplit:
    if value < _utc(spec.train_until):
        return DatasetSplit.TRAIN
    if value < _utc(spec.validation_until):
        return DatasetSplit.VALIDATION
    return DatasetSplit.TEST


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExperienceDatasetError("dataset time must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
