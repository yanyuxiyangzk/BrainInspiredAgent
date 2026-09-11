"""Hardcoded deterministic backtest with multi-dimensional filters (L-007).

架构 §3.3：验证是确定性硬门槛，不依赖模型自我评价——固定数据版本、样本区间、
换仓频率、成本、滑点和基准；计算 IC、周期稳定性、风险调整收益、近期持续性、
换手和独立性；多项联合过滤与 IC 相关性去重；通过入库，否则进入失败模式库。

本模块是纯确定性库：无时钟、无 IO、无随机（合成面板由固定种子生成并版本化、
内容寻址）。样本边界不可穿越：样本内评估点的因子值与前瞻收益只使用
``t < in_sample_bars`` 的数据；样本外只用于度量，不参与任何阈值拟合。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import cast

from domain_sdk.factor_generation import candidate_hash

_REPORT_FORMAT = 1
_MIN_TOTAL_BARS = 100
_MIN_IN_SAMPLE_POINTS = 30
_TRADING_DAYS = 252
_EPSILON = 1e-12

FAILURE_LOW_IC = "LOW_IC"
FAILURE_UNSTABLE = "UNSTABLE"
FAILURE_INCONSISTENT_PERIODS = "INCONSISTENT_PERIODS"
FAILURE_LOW_SHARPE = "LOW_SHARPE"
FAILURE_WEAK_RECENT = "WEAK_RECENT"
FAILURE_HIGH_TURNOVER = "HIGH_TURNOVER"
FAILURE_CORRELATED = "CORRELATED"
FAILURE_OOS_DECAY = "OOS_DECAY"


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


# --------------------------------------------------------------------------- 数据面板


@dataclass(frozen=True, slots=True)
class SimulationPanel:
    """版本化、内容寻址的行情面板：``close`` 与可选附加字段，按资产存放等长序列。"""

    data_version: str
    close: Mapping[str, tuple[float, ...]]
    extra_fields: Mapping[str, Mapping[str, tuple[float, ...]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.data_version:
            raise ValueError("data_version must be non-empty")
        close = {name: tuple(float(v) for v in series) for name, series in sorted(self.close.items())}
        if len(close) < 2:
            raise ValueError("panel needs at least 2 assets")
        lengths = {len(series) for series in close.values()}
        if len(lengths) != 1 or next(iter(lengths)) < 2:
            raise ValueError("all close series must share one length of at least 2 bars")
        extras: dict[str, dict[str, tuple[float, ...]]] = {}
        for field_name, per_asset in sorted(self.extra_fields.items()):
            if field_name == "close":
                raise ValueError("extra_fields must not shadow close")
            normalized = {
                name: tuple(float(v) for v in series) for name, series in sorted(per_asset.items())
            }
            if set(normalized) != set(close) or any(
                len(series) != len(next(iter(close.values()))) for series in normalized.values()
            ):
                raise ValueError(f"field {field_name!r} must cover every asset with equal length")
            extras[field_name] = normalized
        for series in list(close.values()) + [s for f in extras.values() for s in f.values()]:
            if any(not math.isfinite(v) for v in series):
                raise ValueError("panel values must be finite")
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "extra_fields", extras)

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(self.close)

    @property
    def total_bars(self) -> int:
        return len(next(iter(self.close.values())))

    @property
    def fields(self) -> tuple[str, ...]:
        return ("close", *self.extra_fields)

    def field(self, name: str) -> Mapping[str, tuple[float, ...]]:
        if name == "close":
            return self.close
        if name not in self.extra_fields:
            raise ValueError(f"unknown field {name!r}")
        return self.extra_fields[name]

    @property
    def content_digest(self) -> str:
        return _canonical_digest(
            {
                "data_version": self.data_version,
                "close": {name: list(series) for name, series in self.close.items()},
                "extra_fields": {
                    field_name: {name: list(series) for name, series in per_asset.items()}
                    for field_name, per_asset in self.extra_fields.items()
                },
            }
        )


def build_simulation_panel(
    *,
    seed: int,
    total_bars: int,
    assets: Sequence[str],
    data_version: str | None = None,
) -> SimulationPanel:
    """固定种子的合成面板：共同市场因子 + 持续性个股 alpha（AR(1)）+ 独立噪声。

    ``noise`` 附加字段是与收益无关的纯噪声，供验收测试构造"必然失败"的候选。
    """
    names = tuple(sorted(set(assets)))
    if len(names) < 2:
        raise ValueError("simulation needs at least 2 assets")
    if total_bars < 2:
        raise ValueError("simulation needs at least 2 bars")
    rng = random.Random(seed)
    alpha = dict.fromkeys(names, 0.0)
    closes: dict[str, list[float]] = {name: [100.0] for name in names}
    for _ in range(1, total_bars):
        market = rng.gauss(0.0, 0.008)
        for name in names:
            alpha[name] = 0.92 * alpha[name] + rng.gauss(0.0, 0.003)
            daily = max(market + alpha[name] + rng.gauss(0.0, 0.005), -0.5)
            closes[name].append(closes[name][-1] * (1.0 + daily))
    noise = {name: tuple(rng.gauss(0.0, 1.0) for _ in range(total_bars)) for name in names}
    version = data_version or f"sim-{total_bars}b-{len(names)}a-s{seed}-v1"
    return SimulationPanel(
        data_version=version,
        close={name: tuple(series) for name, series in closes.items()},
        extra_fields={"noise": noise},
    )


# --------------------------------------------------------------------------- 契约


@dataclass(frozen=True, slots=True)
class BacktestSpec:
    """硬编码回测参数：样本切分、前瞻/换仓周期、成本、滑点与截面分位。"""

    assets: tuple[str, ...]
    total_bars: int
    in_sample_bars: int
    max_window: int = 20
    horizon_bars: int = 5
    cost_bps: float = 15.0
    slippage_bps: float = 10.0
    quantile: float = 0.5
    recent_bars: int = 60
    stability_bucket_bars: int = 250
    min_oos_points: int = 20

    def __post_init__(self) -> None:
        if len(self.assets) < 2 or len(set(self.assets)) != len(self.assets):
            raise ValueError("assets must contain at least 2 unique names")
        if self.total_bars < _MIN_TOTAL_BARS:
            raise ValueError(f"total_bars must cover at least {_MIN_TOTAL_BARS} bars")
        if not 0 < self.in_sample_bars < self.total_bars:
            raise ValueError("in_sample_bars must lie strictly inside total_bars")
        if self.max_window < 1 or self.horizon_bars < 1:
            raise ValueError("max_window and horizon_bars must be positive")
        if self.in_sample_points < _MIN_IN_SAMPLE_POINTS:
            raise ValueError(
                "in-sample must leave room for warmup (max_window + horizon) and at least "
                f"{_MIN_IN_SAMPLE_POINTS} evaluation bars"
            )
        if self.min_oos_points < 1 or self.oos_points < self.min_oos_points:
            raise ValueError("out-of-sample must keep at least min_oos_points evaluation bars")
        if not 1 <= self.recent_bars <= self.in_sample_points:
            raise ValueError("recent_bars must fit inside the in-sample evaluation window")
        if not 0.0 < self.quantile <= 0.5:
            raise ValueError("quantile must stay within (0, 0.5]")
        if self.cost_bps < 0.0 or self.slippage_bps < 0.0:
            raise ValueError("cost and slippage must be non-negative")
        if self.stability_bucket_bars < 5:
            raise ValueError("stability_bucket_bars must be at least 5")
        object.__setattr__(self, "assets", tuple(self.assets))

    @property
    def in_sample_range(self) -> tuple[int, int]:
        """样本内评估点 ``[start, end)``：前瞻收益 ``t + horizon`` 恒 < in_sample_bars。"""
        return (self.max_window, self.in_sample_bars - self.horizon_bars)

    @property
    def oos_range(self) -> tuple[int, int]:
        return (self.in_sample_bars, self.total_bars - self.horizon_bars)

    @property
    def in_sample_points(self) -> int:
        start, end = self.in_sample_range
        return end - start

    @property
    def oos_points(self) -> int:
        start, end = self.oos_range
        return end - start

    @property
    def digest(self) -> str:
        return _canonical_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    """多维联合过滤阈值；任何一维不达标即拒绝并记录失败模式。"""

    min_ic: float = 0.02
    min_ic_ir: float = 0.3
    min_year_positive_ratio: float = 0.6
    min_sharpe: float = 0.5
    min_recent_ic: float = 0.0
    max_ic_correlation: float = 0.7
    min_oos_ic: float = 0.0
    min_oos_ratio: float = 0.5
    max_turnover: float = 1.0

    def __post_init__(self) -> None:
        if self.min_ic < 0.0:
            raise ValueError("min_ic must be non-negative")
        if self.min_ic_ir < 0.0:
            raise ValueError("min_ic_ir must be non-negative")
        if not 0.0 <= self.min_year_positive_ratio <= 1.0:
            raise ValueError("min_year_positive_ratio must stay within [0, 1]")
        if not 0.0 <= self.max_ic_correlation <= 1.0:
            raise ValueError("max_ic_correlation must stay within [0, 1]")
        if self.min_oos_ratio < 0.0:
            raise ValueError("min_oos_ratio must be non-negative")
        if not 0.0 < self.max_turnover <= 1.0:
            raise ValueError("max_turnover must stay within (0, 1]")

    @property
    def digest(self) -> str:
        return _canonical_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """一次硬编码回测的完整证据：数据版本/digest、参数 digest、指标、失败模式。"""

    candidate_hash: str
    data_version: str
    data_digest: str
    spec_digest: str
    policy_digest: str
    metrics: Mapping[str, float]
    sample: Mapping[str, int]
    failure_modes: tuple[str, ...]
    accepted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "format": _REPORT_FORMAT,
            "candidate_hash": self.candidate_hash,
            "data_version": self.data_version,
            "data_digest": self.data_digest,
            "spec_digest": self.spec_digest,
            "policy_digest": self.policy_digest,
            "metrics": dict(self.metrics),
            "sample": dict(self.sample),
            "failure_modes": list(self.failure_modes),
            "accepted": self.accepted,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ValidationReport:
        if payload.get("format") != _REPORT_FORMAT:
            raise ValueError("unsupported validation report format")
        for key in ("candidate_hash", "data_version", "data_digest", "spec_digest", "policy_digest"):
            if not isinstance(payload.get(key), str):
                raise TypeError(f"report {key} must be a string")
        metrics = payload.get("metrics")
        sample = payload.get("sample")
        modes = payload.get("failure_modes")
        accepted = payload.get("accepted")
        if not isinstance(metrics, Mapping) or not isinstance(sample, Mapping):
            raise TypeError("report metrics and sample must be mappings")
        if not isinstance(modes, list) or not isinstance(accepted, bool):
            raise TypeError("report failure_modes must be a list and accepted a bool")
        return cls(
            candidate_hash=cast(str, payload["candidate_hash"]),
            data_version=cast(str, payload["data_version"]),
            data_digest=cast(str, payload["data_digest"]),
            spec_digest=cast(str, payload["spec_digest"]),
            policy_digest=cast(str, payload["policy_digest"]),
            metrics={str(k): float(cast(float, v)) for k, v in metrics.items()},
            sample={str(k): int(cast(int, v)) for k, v in sample.items()},
            failure_modes=tuple(str(mode) for mode in modes),
            accepted=accepted,
        )


# --------------------------------------------------------------------------- 表达式求值


def _lookback(definition: Mapping[str, object]) -> int:
    """表达式链的总回看长度：决定因子在 t 最早可用的历史深度。"""
    if "field" in definition:
        return 0
    window = cast(int, definition["window"])
    child = cast(Mapping[str, object], definition["input"])
    own = window if definition["op"] == "ts_delta" else window - 1
    return own + _lookback(child)


def _check_definition(definition: Mapping[str, object], fields: Sequence[str]) -> None:
    if "field" in definition:
        if definition["field"] not in fields:
            raise ValueError(f"unknown field {definition['field']!r}")
        return
    operator = definition.get("op")
    if operator not in ("ts_mean", "ts_delta", "rank"):
        raise ValueError(f"unknown operator {operator!r}")
    window = definition.get("window")
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise ValueError("window must be a positive integer")
    child = definition.get("input")
    if not isinstance(child, Mapping):
        raise TypeError("operator input must be an expression node")
    _check_definition(child, fields)


def _evaluate_series(
    definition: Mapping[str, object], panel: SimulationPanel, asset: str
) -> list[float]:
    """按资产求值为等长序列；历史不足处为 NaN（仅出现在 warmup 区）。"""
    if "field" in definition:
        return list(panel.field(str(definition["field"]))[asset])
    window = cast(int, definition["window"])
    child = _evaluate_series(cast(Mapping[str, object], definition["input"]), panel, asset)
    operator = definition["op"]
    output = [math.nan] * len(child)
    for t in range(len(child)):
        start = t - window + 1 if operator != "ts_delta" else t - window
        if start < 0:
            continue
        segment = child[start : t + 1]
        if any(math.isnan(v) for v in segment):
            continue
        if operator == "ts_mean":
            output[t] = sum(segment) / window
        elif operator == "ts_delta":
            output[t] = child[t] - child[t - window]
        else:  # rank：窗口内时序分位（ts_rank），输出无量纲
            current = child[t]
            below = sum(1 for v in segment if v < current)
            ties = sum(1 for v in segment if v == current) - 1
            output[t] = (below + 0.5 * ties) / (window - 1) if window > 1 else 0.5
    return output


# --------------------------------------------------------------------------- 统计工具


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x < _EPSILON or var_y < _EPSILON:
        return 0.0
    return cov / math.sqrt(var_x * var_y)


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    return _pearson(_average_ranks(xs), _average_ranks(ys))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > _EPSILON else 0.0


# --------------------------------------------------------------------------- 回测器


class FactorBacktester:
    """确定性硬门槛：同一 (面板, 定义, 参数, 因子库) 永远得到同一份报告。"""

    def __init__(self, spec: BacktestSpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> BacktestSpec:
        return self._spec

    def evaluate(
        self,
        panel: SimulationPanel,
        definition: Mapping[str, object],
        *,
        filter_policy: FilterPolicy | None = None,
        library: Sequence[Mapping[str, object]] = (),
    ) -> ValidationReport:
        spec = self._spec
        policy = filter_policy if filter_policy is not None else FilterPolicy()
        if panel.total_bars != spec.total_bars or panel.assets != tuple(sorted(spec.assets)):
            raise ValueError("panel does not match the backtest spec (bars or assets differ)")
        _check_definition(definition, panel.fields)
        if _lookback(definition) > spec.max_window:
            raise ValueError("definition lookback exceeds spec max_window")
        for entry in library:
            _check_definition(entry, panel.fields)
            if _lookback(entry) > spec.max_window:
                raise ValueError("library definition lookback exceeds spec max_window")

        factor = self._factor_matrix(panel, definition)
        forward = self._forward_returns(panel)
        is_start, is_end = spec.in_sample_range
        oos_start, oos_end = spec.oos_range
        is_ic = [self._cross_ic(factor, forward, t) for t in range(is_start, is_end)]
        oos_ic = [self._cross_ic(factor, forward, t) for t in range(oos_start, oos_end)]

        sharpe, turnover, rebalances = self._long_short(factor, forward, is_start, is_end)
        correlation = 0.0
        for entry in library:
            other = self._factor_matrix(panel, entry)
            other_ic = [self._cross_ic(other, forward, t) for t in range(is_start, is_end)]
            correlation = max(correlation, abs(_pearson(is_ic, other_ic)))

        mean_is = _mean(is_ic)
        mean_oos = _mean(oos_ic)
        metrics: dict[str, float] = {
            "is_ic": mean_is,
            "is_ic_std": _std(is_ic),
            "is_ic_ir": _ratio(mean_is, _std(is_ic)),
            "year_positive_ratio": self._positive_bucket_ratio(is_ic),
            "sharpe": sharpe,
            "turnover": turnover,
            "recent_ic": _mean(is_ic[-spec.recent_bars :]),
            "ic_correlation": correlation,
            "oos_ic": mean_oos,
            "oos_ic_std": _std(oos_ic),
        }
        failures = self._failure_modes(metrics, policy)
        return ValidationReport(
            candidate_hash=candidate_hash(definition),
            data_version=panel.data_version,
            data_digest=panel.content_digest,
            spec_digest=spec.digest,
            policy_digest=policy.digest,
            metrics=metrics,
            sample={
                "is_start": is_start,
                "is_end": is_end,
                "oos_start": oos_start,
                "oos_end": oos_end,
                "is_points": len(is_ic),
                "oos_points": len(oos_ic),
                "rebalances": rebalances,
                "library_size": len(library),
            },
            failure_modes=tuple(failures),
            accepted=not failures,
        )

    # ----------------------------------------------------------------- 内部

    def _factor_matrix(
        self, panel: SimulationPanel, definition: Mapping[str, object]
    ) -> dict[str, list[float]]:
        return {asset: _evaluate_series(definition, panel, asset) for asset in panel.assets}

    def _forward_returns(self, panel: SimulationPanel) -> dict[str, list[float]]:
        horizon = self._spec.horizon_bars
        forward: dict[str, list[float]] = {}
        for asset, series in panel.close.items():
            forward[asset] = [
                series[t + horizon] / series[t] - 1.0 if t + horizon < len(series) else math.nan
                for t in range(len(series))
            ]
        return forward

    @staticmethod
    def _cross_ic(
        factor: Mapping[str, Sequence[float]], forward: Mapping[str, Sequence[float]], t: int
    ) -> float:
        xs = [factor[asset][t] for asset in factor]
        ys = [forward[asset][t] for asset in factor]
        if any(math.isnan(v) for v in xs) or any(math.isnan(v) for v in ys):
            raise ValueError(f"evaluation point {t} falls inside the warmup region")
        return _spearman(xs, ys)

    def _long_short(
        self,
        factor: Mapping[str, Sequence[float]],
        forward: Mapping[str, Sequence[float]],
        start: int,
        end: int,
    ) -> tuple[float, float, int]:
        """多空分位组合：按 horizon 换仓，扣除成本与滑点；返回 (Sharpe, 平均换手, 换仓次数)。"""
        spec = self._spec
        assets = tuple(factor)
        leg = max(1, min(int(len(assets) * spec.quantile), len(assets) // 2))
        friction = (spec.cost_bps + spec.slippage_bps) / 10_000.0
        weights = dict.fromkeys(assets, 0.0)
        period_returns: list[float] = []
        turnovers: list[float] = []
        rebalances = 0
        for t in range(start, end, spec.horizon_bars):
            ordered = sorted(assets, key=lambda a: (factor[a][t], a))
            target = dict.fromkeys(assets, 0.0)
            for asset in ordered[-leg:]:
                target[asset] = 1.0 / leg
            for asset in ordered[:leg]:
                target[asset] = -1.0 / leg
            traded = sum(abs(target[a] - weights[a]) for a in assets)
            if rebalances > 0:
                turnovers.append(traded / 4.0)  # 归一到 [0, 1]：多空整体翻转记 1
            gross = sum(target[a] * forward[a][t] for a in assets)
            period_returns.append(gross - friction * traded)
            weights = target
            rebalances += 1
        periods_per_year = _TRADING_DAYS / spec.horizon_bars
        sharpe = _ratio(_mean(period_returns), _std(period_returns)) * math.sqrt(periods_per_year)
        return sharpe, _mean(turnovers), rebalances

    def _positive_bucket_ratio(self, ic_series: Sequence[float]) -> float:
        """周期稳定性：按固定长度分桶，统计均值为正的桶占比；尾桶不足半桶并入前桶。"""
        size = self._spec.stability_bucket_bars
        buckets: list[list[float]] = []
        for i in range(0, len(ic_series), size):
            chunk = list(ic_series[i : i + size])
            if buckets and len(chunk) < size / 2:
                buckets[-1].extend(chunk)
            else:
                buckets.append(chunk)
        if not buckets:
            return 0.0
        positive = sum(1 for chunk in buckets if _mean(chunk) > 0.0)
        return positive / len(buckets)

    @staticmethod
    def _failure_modes(metrics: Mapping[str, float], policy: FilterPolicy) -> list[str]:
        failures: list[str] = []
        if metrics["is_ic"] < policy.min_ic:
            failures.append(FAILURE_LOW_IC)
        if metrics["is_ic_ir"] < policy.min_ic_ir:
            failures.append(FAILURE_UNSTABLE)
        if metrics["year_positive_ratio"] < policy.min_year_positive_ratio:
            failures.append(FAILURE_INCONSISTENT_PERIODS)
        if metrics["sharpe"] < policy.min_sharpe:
            failures.append(FAILURE_LOW_SHARPE)
        if metrics["recent_ic"] < policy.min_recent_ic:
            failures.append(FAILURE_WEAK_RECENT)
        if metrics["turnover"] > policy.max_turnover:
            failures.append(FAILURE_HIGH_TURNOVER)
        if metrics["ic_correlation"] > policy.max_ic_correlation:
            failures.append(FAILURE_CORRELATED)
        decayed = metrics["oos_ic"] < policy.min_oos_ic or (
            metrics["is_ic"] > 0.0 and metrics["oos_ic"] < policy.min_oos_ratio * metrics["is_ic"]
        )
        if decayed:
            failures.append(FAILURE_OOS_DECAY)
        return failures
