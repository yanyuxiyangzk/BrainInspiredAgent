"""Factor discovery loop entry: governed adaptive rounds against the fact store.

把 L-001～L-009 的库能力装配成可从 CLI 反复调用的入口（``bia factor-loop
run/status``）：每轮执行 L-003 配额生成 → L-004 自适应步长/预算 → L-005 硬
门槛（含链回看预算）→ L-008 FSA 拦截 → L-007 硬编码回测（预算上限）→
反馈与提交（checkpoint 记录 L-004 搜索状态）；L-009 Hooks 摘要按调用累计。
与 L-010 验收回放共享同一套领域组件，但这里面向持久事实库、不含故障注入。
"""
from __future__ import annotations

import json
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
from domain_sdk.factor_fsa import FsaPolicy, FsaTracker
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
from domain_sdk.factor_loop import FactorDiscoveryLoop, FactorLoopProfile
from domain_sdk.factor_review import (
    CandidateReviewer,
    FactorDimensionTable,
    FactorReviewPolicy,
    OperatorSignature,
)

PROFILE_ID = "factor.discovery"
PROFILE_VERSION = "1.0.0"
_CLOCK_START = datetime(2026, 9, 7, tzinfo=UTC)
_REPLAY_BARS = 120
_ASSETS = ("alpha-1", "alpha-2", "alpha-3", "alpha-4")


def _chain_lookback(definition: object) -> int:
    total = 0
    node = cast(dict[str, object], definition)
    while "op" in node:
        window = cast(int, node["window"])
        total += window if str(node["op"]) == "ts_delta" else window - 1
        node = cast(dict[str, object], node["input"])
    return total


