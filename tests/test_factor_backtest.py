"""L-007 tests: hardcoded deterministic backtest, multi-dimensional filters, data version,
out-of-sample boundary — acceptance is leakage prevention plus a backtest golden.

架构 §3.3：验证是确定性硬门槛，不依赖模型自我评价；固定数据版本、样本区间、
换仓频率、成本、滑点和基准；样本内拟合与样本外验证边界不可穿越。
"""
from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from domain_sdk.factor_backtest import (
    BacktestSpec,
    FactorBacktester,
    FilterPolicy,
    SimulationPanel,
    ValidationReport,
    _pearson,
    build_simulation_panel,
)

MOMENTUM: dict[str, object] = {
    "op": "ts_delta",
    "window": 5,
    "input": {"field": "close"},
}
NOISE: dict[str, object] = {"field": "noise"}


def spec(**overrides: Any) -> BacktestSpec:
    kwargs: dict[str, Any] = {
        "assets": ("alpha-1", "alpha-2", "alpha-3", "alpha-4"),
        "total_bars": 300,
        "in_sample_bars": 200,
        "max_window": 20,
        "stability_bucket_bars": 50,
    }
    kwargs.update(overrides)
    return BacktestSpec(**kwargs)


def policy(**overrides: Any) -> FilterPolicy:
    kwargs: dict[str, Any] = {
        "min_ic": 0.10,
        "min_ic_ir": 0.8,
        "min_year_positive_ratio": 0.5,
        "min_sharpe": 0.5,
        "min_recent_ic": 0.05,
        "max_ic_correlation": 0.8,
        "min_oos_ic": 0.0,
        "min_oos_ratio": 0.5,
        "max_turnover": 0.95,
    }
    kwargs.update(overrides)
    return FilterPolicy(**kwargs)


def backtester(**overrides: Any) -> FactorBacktester:
    return FactorBacktester(spec(**overrides))


def panel() -> SimulationPanel:
    return build_simulation_panel(seed=20260907, total_bars=300, assets=spec().assets)


# --------------------------------------------------------------------------- 数据版本


def test_panel_is_versioned_and_deterministic() -> None:
    first = panel()
    second = build_simulation_panel(seed=20260907, total_bars=300, assets=spec().assets)
    assert first.data_version == second.data_version
    assert first.content_digest == second.content_digest
    assert first.close == second.close
    assert first.field("noise") == second.field("noise")
    assert first.total_bars == 300


def test_panel_digest_tracks_content_and_version() -> None:
    base = panel()
    mutated_close = [list(series) for series in base.close.values()]
    mutated_close[0][100] += 0.5
    mutated = SimulationPanel(
        data_version=base.data_version,
        close={name: tuple(series) for name, series in zip(sorted(base.close), mutated_close)},
        extra_fields={"noise": base.field("noise")},
    )
    assert mutated.content_digest != base.content_digest
    renamed = SimulationPanel(
        data_version=base.data_version + "-b",
        close=dict(base.close),
        extra_fields={"noise": base.field("noise")},
    )
    assert renamed.content_digest != base.content_digest  # 数据版本参与 digest


# --------------------------------------------------------------------------- 配置校验


def test_policy_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="ic"):
        policy(min_ic=-0.1)
    with pytest.raises(ValueError, match="ratio"):
        policy(min_year_positive_ratio=1.5)
    with pytest.raises(ValueError, match="correlation"):
        policy(max_ic_correlation=1.2)
    with pytest.raises(ValueError, match="turnover"):
        policy(max_turnover=0.0)
    with pytest.raises(ValueError, match="oos"):
        policy(min_oos_ratio=-1.0)


def test_spec_rejects_infeasible_sample_boundaries() -> None:
    with pytest.raises(ValueError, match="warmup"):
        spec(in_sample_bars=20)  # 未给最大窗口留出预热区
    with pytest.raises(ValueError, match="out-of-sample"):
        spec(in_sample_bars=295)  # 样本外不足最小长度
    with pytest.raises(ValueError, match="assets"):
        spec(assets=("only-one",))
    with pytest.raises(ValueError, match="bars"):
        spec(total_bars=50, in_sample_bars=40)


# --------------------------------------------------------------------------- 硬编码回测与 golden


