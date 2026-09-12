"""X-02: episodic outcome/trace → candidate experience extraction with evidence chains.

把一次情景记忆——Episode（G01）、OutcomeEvaluation（G02/G03）与 correlation
TraceBundle（G01）——确定性地转成一个带完整证据链的 CANDIDATE 经验样本：

* 只接受三方引用互洽且任务已终态的事实；Episode/Outcome/Trace 缺一拒绝；
* 产出与 ``RestRepair.complete`` 的候选经验契约兼容（``status=CANDIDATE`` 且
  ``evidence_episode_ids`` 指向被复盘的 Episode），可直接进入日复盘落库；
* ``experience_id``/``content_digest`` 只由输入内容与抽取器版本决定，与时钟
  无关——同一情景重复抽取得到同一经验（幂等），为 X-03 重放验证提供锚点。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from brain_kernel.ports import Clock

_TERMINAL_TASK_STATUS = frozenset({"SUCCEEDED", "FAILED"})
_OUTCOME_SECTIONS = ("execution", "goal", "quality", "evidence")


class ExperienceExtractionError(ValueError):
    pass


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceChain:
    episode_id: str
    evaluation_id: str
    correlation_id: str
    task_status: str
    evidence_ids: tuple[str, ...]
    trace_digest: str


@dataclass(frozen=True, slots=True)
class ExperienceCandidate:
    experience_id: str
    statement: str
    summary: str
    conditions: Mapping[str, object]
    outcome: Mapping[str, object]
    successful: bool
    quality_score: float
    evidence: EvidenceChain
    extractor_version: str
    extracted_at: datetime
    content_digest: str

    def __post_init__(self) -> None:
        if not 0 <= self.quality_score <= 1:
            raise ExperienceExtractionError("quality_score must be between 0 and 1")
        if self.extracted_at.tzinfo is None or self.extracted_at.utcoffset() is None:
            raise ExperienceExtractionError("extracted_at must be timezone-aware")
        object.__setattr__(self, "conditions", MappingProxyType(dict(self.conditions)))
        object.__setattr__(self, "outcome", MappingProxyType(dict(self.outcome)))

    def to_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": "1.0",
            "experience_id": self.experience_id,
            "status": "CANDIDATE",
            "statement": self.statement,
            "summary": self.summary,
            "conditions": dict(self.conditions),
            "outcome": dict(self.outcome),
            "successful": self.successful,
            "quality_score": self.quality_score,
            "evidence_episode_ids": [self.evidence.episode_id],
            "evidence": {
                "evaluation_id": self.evidence.evaluation_id,
                "correlation_id": self.evidence.correlation_id,
                "task_status": self.evidence.task_status,
                "evidence_ids": list(self.evidence.evidence_ids),
                "trace_digest": self.evidence.trace_digest,
            },
            "extractor_version": self.extractor_version,
            "extracted_at": self.extracted_at.isoformat().replace("+00:00", "Z"),
        }
        document["content_digest"] = _digest({
            key: value for key, value in document.items()
            if key not in {"content_digest", "extracted_at"}
        })
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> ExperienceCandidate:
        if not isinstance(document, Mapping):
            raise ExperienceExtractionError("experience document must be an object")
        stored = document.get("content_digest")
        expected = _digest({key: value for key, value in document.items()
                            if key not in {"content_digest", "extracted_at"}})
        if stored != expected:
            raise ExperienceExtractionError("experience content digest mismatch")
        evidence = document.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ExperienceExtractionError("experience evidence must be an object")
        extracted_at = document.get("extracted_at")
        if not isinstance(extracted_at, str):
            raise ExperienceExtractionError("experience extracted_at must be a timestamp")
        conditions = document.get("conditions")
        outcome = document.get("outcome")
        if not isinstance(conditions, Mapping) or not isinstance(outcome, Mapping):
            raise ExperienceExtractionError(
                "experience conditions and outcome must be objects"
            )
        episode_ids = document.get("evidence_episode_ids")
        if not isinstance(episode_ids, list) or len(episode_ids) != 1:
            raise ExperienceExtractionError(
                "experience evidence_episode_ids must reference exactly one episode"
            )
        return cls(
            experience_id=str(document["experience_id"]),
            statement=str(document["statement"]),
            summary=str(document["summary"]),
            conditions=conditions,
            outcome=outcome,
            successful=bool(document["successful"]),
            quality_score=float(document["quality_score"]),  # type: ignore[arg-type]
            evidence=EvidenceChain(
                episode_id=str(episode_ids[0]),
                evaluation_id=str(evidence["evaluation_id"]),
                correlation_id=str(evidence["correlation_id"]),
                task_status=str(evidence["task_status"]),
                evidence_ids=tuple(str(item) for item in evidence["evidence_ids"]),
                trace_digest=str(evidence["trace_digest"]),
            ),
            extractor_version=str(document["extractor_version"]),
            extracted_at=datetime.fromisoformat(extracted_at),
            content_digest=str(expected),
        )


class ExperienceExtractor:
    """Deterministic episodic-memory → candidate-experience transformation."""

    VERSION = "experience-extractor/1.0"

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def extract(
        self, *, episode: Mapping[str, object], evaluation: Mapping[str, object],
        trace: Mapping[str, object],
    ) -> ExperienceCandidate:
        for name, document in (
            ("episode", episode), ("evaluation", evaluation), ("trace", trace),
        ):
            if not isinstance(document, Mapping):
                raise ExperienceExtractionError(f"{name} document must be an object")
        episode_id = str(episode.get("episode_id", ""))
        correlation_id = str(episode.get("correlation_id", ""))
        if not episode_id or not correlation_id or not episode.get("task_id"):
            raise ExperienceExtractionError("episode identity must not be empty")
        if evaluation.get("episode_id") != episode_id:
            raise ExperienceExtractionError(
                "evaluation episode_id does not match the episode"
            )
        if str(evaluation.get("correlation_id", "")) != correlation_id:
            raise ExperienceExtractionError(
                "evaluation correlation_id does not match the episode"
            )
        task_status = str(evaluation.get("task_status", ""))
        if task_status not in _TERMINAL_TASK_STATUS:
            raise ExperienceExtractionError(
                f"task is not terminal: {task_status or 'missing'}"
            )
        successful = evaluation.get("successful")
        if not isinstance(successful, bool):
            raise ExperienceExtractionError("successful must be a boolean")
        outcome: dict[str, object] = {
            "successful": successful, "task_status": task_status,
        }
        scores: dict[str, float] = {}
        for section in _OUTCOME_SECTIONS:
            value, score = self._section(evaluation, section)
            outcome[section] = value
            scores[section] = score
        self._check_trace(trace, episode_id=episode_id, task_id=str(evaluation["task_id"]))
        quality = scores["quality"]
        goal_id = str(evaluation.get("goal_id", ""))
        raw_evidence_ids = evaluation.get("evidence_ids", ())
        evidence_ids = (
            tuple(str(item) for item in raw_evidence_ids)
            if isinstance(raw_evidence_ids, list) else ()
        )
        statement = (
            f"Episode {episode_id} finished {task_status}: "
            f"quality {quality:.2f}, execution {scores['execution']:.2f}, "
            f"goal {scores['goal']:.2f}, evidence {scores['evidence']:.2f}."
        )
        summary = (
            f"{'Successful' if successful else 'Failed'} task with quality "
            f"{quality:.2f} and {len(evidence_ids)} evidence items."
        )
        experience_id = _digest({
            "extractor": self.VERSION,
            "episode_id": episode_id,
            "evaluation_id": str(evaluation.get("evaluation_id", "")),
            "correlation_id": correlation_id,
            "outcome": outcome,
        })
        extracted_at = self._clock.now()
        if extracted_at.tzinfo is None or extracted_at.utcoffset() is None:
            raise ExperienceExtractionError("extractor clock must be timezone-aware")
        candidate = ExperienceCandidate(
            experience_id=experience_id,
            statement=statement,
            summary=summary,
            conditions={"task_status": task_status, "goal_id": goal_id},
            outcome=outcome,
            successful=successful,
            quality_score=quality,
            evidence=EvidenceChain(
                episode_id=episode_id,
                evaluation_id=str(evaluation.get("evaluation_id", "")),
                correlation_id=correlation_id,
                task_status=task_status,
                evidence_ids=evidence_ids,
                trace_digest=_digest(trace),
            ),
            extractor_version=self.VERSION,
            extracted_at=extracted_at,
            content_digest="",
        )
        document = candidate.to_document()
        return replace(candidate, content_digest=str(document["content_digest"]))

    def _section(
        self, evaluation: Mapping[str, object], section: str,
    ) -> tuple[Mapping[str, object], float]:
        value = evaluation.get(section)
        if not isinstance(value, Mapping) or "score" not in value:
            raise ExperienceExtractionError(f"outcome section {section} is missing")
        score = value["score"]
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise ExperienceExtractionError(f"outcome section {section} score must be numeric")
        if not 0 <= float(score) <= 1:
            raise ExperienceExtractionError(
                f"outcome section {section} score must be between 0 and 1"
            )
        return value, float(score)

    def _check_trace(
        self, trace: Mapping[str, object], *, episode_id: str, task_id: str,
    ) -> None:
        episodes = trace.get("episodes")
        tasks = trace.get("tasks")
        episodes_ok = isinstance(episodes, list) and any(
            isinstance(item, Mapping) and str(item.get("episode_id")) == episode_id
            for item in episodes
        )
        if not episodes_ok:
            raise ExperienceExtractionError(
                f"trace bundle is missing episode {episode_id}"
            )
        tasks_ok = isinstance(tasks, list) and any(
            isinstance(item, Mapping) and str(item.get("task_id")) == task_id
            for item in tasks
        )
        if not tasks_ok:
            raise ExperienceExtractionError(f"trace bundle is missing task {task_id}")
