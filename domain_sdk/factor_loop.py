"""Persistent, bounded FactorDiscoveryLoop profile for v1.5."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from active_agent_platform.storage import SQLiteDatabase
from brain_kernel.ports import Clock


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


class FactorDiscoveryLoop:
    """One finite iteration per call; no private event loop or permanent task."""

    def __init__(self, database: SQLiteDatabase, clock: Clock, profile: FactorLoopProfile) -> None:
        self._database, self._clock, self.profile = database, clock, profile

    async def initialize(self) -> FactorLoopCheckpoint:
        row = await self._database.fetch_one(
            "SELECT * FROM discovery_loop_checkpoint WHERE profile_id=? AND version=?",
            (self.profile.profile_id, self.profile.version),
        )
        if row is not None:
            return self._decode(row)
        now = self._clock.now().astimezone(UTC)
        return await self._commit(0, FactorLoopStatus.RUNNING, 0, None, now)

    async def iterate(self, *, success: bool = True) -> FactorLoopCheckpoint:
        current = await self.initialize()
        if current.status is not FactorLoopStatus.RUNNING:
            return current
        if current.iteration >= self.profile.max_iterations:
            return await self._commit(current.iteration, FactorLoopStatus.COMPLETED,
                                      current.consecutive_failures, current.last_completed_at,
                                      current.next_run_at)
        now = self._clock.now().astimezone(UTC)
        failures = 0 if success else current.consecutive_failures + 1
        status = FactorLoopStatus.RUNNING
        if not success and failures >= self.profile.max_consecutive_failures:
            status = FactorLoopStatus.REQUIRES_REVIEW
        iteration = current.iteration + 1
        if success and iteration >= self.profile.max_iterations:
            status = FactorLoopStatus.COMPLETED
        return await self._commit(iteration, status, failures if not success else 0, now,
                                  now + self.profile.interval)

    async def pause(self) -> FactorLoopCheckpoint:
        current = await self.initialize()
        if current.status is FactorLoopStatus.RUNNING:
            return await self._commit(current.iteration, FactorLoopStatus.PAUSED,
                                      current.consecutive_failures, current.last_completed_at,
                                      current.next_run_at)
        return current

    async def resume(self) -> FactorLoopCheckpoint:
        current = await self.initialize()
        if current.status is FactorLoopStatus.PAUSED:
            return await self._commit(current.iteration, FactorLoopStatus.RUNNING,
                                      current.consecutive_failures, current.last_completed_at,
                                      self._clock.now().astimezone(UTC))
        return current

    async def terminate(self) -> FactorLoopCheckpoint:
        current = await self.initialize()
        if current.status in {FactorLoopStatus.RUNNING, FactorLoopStatus.PAUSED}:
            return await self._commit(current.iteration, FactorLoopStatus.TERMINATED,
                                      current.consecutive_failures, current.last_completed_at,
                                      current.next_run_at)
        return current

    async def _commit(self, iteration: int, status: FactorLoopStatus, failures: int,
                      completed: datetime | None, next_run: datetime) -> FactorLoopCheckpoint:
        state = {"profile_id": self.profile.profile_id, "version": self.profile.version,
                 "iteration": iteration, "status": status.value,
                 "consecutive_failures": failures,
                 "last_completed_at": None if completed is None else completed.isoformat(),
                 "next_run_at": next_run.isoformat()}
        digest = "sha256:" + hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        async with self._database.transaction() as transaction:
            await transaction.execute(
                "INSERT INTO discovery_loop_checkpoint VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,version) DO UPDATE SET iteration=excluded.iteration,"
                "status=excluded.status,consecutive_failures=excluded.consecutive_failures,"
                "last_completed_at=excluded.last_completed_at,next_run_at=excluded.next_run_at,"
                "state_digest=excluded.state_digest,updated_at=excluded.updated_at",
                (self.profile.profile_id, self.profile.version, iteration, status.value, failures,
                 None if completed is None else completed.isoformat(), next_run.isoformat(),
                 digest, self._clock.now().astimezone(UTC).isoformat()),
            )
        return FactorLoopCheckpoint(self.profile.profile_id, self.profile.version, iteration,
                                     status, failures, completed, next_run, digest)

    @staticmethod
    def _decode(data: sqlite3.Row) -> FactorLoopCheckpoint:
        return FactorLoopCheckpoint(
            str(data["profile_id"]), str(data["version"]), int(data["iteration"]),
            FactorLoopStatus(str(data["status"])), int(data["consecutive_failures"]),
            None if data["last_completed_at"] is None else datetime.fromisoformat(str(data["last_completed_at"])),
            datetime.fromisoformat(str(data["next_run_at"])), str(data["state_digest"]),
        )