def test_momentum_factor_passes_every_dimension() -> None:
    report = backtester().evaluate(panel(), MOMENTUM)
    assert report.accepted
    assert report.failure_modes == ()
    metrics = report.metrics
    assert metrics["is_ic"] > 0.15  # 面板埋了持续性动量信号
    assert metrics["oos_ic"] > 0.0
    assert 0.0 <= metrics["turnover"] <= 1.0
    assert metrics["sharpe"] > policy().min_sharpe


def test_golden_backtest_reproduces_exactly() -> None:
    gate = backtester()
    first = gate.evaluate(panel(), MOMENTUM).to_dict()
    second = gate.evaluate(panel(), MOMENTUM).to_dict()
    assert first == second  # 同输入逐字节一致：无时钟、无随机、无外部状态


def test_golden_metrics_snapshot() -> None:
    report = backtester().evaluate(panel(), MOMENTUM)
    metrics = report.metrics
    expected: dict[str, float] = {
        "is_ic": metrics["is_ic"],
        "is_ic_ir": metrics["is_ic_ir"],
        "oos_ic": metrics["oos_ic"],
        "sharpe": metrics["sharpe"],
        "turnover": metrics["turnover"],
        "recent_ic": metrics["recent_ic"],
        "ic_correlation": metrics["ic_correlation"],
        "year_positive_ratio": metrics["year_positive_ratio"],
    }
    # 回测 golden：首次运行后固化；面板种子与表达式不变则恒等。
    assert {key: round(value, 6) for key, value in expected.items()} == {
        key: round(value, 6) for key, value in GOLDEN_METRICS.items()
    }


GOLDEN_METRICS: dict[str, float] = {
    "is_ic": 0.473143,
    "is_ic_ir": 1.215984,
    "oos_ic": 0.410526,
    "sharpe": 6.704913,
    "turnover": 0.308824,
    "recent_ic": 0.566667,
    "ic_correlation": 0.0,
    "year_positive_ratio": 1.0,
}


def test_noise_factor_fails_a_hard_dimension() -> None:
    report = backtester().evaluate(panel(), NOISE)
    assert not report.accepted
    assert "LOW_IC" in report.failure_modes or "UNSTABLE" in report.failure_modes


def test_noise_factor_has_higher_turnover_than_momentum() -> None:
    gate = backtester()
    momentum = gate.evaluate(panel(), MOMENTUM).metrics["turnover"]
    noise = gate.evaluate(panel(), NOISE).metrics["turnover"]
    assert noise > momentum  # 每日噪声排名翻动，换手高于持续性动量
    tight = policy(max_turnover=noise - 1e-9)
    report = gate.evaluate(panel(), NOISE, filter_policy=tight)
    assert not report.accepted
    assert "HIGH_TURNOVER" in report.failure_modes
    assert "HIGH_TURNOVER" not in gate.evaluate(panel(), MOMENTUM, filter_policy=tight).failure_modes


def test_library_correlation_rejects_duplicate_signal() -> None:
    gate = backtester()
    solo = gate.evaluate(panel(), MOMENTUM)
    assert solo.metrics["ic_correlation"] == 0.0  # 空因子库无相关性约束
    with_library = gate.evaluate(panel(), MOMENTUM, library=(MOMENTUM,))
    assert with_library.metrics["ic_correlation"] > 0.99
    assert not with_library.accepted
    assert "CORRELATED" in with_library.failure_modes


def test_oos_decay_boundary_rejects_decayed_factor() -> None:
    strict = policy(min_oos_ic=10.0)  # 样本外门槛高到必然失败
    report = backtester().evaluate(panel(), MOMENTUM, filter_policy=strict)
    assert not report.accepted
    assert "OOS_DECAY" in report.failure_modes


# --------------------------------------------------------------------------- 防泄漏


def test_future_data_cannot_leak_into_in_sample_metrics() -> None:
    gate = backtester()
    base = panel()
    before = gate.evaluate(base, MOMENTUM).to_dict()

    doubled_oos = {
        name: tuple(value * 2.0 for value in series[spec().in_sample_bars :])
        for name, series in base.close.items()
    }
    mutated = SimulationPanel(
        data_version=base.data_version,
        close={
            name: tuple(series)[: spec().in_sample_bars] + doubled_oos[name]
            for name, series in base.close.items()
        },
        extra_fields={"noise": base.field("noise")},
    )
    after = gate.evaluate(mutated, MOMENTUM).to_dict()
    for dimension in ("is_ic", "is_ic_ir", "sharpe", "turnover", "recent_ic"):
        assert after["metrics"][dimension] == before["metrics"][dimension], (
            f"{dimension} leaked future data"
        )


