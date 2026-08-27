"""Governed, domain-neutral DNA self-evolution orchestration."""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Mapping
from typing import Protocol
from active_agent_platform.storage import SQLiteDatabase
from brain_kernel.ports import Clock

class EvolutionStage(StrEnum):
    CANDIDATE="CANDIDATE"; VALIDATED="VALIDATED"; SHADOW="SHADOW"; CANARY="CANARY"; ACTIVE="ACTIVE"; ROLLED_BACK="ROLLED_BACK"
@dataclass(frozen=True, slots=True)
class DnaCandidate:
    dna_id: str; version: str; document: Mapping[str, object]; parent_digest: str | None = None; stage: EvolutionStage = EvolutionStage.CANDIDATE
    @property
    def content_digest(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(self.document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
@dataclass(frozen=True, slots=True)
class ReplayResult:
    candidate_digest: str; passed: bool; score: float; evidence: Mapping[str, object]
class CandidateGenerator(Protocol):
    async def generate_candidate(self, context: Mapping[str, object]) -> Mapping[str, object]: ...
class DnaEvolutionRuntime:
    def __init__(self, database: SQLiteDatabase | None = None, clock: Clock | None = None) -> None: self.candidates={}; self.replays={}; self.audit=[]; self.database=database; self.clock=clock
    async def propose(self, generator: CandidateGenerator, *, dna_id: str, version: str, context: Mapping[str, object], parent_digest: str | None = None) -> DnaCandidate:
        document=await generator.generate_candidate(context)
        if not isinstance(document, Mapping) or not document: raise ValueError("candidate must be a non-empty object")
        candidate=DnaCandidate(dna_id, version, dict(document), parent_digest); self.candidates[candidate.content_digest]=candidate; self._record("PROPOSE", candidate, None); return candidate
    def validate(self, digest: str) -> DnaCandidate:
        c=self._get(digest)
        if c.stage is not EvolutionStage.CANDIDATE: return c
        u=DnaCandidate(c.dna_id,c.version,c.document,c.parent_digest,EvolutionStage.VALIDATED); self.candidates[digest]=u; self._record("VALIDATE",u,None); return u
    def record_replay(self, digest: str, *, passed: bool, score: float, evidence: Mapping[str, object]) -> ReplayResult:
        if not 0 <= score <= 1: raise ValueError("score must be between 0 and 1")
        self._get(digest); r=ReplayResult(digest,passed,score,dict(evidence)); self.replays[digest]=r; return r
    def promote(self, digest: str, *, stage: EvolutionStage, min_score: float=.5) -> DnaCandidate:
        if stage not in {EvolutionStage.SHADOW,EvolutionStage.CANARY,EvolutionStage.ACTIVE}: raise ValueError("invalid promotion stage")
        c=self._get(digest); r=self.replays.get(digest)
        if c.stage is not EvolutionStage.VALIDATED or r is None or not r.passed or r.score < min_score: raise ValueError("candidate has not passed replay gate")
        u=DnaCandidate(c.dna_id,c.version,c.document,c.parent_digest,stage); self.candidates[digest]=u; self._record("PROMOTE",u,r); return u
    def rollback(self, digest: str, *, reason: str) -> DnaCandidate:
        c=self._get(digest); u=DnaCandidate(c.dna_id,c.version,c.document,c.parent_digest,EvolutionStage.ROLLED_BACK); self.candidates[digest]=u; self._record(reason,u,None); return u
    def _get(self,digest: str)->DnaCandidate:
        if digest not in self.candidates: raise KeyError("unknown DNA candidate")
        return self.candidates[digest]
    def _record(self,a: str,c: DnaCandidate,r: ReplayResult|None)->None: self.audit.append({"action":a,"digest":c.content_digest,"stage":c.stage.value,"score":None if r is None else r.score})

    async def persist(self, candidate: DnaCandidate, *, correlation_id: str = "evolution") -> None:
        if self.database is None: return
        now = self.clock.now().isoformat() if self.clock else "1970-01-01T00:00:00+00:00"
        async with self.database.transaction() as tx:
            await tx.execute("INSERT OR IGNORE INTO dna_evolution_candidate VALUES (?,?,?,?,?,?,?,?)", (candidate.content_digest,candidate.dna_id,candidate.version,json.dumps(candidate.document,sort_keys=True),candidate.parent_digest,candidate.stage.value,1,now))
            await tx.execute("INSERT INTO dna_evolution_audit VALUES (?,?,?,?,?,?,?)", (hashlib.sha256((candidate.content_digest+candidate.stage.value+now).encode()).hexdigest(),candidate.content_digest,"PERSIST",candidate.stage.value,1,json.dumps({"correlation_id":correlation_id}),now))

    async def persist_replay(self, result: ReplayResult) -> None:
        if self.database is None: return
        now = self.clock.now().isoformat() if self.clock else "1970-01-01T00:00:00+00:00"
        async with self.database.transaction() as tx:
            await tx.execute("INSERT OR REPLACE INTO dna_evolution_replay VALUES (?,?,?,?,?)", (result.candidate_digest,int(result.passed),result.score,json.dumps(result.evidence,sort_keys=True),now))
