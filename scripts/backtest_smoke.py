"""Post-install smoke for L-007: hardcoded backtest gate, data versions, no leakage."""
import copy

from domain_sdk.factor_backtest import (
    BacktestSpec,
    FactorBacktester,
    SimulationPanel,
    build_simulation_panel,
)

ASSETS = ("alpha-1", "alpha-2", "alpha-3", "alpha-4")
SPEC = BacktestSpec(assets=ASSETS, total_bars=300, in_sample_bars=200, max_window=20)
MOMENTUM = {"op": "ts_delta", "window": 5, "input": {"field": "close"}}
SEED = 20260907


def smoke() -> None:
    gate = FactorBacktester(SPEC)
    base = build_simulation_panel(seed=SEED, total_bars=300, assets=ASSETS)
    again = build_simulation_panel(seed=SEED, total_bars=300, assets=ASSETS)
    assert base.content_digest == again.content_digest, "panel must be reproducible"

    first = gate.evaluate(base, MOMENTUM)
    assert first.accepted, f"momentum factor rejected: {first.failure_modes}"
    assert first.metrics["is_ic"] > 0.15, "seeded momentum signal missing"
    assert first.metrics["oos_ic"] > 0.0, "out-of-sample confirmation missing"
    assert gate.evaluate(base, MOMENTUM).to_dict() == first.to_dict(), "backtest not deterministic"

    # 防泄漏：篡改样本外价格不得改变任何样本内指标
    mutated = SimulationPanel(
        data_version=base.data_version,
        close={
            name: series[: SPEC.in_sample_bars]
            + tuple(value * 2.0 for value in series[SPEC.in_sample_bars :])
            for name, series in base.close.items()
        },
        extra_fields={"noise": base.field("noise")},
    )
    after = gate.evaluate(mutated, MOMENTUM)
    assert after.data_digest != first.data_digest, "data version must track content"
    for dimension in ("is_ic", "is_ic_ir", "sharpe", "turnover", "recent_ic"):
        assert after.metrics[dimension] == first.metrics[dimension], (
            f"{dimension} leaked future data"
        )

    # 多维过滤：噪声候选必然失败，且有失败模式证据
    noise = gate.evaluate(base, {"field": "noise"})
    assert not noise.accepted and noise.failure_modes, "noise factor must fail a hard gate"

    # 独立性：与因子库相同信号的相关性去重
    with_library = gate.evaluate(base, MOMENTUM, library=(MOMENTUM,))
    assert with_library.metrics["ic_correlation"] > 0.99
    assert not with_library.accepted and "CORRELATED" in with_library.failure_modes

    snapshot = copy.deepcopy(first.metrics)
    assert snapshot == first.metrics
    print(
        "WSL packaging smoke PASS: is_ic",
        round(first.metrics["is_ic"], 4),
        "oos_ic",
        round(first.metrics["oos_ic"], 4),
        "turnover",
        round(first.metrics["turnover"], 4),
        "noise_modes",
        ",".join(noise.failure_modes),
    )


smoke()