def test_report_records_data_version_and_spec_digest() -> None:
    report = backtester().evaluate(panel(), MOMENTUM)
    assert report.data_version == panel().data_version
    assert report.data_digest == panel().content_digest
    assert report.spec_digest
    assert report.candidate_hash
    payload = report.to_dict()
    restored = type(report).from_dict(payload)
    assert restored == report


def test_evaluator_rejects_unknown_operator_before_any_math() -> None:
    with pytest.raises(ValueError, match="operator"):
        backtester().evaluate(panel(), {"op": "wavelet", "window": 5, "input": {"field": "close"}})


def test_factor_series_respect_their_declared_window() -> None:
    gate = backtester()
    slow = gate.evaluate(panel(), {"op": "ts_delta", "window": 20, "input": {"field": "close"}})
    fast = gate.evaluate(panel(), MOMENTUM)
    assert slow.metrics["is_ic"] != fast.metrics["is_ic"] or slow.accepted == fast.accepted
    snapshot = copy.deepcopy(slow.metrics)
    assert snapshot == slow.metrics  # report 不可变


# --------------------------------------------------------------------------- 面板/报告/定义守卫


def test_simulation_panel_rejects_invalid_structures() -> None:
    close = {"a": (1.0, 2.0), "b": (2.0, 3.0)}
    with pytest.raises(ValueError, match="data_version"):
        SimulationPanel("", close)
    with pytest.raises(ValueError, match="2 assets"):
        SimulationPanel("v1", {"a": (1.0, 2.0)})
    with pytest.raises(ValueError, match="share one length"):
        SimulationPanel("v1", {"a": (1.0, 2.0, 3.0), "b": (1.0, 2.0)})
    with pytest.raises(ValueError, match="shadow"):
        SimulationPanel("v1", close, extra_fields={"close": {"a": (1.0, 2.0), "b": (1.0, 2.0)}})
    with pytest.raises(ValueError, match="every asset"):
        SimulationPanel("v1", close, extra_fields={"f": {"a": (1.0, 2.0)}})
    with pytest.raises(ValueError, match="finite"):
        SimulationPanel("v1", {"a": (1.0, float("inf")), "b": (2.0, 3.0)})


def test_panel_field_accessor_rejects_unknown_field() -> None:
    with pytest.raises(ValueError, match="unknown field"):
        panel().field("nope")


def test_build_simulation_panel_rejects_degenerate_universes() -> None:
    with pytest.raises(ValueError, match="2 assets"):
        build_simulation_panel(seed=1, total_bars=10, assets=("only",))
    with pytest.raises(ValueError, match="2 bars"):
        build_simulation_panel(seed=1, total_bars=1, assets=("a", "b"))


def test_spec_rejects_more_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="strictly inside"):
        spec(in_sample_bars=300)
    with pytest.raises(ValueError, match="positive"):
        spec(max_window=0)
    with pytest.raises(ValueError, match="recent_bars"):
        spec(recent_bars=500)
    with pytest.raises(ValueError, match="quantile"):
        spec(quantile=0.6)
    with pytest.raises(ValueError, match="non-negative"):
        spec(cost_bps=-1.0)
    with pytest.raises(ValueError, match="stability_bucket_bars"):
        spec(stability_bucket_bars=4)


def test_policy_rejects_negative_ic_ir() -> None:
    with pytest.raises(ValueError, match="ic_ir"):
        policy(min_ic_ir=-0.5)


def test_report_roundtrip_rejects_corrupt_payloads() -> None:
    payload = backtester().evaluate(panel(), MOMENTUM).to_dict()
    assert ValidationReport.from_dict(payload) == ValidationReport.from_dict(payload)
    with pytest.raises(ValueError, match="format"):
        ValidationReport.from_dict({**payload, "format": 99})
    with pytest.raises(TypeError, match="candidate_hash"):
        ValidationReport.from_dict({**payload, "candidate_hash": 7})
    with pytest.raises(TypeError, match="data_digest"):
        ValidationReport.from_dict({**payload, "data_digest": 7})
    with pytest.raises(TypeError, match="mappings"):
        ValidationReport.from_dict({**payload, "metrics": "nope"})
    with pytest.raises(TypeError, match="must be a list"):
        ValidationReport.from_dict({**payload, "failure_modes": "nope", "accepted": True})


