"""X-06 tests: memory-enhanced decision A/B loop (dual-track comparison).

同一组决策案例分别由"无记忆基线臂"与"记忆增强臂"决策，确定性裁判打分；
通过条件 = 质量增量达标 且 错误召回（记忆臂检索到禁用记忆）在预算内。
"""
from __future__ import annotations

import pytest

from domain_sdk.decision_ab import (
    AbPolicy,
    Decision,
    DecisionCase,
    DecisionQuality,
    run_ab_comparison,
)


def cases(count: int = 6) -> tuple[DecisionCase, ...]:
    return tuple(
        DecisionCase(
            case_id=f"case-{index}",
            context={"task": "pick-window", "default": "1d", "good": f"{(index % 3) * 5 + 5}d"},
        )
        for index in range(count)
    )


class Judge:
    """确定性裁判：决策等于案例里的 good 值得满分，default 值不及格。"""

    def assess(self, case: DecisionCase, decision: Decision) -> DecisionQuality:
        chosen = decision.document.get("window")
        if chosen == case.context["good"]:
            return DecisionQuality(True, 1.0)
        if chosen == case.context["default"]:
            return DecisionQuality(False, 0.2)
        return DecisionQuality(False, 0.0)


class BaselineDecider:
    """无记忆臂：永远选保守默认值。"""

    async def decide(self, case: DecisionCase) -> Decision:
        return Decision(document={"window": case.context["default"]})


class MemoryDecider:
    """记忆增强臂：检索命中的已验证声明给出 good 值。"""

    def __init__(self, *, recalled: tuple[str, ...] = (), window: str = "good") -> None:
        self._recalled = recalled
        self._window = window

    async def decide(self, case: DecisionCase) -> Decision:
        if self._window == "good":
            return Decision(
                document={"window": case.context["good"]},
                recalled_memory_ids=self._recalled,
            )
        return Decision(
            document={"window": case.context["default"]},  # 记忆没帮上忙
            recalled_memory_ids=self._recalled,
        )


@pytest.mark.asyncio
async def test_memory_arm_improves_quality_and_passes() -> None:
    report = await run_ab_comparison(
        cases(), BaselineDecider(), MemoryDecider(),
        Judge(), forbidden_memory_ids=frozenset(), policy=AbPolicy(),
        correlation_id="ab-1",
    )
    assert report.status == "PASSED"
    assert report.metrics["baseline_quality"] == pytest.approx(0.2)
    assert report.metrics["memory_quality"] == pytest.approx(1.0)
    assert report.metrics["quality_delta"] == pytest.approx(0.8)
    assert report.metrics["error_recall_rate"] == 0.0
    assert report.metrics["baseline_successes"] == 0
    assert report.metrics["memory_successes"] == 6
    assert all(row["memory_quality"] >= row["baseline_quality"] for row in report.case_rows)


@pytest.mark.asyncio
async def test_useless_memory_fails_quality_floor() -> None:
    report = await run_ab_comparison(
        cases(), BaselineDecider(), MemoryDecider(window="default"),
        Judge(), forbidden_memory_ids=frozenset(), policy=AbPolicy(),
        correlation_id="ab-2",
    )
    assert report.status == "FAILED"
    assert report.metrics["quality_delta"] == pytest.approx(0.0)
    assert any("delta" in reason for reason in report.failure_reasons)


@pytest.mark.asyncio
async def test_polluted_memory_fails_error_recall_budget() -> None:
    report = await run_ab_comparison(
        cases(), BaselineDecider(),
        MemoryDecider(recalled=("m-contradicted",)),  # 质量提升但检索到禁用记忆
        Judge(), forbidden_memory_ids=frozenset({"m-contradicted"}),
        policy=AbPolicy(), correlation_id="ab-3",
    )
    assert report.status == "FAILED"
    assert report.metrics["quality_delta"] == pytest.approx(0.8)
    assert report.metrics["error_recall_rate"] == pytest.approx(1.0)
    assert any("error" in reason for reason in report.failure_reasons)


@pytest.mark.asyncio
async def test_min_cases_guard_and_never_below_baseline_guard() -> None:
    short = cases(2)
    report = await run_ab_comparison(
        short, BaselineDecider(), MemoryDecider(),
        Judge(), forbidden_memory_ids=frozenset(),
        policy=AbPolicy(min_cases=5), correlation_id="ab-4",
    )
    assert report.status == "FAILED"
    assert any("cases" in reason for reason in report.failure_reasons)


@pytest.mark.asyncio
async def test_memory_regression_is_flagged() -> None:
    class RegressingDecider:
        async def decide(self, case: DecisionCase) -> Decision:
            return Decision(
                document={"window": "wrong-value"},  # 比默认值还差
                recalled_memory_ids=(),
            )

    report = await run_ab_comparison(
        cases(), BaselineDecider(), RegressingDecider(),
        Judge(), forbidden_memory_ids=frozenset(), policy=AbPolicy(),
        correlation_id="ab-5",
    )
    assert report.status == "FAILED"
    assert report.metrics["quality_delta"] < 0


def test_policy_validates_bounds_and_report_roundtrip() -> None:
    with pytest.raises(ValueError, match="delta"):
        AbPolicy(min_quality_delta=-1.0)
    with pytest.raises(ValueError, match="error"):
        AbPolicy(max_error_recall_rate=1.5)
    with pytest.raises(ValueError, match="cases"):
        AbPolicy(min_cases=0)
    with pytest.raises(ValueError, match="case_id"):
        DecisionCase("", {"task": "t"})