class FactorLoopApp:
    """一次装配、多轮复用；跨调用经由 checkpoint 续跑。"""

    def __init__(
        self, database: SQLiteDatabase, checkpoint: Path, *,
        seed: int = 20260907,
        candidates_per_round: int = 12,
        max_backtests_per_round: int = 4,
    ) -> None:
        if candidates_per_round < 1 or max_backtests_per_round < 1:
            raise ValueError("candidates and backtest budgets must be positive")
        self._database = database
        self._checkpoint = checkpoint
        self._seed = seed
        self._candidates = candidates_per_round
        self._max_backtests = max_backtests_per_round
        self._panel: SimulationPanel = build_simulation_panel(
            seed=seed, total_bars=_REPLAY_BARS, assets=_ASSETS
        )
        vocabulary = FactorVocabulary(
            fields=("close", "noise"),
            operators=("rank", "ts_mean", "ts_delta"),
            windows=(5, 10, 20),
        )
        self._vocabulary = vocabulary
        self._gate = CandidateReviewer(
            vocabulary,
            FactorDimensionTable(
                field_dimensions={"close": "price", "noise": "price"},
                operator_signatures={
                    "rank": OperatorSignature(
                        accepted_input_dimensions=None,
                        output_dimension="shapeless", idempotent=True,
                    ),
                    "ts_mean": OperatorSignature(
                        accepted_input_dimensions=None, output_dimension=None
                    ),
                    "ts_delta": OperatorSignature(
                        accepted_input_dimensions=None, output_dimension=None
                    ),
                },
            ),
            policy=FactorReviewPolicy(min_window=5, max_window=20),
        )
        self._backtester = FactorBacktester(
            BacktestSpec(
                assets=_ASSETS, total_bars=_REPLAY_BARS, in_sample_bars=80,
                max_window=20, horizon_bars=5, recent_bars=40,
                stability_bucket_bars=27,
            )
        )
        self._filter = FilterPolicy(
            min_ic=0.05, min_ic_ir=0.3, min_year_positive_ratio=0.5,
            min_sharpe=0.3, min_recent_ic=0.0, max_ic_correlation=0.9,
            min_oos_ic=0.0, min_oos_ratio=0.3, max_turnover=1.0,
        )
        self._fsa = FsaTracker(FsaPolicy())
        self._pool = ParentPool(capacity=16)
        self._bus = FactorHookBus(PROFILE_ID, PROFILE_VERSION)
        self._collector = FactorIterationSummaryCollector()
        self._bus.register(FactorHookEvent.ITERATION_COMPLETED, self._collector)
        self._bus.register(FactorHookEvent.ITERATION_FAILED, self._collector)
        self._loop = FactorDiscoveryLoop(
            database, FakeClock(_CLOCK_START),
            FactorLoopProfile(
                PROFILE_ID, PROFILE_VERSION,
                max_iterations=10_000, max_consecutive_failures=8,
            ),
            checkpoint_path=checkpoint, hooks=self._bus,
        )
        self._search = AdaptiveFactorSearch(vocabulary, seed=seed)
        self._accepted_hashes: set[str] = set()
        self._recent_accepted: list[dict[str, object]] = []
        self._cost = {"candidates": 0, "backtests": 0, "accepted": 0}

    async def run(self, rounds: int) -> dict[str, object]:
        if rounds < 1:
            raise ValueError("rounds must be positive")
        for _ in range(rounds):
            await self._round_once()
        checkpoint = await self._loop.initialize()
        summary = self._collector.summary()
        return {
            "rounds": rounds,
            "candidates": self._cost["candidates"],
            "backtests": self._cost["backtests"],
            "accepted": self._cost["accepted"],
            "iteration": checkpoint.iteration,
            "status": checkpoint.status.value,
            "completed_events": summary["completed"],
        }

    async def _round_once(self) -> None:
        tested = await self._loop.tested_hashes()
        generated = await self._search.run_round(
            self._candidates, self._pool, seen_hashes=tested
        )
        self._cost["candidates"] += len(generated.candidates)
        report = self._gate.review_batch(
            [item.definition for item in generated.candidates], known_hashes=tested
        )
        survivors: list[tuple[str, dict[str, object]]] = []
        for outcome in report.outcomes:
            if not outcome.accepted:
                continue
            if _chain_lookback(outcome.definition) > self._backtester.spec.max_window:
                continue  # 数据越界：总回看深度超过回测预算
            decision = self._fsa.intercept(outcome.definition)
            if not decision.allowed:
                continue
            survivors.append(
                (candidate_hash(outcome.definition), dict(outcome.definition))
            )
        backtested: list[tuple[str, dict[str, object]]] = []
        accepted_now: list[str] = []
        failure_modes: list[str] = []
        for digest, definition in survivors:
            if len(backtested) >= self._max_backtests:
                break
            if digest in tested or any(digest == seen for seen, _ in backtested):
                continue
            validation = self._backtester.evaluate(
                self._panel, definition, filter_policy=self._filter,
                library=self._recent_accepted[-8:],
            )
            backtested.append((digest, definition))
            if validation.accepted:
                accepted_now.append(digest)
                self._accepted_hashes.add(digest)
                self._recent_accepted.append(definition)
                self._pool.add(definition)
            else:
                failure_modes.extend(validation.failure_modes)
        self._cost["backtests"] += len(backtested)
        self._cost["accepted"] += len(accepted_now)

        feedback = RoundFeedback.from_round(
            generated, [digest for digest, _ in backtested], accepted_now
        )
        state = self._search.observe(feedback)
        self._fsa.observe(
            [item.definition for item in generated.candidates],
            accepted_hashes=accepted_now,
        )
        by_hash = {
            candidate_hash(item.definition): item.definition
            for item in generated.candidates
        }
        await self._loop.commit_iteration(
            candidates=[by_hash[digest] for digest, _ in backtested],
            factors=[by_hash[digest] for digest in accepted_now if digest in by_hash],
            search_state=state.to_dict(),
            details={
                "candidates": len(generated.candidates),
                "accepted": len(accepted_now),
                "failure_modes": failure_modes,
            },
        )


async def run_factor_rounds(
    database: SQLiteDatabase, checkpoint: Path, *, rounds: int,
    seed: int = 20260907, candidates_per_round: int = 12,
    max_backtests_per_round: int = 4,
) -> dict[str, object]:
    """在事实库上运行 N 轮因子发现；checkpoint 缺失时自动初始化。"""
    app = FactorLoopApp(
        database, checkpoint, seed=seed,
        candidates_per_round=candidates_per_round,
        max_backtests_per_round=max_backtests_per_round,
    )
    return await app.run(rounds)


async def factor_loop_status(
    database: SQLiteDatabase, checkpoint: Path
) -> dict[str, object]:
    """只读视角：checkpoint 行 + 指针中的搜索状态。"""
    row = await database.fetch_one(
        "SELECT * FROM discovery_loop_checkpoint WHERE profile_id=? AND version=?",
        (PROFILE_ID, PROFILE_VERSION),
    )
    if row is None:
        return {"status": "UNINITIALIZED", "iteration": 0, "search_state": None}
    search_state = None
    if checkpoint.is_file():
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("search_state"), dict):
            search_state = payload["search_state"]
    return {
        "status": str(row["status"]),
        "iteration": int(cast(int, row["iteration"])),
        "consecutive_failures": int(cast(int, row["consecutive_failures"])),
        "facts_digest": str(row["facts_digest"]) if row["facts_digest"] else None,
        "search_state": search_state,
    }