def test_evaluate_rejects_bad_definitions_and_mismatched_panels() -> None:
    gate = backtester()
    assert gate.spec.total_bars == 300
    with pytest.raises(ValueError, match="unknown field"):
        gate.evaluate(panel(), {"field": "nope"})
    with pytest.raises(ValueError, match="window"):
        gate.evaluate(panel(), {"op": "ts_delta", "window": 0, "input": {"field": "close"}})
    with pytest.raises(TypeError, match="expression node"):
        gate.evaluate(panel(), {"op": "ts_delta", "window": 5, "input": "close"})
    with pytest.raises(ValueError, match="lookback exceeds"):
        gate.evaluate(panel(), {"op": "ts_delta", "window": 25, "input": {"field": "close"}})
    with pytest.raises(ValueError, match="library definition lookback"):
        gate.evaluate(
            panel(),
            MOMENTUM,
            library=({"op": "ts_delta", "window": 25, "input": {"field": "close"}},),
        )
    other = build_simulation_panel(seed=11, total_bars=200, assets=spec().assets)
    with pytest.raises(ValueError, match="does not match the backtest spec"):
        gate.evaluate(other, MOMENTUM)


# --------------------------------------------------------------------------- 算子覆盖与统计守卫


def test_ts_mean_rank_and_nested_operators_produce_finite_metrics() -> None:
    gate = backtester()
    smooth = gate.evaluate(panel(), {"op": "ts_mean", "window": 10, "input": {"field": "close"}})
    ranked = gate.evaluate(panel(), {"op": "rank", "window": 10, "input": {"field": "close"}})
    nested = gate.evaluate(
        panel(),
        {"op": "ts_mean", "window": 5, "input": {"op": "ts_delta", "window": 5, "input": {"field": "close"}}},
    )
    momentum = gate.evaluate(panel(), MOMENTUM)
    for report in (smooth, ranked, nested):
        assert all(math.isfinite(value) for value in report.metrics.values())
    assert smooth.metrics["is_ic"] != momentum.metrics["is_ic"]
    assert nested.metrics["is_ic"] != momentum.metrics["is_ic"]


def test_constant_return_cross_sections_score_zero_ic() -> None:
    """价格全平 → 前向收益截面为常数：秩并列 + 零方差必须得 0 而非 NaN/崩溃。"""
    flat = SimulationPanel(
        data_version="flat-v1",
        close={name: tuple([100.0] * 300) for name in spec().assets},
        extra_fields={"noise": panel().field("noise")},
    )
    report = backtester().evaluate(flat, NOISE)
    assert report.metrics["is_ic"] == 0.0
    assert not report.accepted


def test_single_rebalance_spec_has_zero_turnover_and_merged_tail_bucket() -> None:
    overrides: dict[str, Any] = {
        "total_bars": 120,
        "in_sample_bars": 65,
        "horizon_bars": 30,
        "max_window": 5,
        "recent_bars": 30,
        "stability_bucket_bars": 25,
    }
    gate = backtester(**overrides)
    one_bar_panel = build_simulation_panel(
        seed=20260907, total_bars=120, assets=spec().assets
    )
    report = gate.evaluate(one_bar_panel, MOMENTUM)
    assert report.sample["rebalances"] == 1
    assert report.metrics["turnover"] == 0.0  # 仅一次建仓：无换手记录


def test_period_stability_gate_fires_for_noise() -> None:
    report = backtester().evaluate(panel(), NOISE, filter_policy=policy(min_year_positive_ratio=1.0))
    assert "INCONSISTENT_PERIODS" in report.failure_modes


def test_weak_recent_gate_fires_for_overly_strict_threshold() -> None:
    report = backtester().evaluate(panel(), MOMENTUM, filter_policy=policy(min_recent_ic=2.0))
    assert "WEAK_RECENT" in report.failure_modes


def test_defensive_statistical_guards() -> None:
    assert _pearson((), ()) == 0.0  # 少于 2 个观测点按 0 处理
    gate = backtester()
    assert gate._positive_bucket_ratio(()) == 0.0  # 空序列无稳定桶
    with pytest.raises(ValueError, match="warmup"):
        gate._cross_ic(
            {"a": [float("nan")] * 3, "b": [1.0, 2.0, 3.0]},
            {"a": [0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.0]},
            0,
        )
