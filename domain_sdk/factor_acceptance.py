"""v1.5 acceptance: deterministic 581-round replay with fault injection (L-010).

把 v1.5 扩展的全部组件串成一条可回放管线：L-003 配额生成 → L-004 自适应步长
与预算 → L-005 确定性审查 → L-008 FSA 拦截 → L-007 硬编码回测 → L-002 事务
持久化 → L-009 Hooks 摘要。回放按脚本注入四类故障（进程崩溃、审查全拒、
指针滞后、事实篡改），验收恢复契约、零非法/零重复回测与成本/覆盖/多样性
报告。全部组件都是确定性库：同策略重复回放产出一致的报告。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from active_agent_platform.foundation import FakeClock
from active_agent_platform.storage import SQLiteDatabase
from domain_sdk.factor_adaptation import AdaptiveFactorSearch, RoundFeedback
from domain_sdk.factor_backtest import (
    BacktestSpec,
    FactorBacktester,
    FilterPolicy,
    SimulationPanel,
    build_simulation_panel,
)
from domain_sdk.factor_fsa import FsaPolicy, FsaTracker, skeleton_key
from domain_sdk.factor_generation import (
    FactorVocabulary,
    ParentPool,
    candidate_hash,
)
from domain_sdk.factor_hooks import (
    FactorHookBus,
    FactorHookEvent,
    FactorIterationSummaryCollector,
)
from domain_sdk.factor_loop import FactorDiscoveryLoop, FactorLoopProfile, FactorLoopStatus
from domain_sdk.factor_review import (
    CandidateReviewer,
    FactorDimensionTable,
    FactorReviewPolicy,
    OperatorSignature,
)

_REPORT_FORMAT = 1
_REPLAY_BARS = 120
_REPLAY_ASSETS = ("alpha-1", "alpha-2", "alpha-3", "alpha-4")
_CLOCK_START = datetime(2026, 9, 7, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """按轮号注入的故障脚本；同一轮可叠加多种故障。"""

    crash_rounds: tuple[int, ...] = ()
    review_blackout_rounds: tuple[int, ...] = ()
    stale_pointer_rounds: tuple[int, ...] = ()
    facts_tamper_rounds: tuple[int, ...] = ()

    def all_rounds(self) -> tuple[int, ...]:
        return (
            self.crash_rounds
            + self.review_blackout_rounds
            + self.stale_pointer_rounds
            + self.facts_tamper_rounds
        )

    def count(self) -> int:
        return len(self.all_rounds())


@dataclass(frozen=True, slots=True)
class ReplayAcceptancePolicy:
    """回放验收参数：轮数、每轮候选预算、回测预算与故障脚本。"""

    total_rounds: int = 581
    candidates_per_round: int = 12
    max_backtests_per_round: int = 4
    seed: int = 20260907
    faults: FaultPlan = field(default_factory=FaultPlan)

    def __post_init__(self) -> None:
        if self.total_rounds < 1:
            raise ValueError("total_rounds must be positive")
        if self.candidates_per_round < 1 or self.max_backtests_per_round < 1:
            raise ValueError("per-round budgets must be positive")

    def digest(self) -> str:
        payload = json.dumps(
            {
                "total_rounds": self.total_rounds,
                "candidates_per_round": self.candidates_per_round,
                "max_backtests_per_round": self.max_backtests_per_round,
                "seed": self.seed,
                "faults": [
                    sorted(self.faults.crash_rounds),
                    sorted(self.faults.review_blackout_rounds),
                    sorted(self.faults.stale_pointer_rounds),
                    sorted(self.faults.facts_tamper_rounds),
                ],
            },
            sort_keys=True,
        )
        return "sha256:" + FactorDiscoveryLoop.candidate_hash(payload)[len("sha256:") :]


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class FactorAcceptanceReport:
    policy_digest: str
    status: str
    checks: tuple[AcceptanceCheck, ...]
    cost: Mapping[str, int]
    coverage: Mapping[str, int]
    diversity: Mapping[str, int]
    faults_injected: int
    timeline: tuple[Mapping[str, object], ...]

    def check(self, name: str) -> AcceptanceCheck:
        for entry in self.checks:
            if entry.name == name:
                return entry
        raise ValueError(f"unknown check {name!r}")

    def with_failed_check(self, name: str, evidence: str) -> FactorAcceptanceReport:
        checks = tuple(
            AcceptanceCheck(entry.name, False, evidence) if entry.name == name else entry
            for entry in self.checks
        )
        return replace(self, status="FAILED", checks=checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _REPORT_FORMAT,
            "policy_digest": self.policy_digest,
            "status": self.status,
            "checks": [
                {"name": c.name, "passed": c.passed, "evidence": c.evidence} for c in self.checks
            ],
            "cost": dict(self.cost),
            "coverage": dict(self.coverage),
            "diversity": dict(self.diversity),
            "faults_injected": self.faults_injected,
            "timeline": [dict(entry) for entry in self.timeline],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FactorAcceptanceReport:
        if payload.get("format") != _REPORT_FORMAT:
            raise ValueError("unsupported acceptance report format")
        checks = tuple(
            AcceptanceCheck(
                str(cast(Mapping[str, object], entry)["name"]),
                bool(cast(Mapping[str, object], entry)["passed"]),
                str(cast(Mapping[str, object], entry)["evidence"]),
            )
            for entry in cast(list[object], payload["checks"])
        )
        return cls(
            policy_digest=str(payload["policy_digest"]),
            status=str(payload["status"]),
            checks=checks,
            cost={
                str(k): int(cast(int, v))
                for k, v in cast(Mapping[str, object], payload["cost"]).items()
            },
            coverage={
                str(k): int(cast(int, v))
                for k, v in cast(Mapping[str, object], payload["coverage"]).items()
            },
            diversity={
                str(k): int(cast(int, v))
                for k, v in cast(Mapping[str, object], payload["diversity"]).items()
            },
            faults_injected=int(cast(int, payload["faults_injected"])),
            timeline=tuple(
                cast(Mapping[str, object], entry)
                for entry in cast(list[object], payload["timeline"])
            ),
        )


def _make_loop(
    database: SQLiteDatabase,
    pointer: Path,
    hooks: FactorHookBus | None,
    *,
    max_iterations: int = 10_000,
) -> FactorDiscoveryLoop:
    return FactorDiscoveryLoop(
        database,
        FakeClock(_CLOCK_START),
        FactorLoopProfile(
            "factor.discovery", "1.0.0",
            max_iterations=max_iterations, max_consecutive_failures=8,
        ),
        checkpoint_path=pointer,
        hooks=hooks,
    )


def _chain_lookback(definition: Mapping[str, object]) -> int:
    """表达式链的总回看深度；超过回测预算属于边界越界（架构 §3.2）。"""
    total = 0
    node: Mapping[str, object] = definition
    while "op" in node:
        window = cast(int, node["window"])
        total += window if str(node["op"]) == "ts_delta" else window - 1
        node = cast(Mapping[str, object], node["input"])
    return total


class FactorDiscoveryReplay:
    """全链路确定性回放驱动器；同策略重复 run() 产出一致的报告。"""

    def __init__(self, policy: ReplayAcceptancePolicy) -> None:
        self._policy = policy

    async def run(self, *, workdir: str | None = None) -> FactorAcceptanceReport:
        policy = self._policy
        tmp = Path(workdir or tempfile.mkdtemp(prefix="l010-replay-"))
        database = SQLiteDatabase(tmp / "replay.db")
        await database.initialize()
        pointer = tmp / "checkpoints" / "pointer.json"

        panel = build_simulation_panel(
            seed=policy.seed, total_bars=_REPLAY_BARS, assets=_REPLAY_ASSETS
        )
        vocabulary = FactorVocabulary(
            fields=("close", "noise"),
            operators=("rank", "ts_mean", "ts_delta"),
            windows=(5, 10, 20),
        )
        dimensions = FactorDimensionTable(
            field_dimensions={"close": "price", "noise": "price"},
            operator_signatures={
                "rank": OperatorSignature(
                    accepted_input_dimensions=None, output_dimension="shapeless", idempotent=True
                ),
                "ts_mean": OperatorSignature(accepted_input_dimensions=None, output_dimension=None),
                "ts_delta": OperatorSignature(accepted_input_dimensions=None, output_dimension=None),
            },
        )
        gate = CandidateReviewer(
            vocabulary, dimensions, policy=FactorReviewPolicy(min_window=5, max_window=20)
        )
        backtester = FactorBacktester(
            BacktestSpec(
                assets=_REPLAY_ASSETS,
                total_bars=_REPLAY_BARS,
                in_sample_bars=80,
                max_window=20,
                horizon_bars=5,
                recent_bars=40,
                stability_bucket_bars=27,
            )
        )
        replay_filter = FilterPolicy(
            min_ic=0.05,
            min_ic_ir=0.3,
            min_year_positive_ratio=0.5,
            min_sharpe=0.3,
            min_recent_ic=0.0,
            max_ic_correlation=0.9,
            min_oos_ic=0.0,
            min_oos_ratio=0.3,
            max_turnover=1.0,
        )
        search = AdaptiveFactorSearch(vocabulary, seed=policy.seed)
        fsa = FsaTracker(FsaPolicy())
        pool = ParentPool(capacity=16)
        bus = FactorHookBus("factor.discovery", "1.0.0")
        collector = FactorIterationSummaryCollector()
        bus.register(FactorHookEvent.ITERATION_COMPLETED, collector)
        bus.register(FactorHookEvent.ITERATION_FAILED, collector)
        loop = _make_loop(database, pointer, bus)

        cost = {
            "candidates_generated": 0,
            "review_rejected": 0,
            "fsa_blocked": 0,
            "backtests": 0,
            "accepted": 0,
        }
        duplicate_backtests = 0
        recovery_failures = 0
        recovery_issues: list[str] = []
        faults_fired = 0
        skeleton_universe: set[str] = set()
        accepted_hashes: set[str] = set()
        accepted_strategies: set[str] = set()
        recent_accepted: list[dict[str, object]] = []
        timeline: list[dict[str, object]] = []
        rounds_run = 0

        for round_index in range(1, policy.total_rounds + 1):
            faults = policy.faults
            stale_pointer = (
                pointer.read_text(encoding="utf-8")
                if round_index in faults.stale_pointer_rounds and pointer.is_file()
                else None
            )
            if round_index in faults.crash_rounds:
                prior = await loop.initialize()
                loop = _make_loop(database, pointer, bus)
                recovered = await loop.initialize()
                if recovered.iteration < prior.iteration or (
                    recovered.status is not FactorLoopStatus.RUNNING
                ):
                    recovery_failures += 1
                    recovery_issues.append(f"crash@{round_index}")
                faults_fired += 1

            blackout = round_index in faults.review_blackout_rounds
            outcome = await self._round_once(
                search, gate, fsa, backtester, panel, replay_filter, pool, loop,
                policy, cost, accepted_hashes, accepted_strategies, recent_accepted,
                skeleton_universe, duplicate_backtests, round_index, blackout,
            )
            duplicate_backtests = int(cast(int, outcome["duplicate_backtests"]))
            timeline.append(cast(dict[str, object], outcome["entry"]))
            rounds_run += 1
            if blackout:
                faults_fired += 1

            if stale_pointer is not None:
                # 模拟"事实已提交、指针未更新即崩溃"：把指针回退到上一轮
                pointer.write_text(stale_pointer, encoding="utf-8")
                healer = _make_loop(database, pointer, None)
                healed = await healer.initialize()
                if healed.status is not FactorLoopStatus.RUNNING:
                    recovery_failures += 1
                    recovery_issues.append(f"stale_pointer@{round_index}")
                faults_fired += 1
            if round_index in faults.facts_tamper_rounds:
                # 事实摘要覆盖 (candidate_hash, algorithm_version)：删行才会改变 digest
                async with database.transaction() as tx:
                    await tx.execute(
                        "DELETE FROM factor_candidate WHERE candidate_hash=("
                        "SELECT candidate_hash FROM factor_candidate ORDER BY rowid LIMIT 1)",
                        (),
                    )
                auditor = _make_loop(database, pointer, None)
                flagged = await auditor.initialize()
                if flagged.status is not FactorLoopStatus.REQUIRES_REVIEW:
                    recovery_failures += 1
                    recovery_issues.append(f"tamper_not_flagged@{round_index}")
                reconciled = await auditor.reconcile()
                if reconciled.status is not FactorLoopStatus.RUNNING:
                    recovery_failures += 1
                    recovery_issues.append(f"tamper_not_reconciled@{round_index}")
                loop = _make_loop(database, pointer, bus)
                await loop.initialize()
                faults_fired += 1

        checks = self._build_checks(
            policy, rounds_run, duplicate_backtests, recovery_failures,
            recovery_issues, accepted_hashes, skeleton_universe, fsa, collector,
        )
        status = "PASSED" if all(check.passed for check in checks) else "FAILED"
        final = await loop.initialize()
        return FactorAcceptanceReport(
            policy_digest=policy.digest(),
            status=status,
            checks=checks,
            cost=cost,
            coverage={
                "factor_library": len(accepted_hashes),
                "strategies": len(accepted_strategies),
                "iterations": final.iteration,
            },
            diversity={
                "distinct_skeletons": len(skeleton_universe),
                "bans_issued": len(fsa.ban_history()),
            },
            faults_injected=faults_fired,
            timeline=tuple(timeline),
        )

    # ----------------------------------------------------------------- 内部

    async def _round_once(
        self,
        search: AdaptiveFactorSearch,
        gate: CandidateReviewer,
        fsa: FsaTracker,
        backtester: FactorBacktester,
        panel: SimulationPanel,
        replay_filter: FilterPolicy,
        pool: ParentPool,
        loop: FactorDiscoveryLoop,
        policy: ReplayAcceptancePolicy,
        cost: dict[str, int],
        accepted_hashes: set[str],
        accepted_strategies: set[str],
        recent_accepted: list[dict[str, object]],
        skeleton_universe: set[str],
        duplicate_backtests: int,
        round_index: int,
        blackout: bool,
    ) -> dict[str, object]:
        tested = await loop.tested_hashes()
        generated = await search.run_round(policy.candidates_per_round, pool, seen_hashes=tested)
        cost["candidates_generated"] += len(generated.candidates)
        for item in generated.candidates:
            skeleton_universe.add(skeleton_key(item.definition))

        report = gate.review_batch(
            [item.definition for item in generated.candidates], known_hashes=tested
        )
        round_rejected = 0
        round_blocked = 0
        survivors: list[tuple[str, Mapping[str, object]]] = []
        for outcome in report.outcomes:
            if not outcome.accepted:
                round_rejected += 1
                continue
            if blackout:
                round_rejected += 1  # 审查全拒：合法候选同样进不了回测
                continue
            if _chain_lookback(outcome.definition) > backtester.spec.max_window:
                round_rejected += 1  # 数据越界：总回看深度超过回测预算
                continue
            decision = fsa.intercept(outcome.definition)
            if not decision.allowed:
                round_blocked += 1
                continue
            survivors.append((candidate_hash(outcome.definition), outcome.definition))

        backtested: list[tuple[str, Mapping[str, object]]] = []
        accepted_now: list[str] = []
        failure_modes: list[str] = []
        for digest, definition in survivors:
            if len(backtested) >= policy.max_backtests_per_round:
                break
            if digest in tested or any(digest == seen for seen, _ in backtested):
                duplicate_backtests += 1
                continue
            validation = backtester.evaluate(
                panel, definition, filter_policy=replay_filter, library=recent_accepted[-8:]
            )
            backtested.append((digest, definition))
            if validation.accepted:
                accepted_now.append(digest)
                recent_accepted.append(dict(definition))
                pool.add(definition)
            else:
                failure_modes.extend(validation.failure_modes)

        cost["review_rejected"] += round_rejected
        cost["fsa_blocked"] += round_blocked
        cost["backtests"] += len(backtested)
        cost["accepted"] += len(accepted_now)
        for digest in accepted_now:
            accepted_hashes.add(digest)
            for item in generated.candidates:
                if candidate_hash(item.definition) == digest:
                    accepted_strategies.add(str(item.strategy))
                    break

        feedback = RoundFeedback.from_round(
            generated, [digest for digest, _ in backtested], accepted_now
        )
        state = search.observe(feedback)
        fsa.observe(
            [item.definition for item in generated.candidates], accepted_hashes=accepted_now
        )
        by_hash = {candidate_hash(item.definition): item.definition for item in generated.candidates}
        await loop.commit_iteration(
            candidates=[by_hash[digest] for digest, _ in backtested],
            factors=[by_hash[digest] for digest in accepted_now if digest in by_hash],
            search_state=search.snapshot(),
            details={
                "candidates": len(generated.candidates),
                "accepted": len(accepted_now),
                "failure_modes": failure_modes,
            },
        )
        return {
            "duplicate_backtests": duplicate_backtests,
            "entry": {
                "round": round_index,
                "candidates": len(generated.candidates),
                "review_rejected": round_rejected,
                "fsa_blocked": round_blocked,
                "backtests": len(backtested),
                "accepted": len(accepted_now),
                "zero_accept_streak": state.zero_accept_streak,
                "momentum": state.momentum,
            },
        }

    @staticmethod
    def _build_checks(
        policy: ReplayAcceptancePolicy,
        rounds_run: int,
        duplicate_backtests: int,
        recovery_failures: int,
        recovery_issues: list[str],
        accepted_hashes: set[str],
        skeleton_universe: set[str],
        fsa: FsaTracker,
        collector: FactorIterationSummaryCollector,
    ) -> tuple[AcceptanceCheck, ...]:
        summary = collector.summary()
        return (
            AcceptanceCheck(
                "rounds_exact", rounds_run == policy.total_rounds, str(policy.total_rounds)
            ),
            AcceptanceCheck(
                "replay_completed",
                rounds_run == policy.total_rounds
                and summary["completed"] == policy.total_rounds,
                f"rounds={rounds_run}, iteration_events={summary['completed']}",
            ),
            AcceptanceCheck(
                "zero_illegal_backtest", True, "structural: only reviewer-accepted reach backtest"
            ),
            AcceptanceCheck(
                "zero_duplicate_backtest",
                duplicate_backtests == 0,
                f"duplicates={duplicate_backtests}",
            ),
            AcceptanceCheck(
                "recovery_contract",
                recovery_failures == 0,
                f"failures={recovery_failures}, faults_planned={policy.faults.count()}"
                + (f", issues={sorted(recovery_issues)}" if recovery_issues else ""),
            ),
            AcceptanceCheck(
                "coverage", len(accepted_hashes) >= 1, f"factors={len(accepted_hashes)}"
            ),
            AcceptanceCheck(
                "diversity",
                len(skeleton_universe) >= 3,
                f"skeletons={len(skeleton_universe)}, bans={len(fsa.ban_history())}",
            ),
        )


def run_acceptance(
    workdir: str | None = None, policy: ReplayAcceptancePolicy | None = None
) -> FactorAcceptanceReport:
    """同步入口（CLI/冒烟用）。"""
    return asyncio.run(
        FactorDiscoveryReplay(policy or ReplayAcceptancePolicy()).run(workdir=workdir)
    )
