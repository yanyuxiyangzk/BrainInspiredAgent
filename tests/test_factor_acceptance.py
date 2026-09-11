"""L-010 tests: v1.5 acceptance — 581-round deterministic replay, fault injection,
cost/coverage/diversity report.

验收主断言：默认 581 轮全链路回放（生成→审查→FSA→回测→自适应→持久化→Hooks）
全部检查 PASSED；注入故障后恢复契约成立；全程零非法回测、零重复回测。
"""
from __future__ import annotations

import pytest

from domain_sdk.factor_acceptance import (
    FactorDiscoveryReplay,
    FaultPlan,
    ReplayAcceptancePolicy,
)

pytestmark = pytest.mark.asyncio

POLICY = ReplayAcceptancePolicy(total_rounds=581)


async def test_full_replay_581_rounds_passes_every_check() -> None:
    replay = FactorDiscoveryReplay(POLICY)
    report = await replay.run()
    assert report.status == "PASSED"
    assert report.check("replay_completed").passed
    assert report.check("rounds_exact").evidence == "581"
    failed = [c.name for c in report.checks if not c.passed]
    assert failed == [], failed


async def test_report_records_cost_coverage_and_diversity() -> None:
    replay = FactorDiscoveryReplay(POLICY)
    report = await replay.run()
    assert report.cost["candidates_generated"] > 0
    assert report.cost["backtests"] > 0
    assert report.cost["backtests"] <= report.cost["candidates_generated"]
    assert report.cost["review_rejected"] + report.cost["fsa_blocked"] + report.cost["backtests"] <= (
        report.cost["candidates_generated"]
    )
    assert report.coverage["factor_library"] >= 1
    assert report.coverage["strategies"] >= 2
    assert report.coverage["iterations"] == 581
    assert report.diversity["distinct_skeletons"] >= 3
    assert report.diversity["bans_issued"] >= 0


async def test_fault_injection_preserves_recovery_contract() -> None:
    policy = ReplayAcceptancePolicy(
        total_rounds=120,
        faults=FaultPlan(crash_rounds=(40, 80), stale_pointer_rounds=(60,), facts_tamper_rounds=(100,)),
    )
    replay = FactorDiscoveryReplay(policy)
    report = await replay.run()
    assert report.status == "PASSED"
    recovery = report.check("recovery_contract")
    assert recovery.passed
    assert report.faults_injected == 4
    # 崩溃后迭代不丢失：恢复后的 checkpoint 迭代数单调不减
    assert report.coverage["iterations"] == 120


async def test_fault_rounds_never_backtest_illegal_or_duplicate() -> None:
    policy = ReplayAcceptancePolicy(
        total_rounds=80,
        faults=FaultPlan(review_blackout_rounds=(20, 30, 40), crash_rounds=(50,)),
    )
    replay = FactorDiscoveryReplay(policy)
    report = await replay.run()
    assert report.status == "PASSED"
    assert report.check("zero_illegal_backtest").passed
    assert report.check("zero_duplicate_backtest").passed
    # 审查 blackout 轮：零候选可回测，但回放继续推进
    assert report.cost["backtests"] > 0  # 其他轮正常


async def test_blackout_freezes_search_feedback_until_facts_return() -> None:
    policy = ReplayAcceptancePolicy(
        total_rounds=30,
        faults=FaultPlan(review_blackout_rounds=tuple(range(5, 9))),
    )
    replay = FactorDiscoveryReplay(policy)
    report = await replay.run()
    timeline = report.timeline
    pre = next(entry for entry in timeline if entry["round"] == 4)
    black = next(entry for entry in timeline if entry["round"] == 8)
    after = next(entry for entry in timeline if entry["round"] == 9)
    assert black["backtests"] == 0 and after["backtests"] >= 0
    # 无回测事实则动量与停滞计数冻结；恢复回测后反馈继续
    assert black["zero_accept_streak"] == pre["zero_accept_streak"]
    assert black["momentum"] == pre["momentum"]


async def test_report_roundtrip_and_failed_verdict() -> None:
    policy = ReplayAcceptancePolicy(total_rounds=24)
    report = await FactorDiscoveryReplay(policy).run()
    restored = type(report).from_dict(report.to_dict())
    assert restored == report
    assert restored.status == "PASSED"

    broken = report.with_failed_check("replay_completed", "interrupted at round 12")
    assert broken.status == "FAILED"
    assert not broken.check("replay_completed").passed
    assert broken.check("replay_completed").evidence == "interrupted at round 12"
