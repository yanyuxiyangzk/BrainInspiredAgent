"""Persistent, bounded FactorDiscoveryLoop profile for v1.5.

L-002 adds the recovery contract from the factor discovery architecture:
facts are committed in one SQLite transaction first, then the checkpoint
pointer is published through a temp-file + fsync + atomic rename write.
Recovery cross-checks stored facts against the checkpoint digest and never
repairs or overwrites facts blindly; mismatches land in REQUIRES_REVIEW
until an explicit reconcile.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from active_agent_platform.storage import SQLiteDatabase, SQLiteTransaction
from brain_kernel.ports import Clock
from domain_sdk.factor_hooks import FactorHookBus, FactorHookEvent

_POINTER_FORMAT = 1


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class FactorLoopStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    TERMINATED = "TERMINATED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


@dataclass(frozen=True, slots=True)
class FactorLoopProfile:
    profile_id: str
    version: str
    interval: timedelta = timedelta(minutes=5)
    max_iterations: int = 100
    max_consecutive_failures: int = 3

    def __post_init__(self) -> None:
        if not self.profile_id or not self.version:
            raise ValueError("factor loop profile identity is required")
        if self.interval <= timedelta(0) or self.max_iterations < 1 or self.max_consecutive_failures < 1:
            raise ValueError("factor loop profile bounds are invalid")


@dataclass(frozen=True, slots=True)
class FactorLoopCheckpoint:
    profile_id: str
    version: str
    iteration: int
    status: FactorLoopStatus
    consecutive_failures: int
    last_completed_at: datetime | None
    next_run_at: datetime
    state_digest: str
    facts_digest: str | None = None


def compute_library_digest(factor_hashes: Iterable[str]) -> str:
    """Stable digest over the full factor library contents."""
    payload = json.dumps(sorted(factor_hashes), separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def compute_facts_digest(
    candidates: Iterable[tuple[str, str]], factor_hashes: Iterable[str]
) -> str:
    """Digest over every tested candidate (hash + algorithm version) and stored factor."""
    state = {
        "candidates": sorted(f"{candidate_hash}|{algorithm}" for candidate_hash, algorithm in candidates),
        "factors": sorted(factor_hashes),
    }
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class FactorDiscoveryLoop:
    """One finite iteration per call; no private event loop or permanent task."""

    def __init__(
        self,
        database: SQLiteDatabase,
        clock: Clock,
        profile: FactorLoopProfile,
        *,
        checkpoint_path: str | Path | None = None,
        hooks: FactorHookBus | None = None,
    ) -> None:
        self._database, self._clock, self.profile = database, clock, profile
        self._checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None
        self._hooks = hooks
        self._search_state: Mapping[str, object] | None = None

    @property
    def search_state(self) -> Mapping[str, object] | None:
        """最近一次写入或从指针恢复的搜索状态（L-004 FactorSearchState 快照）。"""
        return self._search_state

    async def initialize(self) -> FactorLoopCheckpoint:
        row = await self._database.fetch_one(
            "SELECT * FROM discovery_loop_checkpoint WHERE profile_id=? AND version=?",
            (self.profile.profile_id, self.profile.version),
        )
        if row is None:
            now = self._clock.now().astimezone(UTC)
            return await self._commit(0, FactorLoopStatus.RUNNING, 0, None, now,
                                      await self._recompute_facts_digest())
        checkpoint = self._decode(row)
        expected = await self._recompute_facts_digest()
        if checkpoint.facts_digest is None:
            checkpoint = await self._heal_legacy_facts_digest(checkpoint, expected)
        elif checkpoint.facts_digest != expected:
            return await self._mark_review(checkpoint)
        if self._checkpoint_path is not None:
            pointer_state, payload = await asyncio.to_thread(self._read_pointer)
            if pointer_state == "ok" and isinstance(payload.get("search_state"), Mapping):
                self._search_state = cast(Mapping[str, object], payload["search_state"])
            if pointer_state == "corrupt" or (
                pointer_state == "ok" and self._pointer_problem(payload, checkpoint)
            ):
                return await self._mark_review(checkpoint)
        await self._write_pointer(checkpoint)
        return checkpoint

    async def iterate(self, *, success: bool = True) -> FactorLoopCheckpoint:
        current = await self.initialize()
        if current.status is not FactorLoopStatus.RUNNING:
            return current
        if current.iteration >= self.profile.max_iterations:
            return await self._commit(current.iteration, FactorLoopStatus.COMPLETED,
                                      current.consecutive_failures, current.last_completed_at,
                                      current.next_run_at, current.facts_digest)
        now = self._clock.now().astimezone(UTC)
        failures = 0 if success else current.consecutive_failures + 1
        status = FactorLoopStatus.RUNNING
        if not success and failures >= self.profile.max_consecutive_failures:
            status = FactorLoopStatus.REQUIRES_REVIEW
        iteration = current.iteration + 1
        if success and iteration >= self.profile.max_iterations:
            status = FactorLoopStatus.COMPLETED
        checkpoint = await self._commit(iteration, status, failures if not success else 0, now,
                                        now + self.profile.interval, current.facts_digest)
        if self._hooks is not None:
            event = FactorHookEvent.ITERATION_COMPLETED if success else FactorHookEvent.ITERATION_FAILED
            await self._hooks.emit(event, iteration=checkpoint.iteration,
                                   digest=checkpoint.state_digest, details={})
        return checkpoint

    async def pause(self) -> FactorLoopCheckpoint:
        current = await self.initialize()
        if current.status is FactorLoopStatus.RUNNING:
            return await self._commit(current.iteration, FactorLoopStatus.PAUSED,
                                      current.consecutive_failures, current.last_completed_at,
                                      current.next_run_at, current.facts_digest)
        return current

    async def resume(self) -> FactorLoopCheckpoint:
        current = await self.initialize()
        if current.status is FactorLoopStatus.PAUSED:
            return await self._commit(current.iteration, FactorLoopStatus.RUNNING,
                                      current.consecutive_failures, current.last_completed_at,
                                      self._clock.now().astimezone(UTC), current.facts_digest)
        return current

    async def terminate(self) -> FactorLoopCheckpoint:
        current = await self.initialize()
        if current.status in {FactorLoopStatus.RUNNING, FactorLoopStatus.PAUSED}:
            return await self._commit(current.iteration, FactorLoopStatus.TERMINATED,
                                      current.consecutive_failures, current.last_completed_at,
                                      current.next_run_at, current.facts_digest)
        return current

    async def reconcile(self) -> FactorLoopCheckpoint:
        """Explicit recovery decision: the committed facts are authoritative."""
        row = await self._database.fetch_one(
            "SELECT * FROM discovery_loop_checkpoint WHERE profile_id=? AND version=?",
            (self.profile.profile_id, self.profile.version),
        )
        if row is None:
            return await self.initialize()
        current = self._decode(row)
        if current.status is not FactorLoopStatus.REQUIRES_REVIEW:
            return current
        restored = replace(current, status=FactorLoopStatus.RUNNING,
                           facts_digest=await self._recompute_facts_digest())
        await self._persist(restored)
        await self._write_pointer(restored)
        return restored

    @staticmethod
    def candidate_hash(candidate: object, *, algorithm_version: str = "1") -> str:
        """Return a stable hash for a canonical candidate definition."""
        payload = json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256((algorithm_version + "\0" + payload).encode()).hexdigest()

    async def tested_hashes(self, algorithm_version: str | None = None) -> frozenset[str]:
        """Hashes of candidates already committed, optionally scoped to one algorithm version."""
        if algorithm_version is None:
            rows = await self._database.fetch_all("SELECT candidate_hash FROM factor_candidate")
        else:
            rows = await self._database.fetch_all(
                "SELECT candidate_hash FROM factor_candidate WHERE algorithm_version=?",
                (algorithm_version,),
            )
        return frozenset(str(row["candidate_hash"]) for row in rows)

    async def filter_untested(
        self, candidates: Sequence[object], *, algorithm_version: str = "1"
    ) -> list[object]:
        """Drop candidates whose hash was already tested so resume never re-backtests."""
        tested = await self.tested_hashes(algorithm_version)
        return [
            candidate
            for candidate in candidates
            if self.candidate_hash(candidate, algorithm_version=algorithm_version) not in tested
        ]

    async def commit_iteration(self, candidates: Sequence[object] = (), factors: Sequence[object] = (), *,
                               algorithm_version: str = "1", success: bool = True,
                               details: Mapping[str, object] | None = None,
                               search_state: Mapping[str, object] | None = None) -> FactorLoopCheckpoint:
        """Atomically persist new candidates/factors and advance the checkpoint.

        The SQLite transaction commits candidate/factor facts plus the checkpoint
        row (including the facts digest); only after that commit succeeds is the
        file pointer published. A crash in between is healed on the next
        initialize() from the authoritative facts. ``search_state`` (an L-004
        FactorSearchState snapshot) rides the pointer payload for resume; hook
        events are emitted after the commit with idempotent keys.
        """
        current = await self.initialize()
        if current.status is not FactorLoopStatus.RUNNING:
            return current
        if search_state is not None:
            self._search_state = dict(search_state)
        now = self._clock.now().astimezone(UTC)
        candidate_rows = [(self.candidate_hash(c, algorithm_version=algorithm_version),
                           _canonical(c), algorithm_version, now.isoformat()) for c in candidates]
        factor_rows = [(self.candidate_hash(f, algorithm_version=algorithm_version),
                        _canonical(f), now.isoformat()) for f in factors]
        async with self._database.transaction() as tx:
            if candidate_rows:
                await tx.executemany(
                    "INSERT OR IGNORE INTO factor_candidate"
                    " (candidate_hash, candidate_json, algorithm_version, created_at) VALUES (?,?,?,?)",
                    candidate_rows,
                )
            if factor_rows:
                stored = await tx.fetch_all("SELECT factor_hash FROM factor_library")
                merged = sorted({str(row["factor_hash"]) for row in stored} | {row[0] for row in factor_rows})
                library_digest = compute_library_digest(merged)
                await tx.executemany(
                    "INSERT OR IGNORE INTO factor_library"
                    " (factor_hash, factor_json, library_digest, created_at) VALUES (?,?,?,?)",
                    [(digest, payload, library_digest, created) for digest, payload, created in factor_rows],
                )
            facts_digest = await self._recompute_facts_digest(tx)
            failures = 0 if success else current.consecutive_failures + 1
            status = (FactorLoopStatus.REQUIRES_REVIEW
                      if failures >= self.profile.max_consecutive_failures else FactorLoopStatus.RUNNING)
            iteration = current.iteration + 1
            if iteration >= self.profile.max_iterations and status is FactorLoopStatus.RUNNING:
                status = FactorLoopStatus.COMPLETED
            checkpoint = FactorLoopCheckpoint(
                self.profile.profile_id, self.profile.version, iteration, status, failures,
                now, now + self.profile.interval,
                self._state_digest(iteration, status, failures, now, now + self.profile.interval),
                facts_digest,
            )
            await self._persist_in_transaction(tx, checkpoint)
        await self._write_pointer(checkpoint)
        if self._hooks is not None:
            hooks = self._hooks
            await hooks.emit(FactorHookEvent.CHECKPOINT_COMMITTED,
                             iteration=checkpoint.iteration,
                             digest=checkpoint.state_digest, details={})
            iteration_event = (FactorHookEvent.ITERATION_COMPLETED if success
                               else FactorHookEvent.ITERATION_FAILED)
            await hooks.emit(iteration_event, iteration=checkpoint.iteration,
                             digest=checkpoint.facts_digest or checkpoint.state_digest,
                             details=dict(details or {}))
            for factor_hash, _, _ in factor_rows:
                await hooks.emit(FactorHookEvent.FACTOR_ACCEPTED,
                                 iteration=checkpoint.iteration, digest=factor_hash,
                                 details={"factor_hash": factor_hash, **dict(details or {})})
        return checkpoint

    async def _recompute_facts_digest(self, tx: SQLiteTransaction | None = None) -> str:
        source = tx if tx is not None else self._database
        candidates = await source.fetch_all(
            "SELECT candidate_hash, algorithm_version FROM factor_candidate"
        )
        factors = await source.fetch_all("SELECT factor_hash FROM factor_library")
        return compute_facts_digest(
            [(str(row["candidate_hash"]), str(row["algorithm_version"])) for row in candidates],
            [str(row["factor_hash"]) for row in factors],
        )

    async def _heal_legacy_facts_digest(
        self, checkpoint: FactorLoopCheckpoint, expected: str
    ) -> FactorLoopCheckpoint:
        healed = replace(checkpoint, facts_digest=expected)
        await self._persist(healed)
        return healed

    async def _mark_review(self, current: FactorLoopCheckpoint) -> FactorLoopCheckpoint:
        reviewed = replace(current, status=FactorLoopStatus.REQUIRES_REVIEW)
        await self._persist(reviewed)
        return reviewed

    def _pointer_problem(self, payload: dict[object, object], checkpoint: FactorLoopCheckpoint) -> bool:
        if payload.get("format") != _POINTER_FORMAT:
            return True
        if payload.get("profile_id") != self.profile.profile_id or payload.get("version") != self.profile.version:
            return True
        iteration = payload.get("iteration")
        if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 0:
            return True
        if iteration < checkpoint.iteration:
            return False  # crash window after the fact commit, before the pointer write
        if iteration != checkpoint.iteration:
            return True  # pointer claims iterations the database never committed
        if payload.get("state_digest") != checkpoint.state_digest:
            return True
        return payload.get("facts_digest") not in (None, checkpoint.facts_digest)

    def _read_pointer(self) -> tuple[str, dict[object, object]]:
        """Return (missing|ok|corrupt, payload) for the checkpoint pointer file."""
        assert self._checkpoint_path is not None
        try:
            raw = self._checkpoint_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "missing", {}
        except (OSError, UnicodeDecodeError):
            return "corrupt", {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return "corrupt", {}
        if not isinstance(payload, dict):
            return "corrupt", {}
        return "ok", payload

    async def _write_pointer(self, checkpoint: FactorLoopCheckpoint) -> None:
        if self._checkpoint_path is None:
            return
        payload: dict[str, object] = {
            "format": _POINTER_FORMAT,
            "profile_id": checkpoint.profile_id,
            "version": checkpoint.version,
            "iteration": checkpoint.iteration,
            "status": checkpoint.status.value,
            "consecutive_failures": checkpoint.consecutive_failures,
            "last_completed_at": None if checkpoint.last_completed_at is None
            else checkpoint.last_completed_at.isoformat(),
            "next_run_at": checkpoint.next_run_at.isoformat(),
            "state_digest": checkpoint.state_digest,
            "facts_digest": checkpoint.facts_digest,
            "updated_at": self._clock.now().astimezone(UTC).isoformat(),
        }
        if self._search_state is not None:
            payload["search_state"] = self._search_state
        await asyncio.to_thread(self._write_pointer_sync, payload)

    def _write_pointer_sync(self, payload: dict[str, object]) -> None:
        assert self._checkpoint_path is not None
        destination = self._checkpoint_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".checkpoint-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    async def _commit(self, iteration: int, status: FactorLoopStatus, failures: int,
                      completed: datetime | None, next_run: datetime,
                      facts_digest: str | None) -> FactorLoopCheckpoint:
        checkpoint = FactorLoopCheckpoint(
            self.profile.profile_id, self.profile.version, iteration, status, failures,
            completed, next_run,
            self._state_digest(iteration, status, failures, completed, next_run),
            facts_digest,
        )
        await self._persist(checkpoint)
        await self._write_pointer(checkpoint)
        if self._hooks is not None:
            await self._hooks.emit(FactorHookEvent.CHECKPOINT_COMMITTED,
                                   iteration=iteration, digest=checkpoint.state_digest,
                                   details={})
            if status is FactorLoopStatus.REQUIRES_REVIEW:
                await self._hooks.emit(FactorHookEvent.PROFILE_STALLED,
                                       iteration=iteration, digest=checkpoint.state_digest,
                                       details={"consecutive_failures": failures})
        return checkpoint

    def _state_digest(self, iteration: int, status: FactorLoopStatus, failures: int,
                      completed: datetime | None, next_run: datetime) -> str:
        state = {"profile_id": self.profile.profile_id, "version": self.profile.version,
                 "iteration": iteration, "status": status.value,
                 "consecutive_failures": failures,
                 "last_completed_at": None if completed is None else completed.isoformat(),
                 "next_run_at": next_run.isoformat()}
        return "sha256:" + hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

    async def _persist(self, checkpoint: FactorLoopCheckpoint) -> None:
        async with self._database.transaction() as tx:
            await self._persist_in_transaction(tx, checkpoint)

    async def _persist_in_transaction(
        self, tx: SQLiteTransaction, checkpoint: FactorLoopCheckpoint
    ) -> None:
        await tx.execute(
            "INSERT INTO discovery_loop_checkpoint"
            " (profile_id,version,iteration,status,consecutive_failures,last_completed_at,"
            "  next_run_at,state_digest,facts_digest,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(profile_id,version) DO UPDATE SET iteration=excluded.iteration,"
            " status=excluded.status,consecutive_failures=excluded.consecutive_failures,"
            " last_completed_at=excluded.last_completed_at,next_run_at=excluded.next_run_at,"
            " state_digest=excluded.state_digest,facts_digest=excluded.facts_digest,"
            " updated_at=excluded.updated_at",
            (checkpoint.profile_id, checkpoint.version, checkpoint.iteration, checkpoint.status.value,
             checkpoint.consecutive_failures,
             None if checkpoint.last_completed_at is None else checkpoint.last_completed_at.isoformat(),
             checkpoint.next_run_at.isoformat(), checkpoint.state_digest, checkpoint.facts_digest,
             self._clock.now().astimezone(UTC).isoformat()),
        )

    @staticmethod
    def _decode(data: sqlite3.Row) -> FactorLoopCheckpoint:
        return FactorLoopCheckpoint(
            str(data["profile_id"]), str(data["version"]), int(data["iteration"]),
            FactorLoopStatus(str(data["status"])), int(data["consecutive_failures"]),
            None if data["last_completed_at"] is None else datetime.fromisoformat(str(data["last_completed_at"])),
            datetime.fromisoformat(str(data["next_run_at"])), str(data["state_digest"]),
            None if data["facts_digest"] is None else str(data["facts_digest"]),
        )
