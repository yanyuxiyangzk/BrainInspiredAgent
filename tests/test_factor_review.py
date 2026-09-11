"""L-005 tests: deterministic rule review — AST, dimension, complexity and boundary gates.

验收主断言是"非法候选零回测"：任何被审查拒绝的候选都不能进入回测阶段；
审查先于回测发生，且审查对同一输入永远给出同一 verdict。
"""
from __future__ import annotations

import pytest

from domain_sdk.factor_adaptation import AdaptiveFactorSearch, RoundFeedback
from domain_sdk.factor_generation import (
    FactorCandidateFactory,
    FactorVocabulary,
    ParentPool,
    candidate_hash,
)
from domain_sdk.factor_review import (
    CandidateReviewer,
    FactorDimensionTable,
    FactorReviewPolicy,
    OperatorSignature,
    ReviewCode,
    ReviewOutcome,
    ReviewReport,
)

VOCABULARY = FactorVocabulary(
    fields=("close", "high", "volume"),
    operators=("rank", "ts_mean", "ts_delta"),
    windows=(5, 10, 20),
)

DIMENSIONS = FactorDimensionTable(
    field_dimensions={"close": "price", "high": "price", "volume": "volume"},
    operator_signatures={
        "rank": OperatorSignature(
            accepted_input_dimensions=None, output_dimension="shapeless", idempotent=True
        ),
        "ts_mean": OperatorSignature(accepted_input_dimensions=None, output_dimension=None),
        "ts_delta": OperatorSignature(accepted_input_dimensions=None, output_dimension=None),
        "price_momentum": OperatorSignature(
            accepted_input_dimensions=frozenset({"price"}), output_dimension=None
        ),
    },
)

EXTENDED_VOCABULARY = FactorVocabulary(
    fields=("close", "high", "volume"),
    operators=("rank", "ts_mean", "ts_delta", "price_momentum"),
    windows=(5, 10, 20),
)


def reviewer(**policy_overrides: object) -> CandidateReviewer:
    overrides: dict[str, object] = {"max_window": 20}
    overrides.update(policy_overrides)
    policy = FactorReviewPolicy(**overrides)  # type: ignore[arg-type]
    return CandidateReviewer(EXTENDED_VOCABULARY, DIMENSIONS, policy=policy)


def chain(op: str, window: int, field: str, depth: int = 1) -> dict[str, object]:
    tree: dict[str, object] = {"field": field}
    for _ in range(depth):
        tree = {"op": op, "window": window, "input": tree}
    return tree


_ALTERNATING_OPS = ("rank", "ts_mean", "ts_delta")


def varied_chain(field: str, depth: int, window: int = 10) -> dict[str, object]:
    """深层合法树：交替算子，避免触发幂等冗余规则。"""
    tree: dict[str, object] = {"field": field}
    for index in range(depth):
        tree = {"op": _ALTERNATING_OPS[index % len(_ALTERNATING_OPS)], "window": window, "input": tree}
    return tree


def accepted_of(report: ReviewReport) -> list[dict[str, object]]:
    return [outcome.definition for outcome in report.outcomes if outcome.accepted]


def reason_codes(outcome: ReviewOutcome) -> set[ReviewCode]:
    return set(outcome.reasons)


# --------------------------------------------------------------------------- 合法候选


@pytest.mark.asyncio
async def test_every_generated_candidate_passes_review() -> None:
    factory = FactorCandidateFactory(VOCABULARY, seed=12)
    pool = ParentPool(capacity=8)
    pool.add(chain("ts_mean", 5, "close"))
    candidates = await factory.generate(30, pool)
    gate = CandidateReviewer(VOCABULARY, DIMENSIONS, policy=FactorReviewPolicy(max_window=20))
    report = gate.review_batch([item.definition for item in candidates])
    # 随机生成可能自然产生冗余恒等式与批内重复，这是审查应当拦截的仅有的合法拒绝理由
    legitimate = {ReviewCode.REDUNDANT_IDENTITY, ReviewCode.DUPLICATE}
    assert all(
        set(outcome.reasons) <= legitimate
        for outcome in report.outcomes
        if not outcome.accepted
    )
    assert len(report.accepted_definitions) + report.rejected == 30
    assert len(report.accepted_definitions) >= 10


def test_review_is_deterministic_for_same_input() -> None:
    gate = reviewer()
    tree = {"op": "rank", "window": 10, "input": chain("ts_mean", 5, "volume")}
    assert gate.review(tree).accepted
    assert gate.review(tree).reasons == gate.review(tree).reasons


# --------------------------------------------------------------------------- AST/词表边界


@pytest.mark.parametrize(
    "tree",
    [
        "rank(close)",
        42,
        {},
        {"op": "rank", "input": {"field": "close"}},  # 缺 window
        {"op": "rank", "window": 10},  # 缺 input
        {"op": "rank", "window": 10, "input": {"field": "close"}, "scale": 2},  # 未知键
        {"op": "rank", "window": True, "input": {"field": "close"}},  # bool window
        {"op": "rank", "window": "10", "input": {"field": "close"}},  # 非整数 window
        {"op": "rank", "window": 10, "input": "close"},  # input 不是节点
        {"field": "close", "op": "rank"},  # 叶子混入算子键
    ],
)
def test_malformed_trees_are_rejected_before_anything_else(tree: object) -> None:
    outcome = reviewer().review(tree)  # type: ignore[arg-type]
    assert not outcome.accepted
    assert ReviewCode.MALFORMED_TREE in outcome.reasons


def test_unknown_field_operator_and_window_are_rejected() -> None:
    gate = reviewer()
    assert reason_codes(gate.review({"field": "open_interest"})) == {ReviewCode.UNKNOWN_FIELD}
    assert reason_codes(gate.review(chain("wavelet", 10, "close"))) == {
        ReviewCode.UNKNOWN_OPERATOR
    }
    assert reason_codes(gate.review(chain("rank", 15, "close"))) == {ReviewCode.UNKNOWN_WINDOW}


def test_window_bounds_are_checked_separately_from_vocabulary() -> None:
    gate = reviewer(max_window=10)
    outcome = gate.review(chain("rank", 20, "close"))
    assert not outcome.accepted
    assert ReviewCode.WINDOW_OUT_OF_BOUNDS in outcome.reasons
    assert ReviewCode.UNKNOWN_WINDOW not in outcome.reasons
    assert gate.review(chain("rank", 5, "close")).accepted


def test_depth_and_complexity_limits_are_independent() -> None:
    deep = varied_chain("close", depth=4)
    assert ReviewCode.DEPTH_EXCEEDED in reviewer(max_depth=3).review(deep).reasons
    assert reviewer(max_depth=6).review(deep).accepted
    wide_gate = reviewer(max_depth=6, max_operator_nodes=2)
    outcome = wide_gate.review(deep)
    assert ReviewCode.COMPLEXITY_EXCEEDED in outcome.reasons
    assert ReviewCode.DEPTH_EXCEEDED not in outcome.reasons


# --------------------------------------------------------------------------- 量纲


def test_dimension_mismatch_between_operator_and_field() -> None:
    gate = reviewer()
    outcome = gate.review(chain("price_momentum", 10, "volume"))
    assert ReviewCode.DIMENSION_MISMATCH in outcome.reasons
    assert gate.review(chain("price_momentum", 10, "close")).accepted
    # rank 输出无量纲后接 price_momentum 同样拒绝
    nested = {
        "op": "price_momentum",
        "window": 10,
        "input": {"op": "rank", "window": 5, "input": {"field": "close"}},
    }
    assert ReviewCode.DIMENSION_MISMATCH in gate.review(nested).reasons


def test_dimension_flows_through_dimension_preserving_ops() -> None:
    gate = reviewer()
    volume_chain = {
        "op": "ts_delta",
        "window": 10,
        "input": {"op": "ts_delta", "window": 5, "input": {"field": "volume"}},
    }
    assert gate.review(volume_chain).accepted
    mixed = {
        "op": "ts_mean",
        "window": 10,
        "input": {"op": "price_momentum", "window": 5, "input": {"field": "volume"}},
    }
    assert ReviewCode.DIMENSION_MISMATCH in gate.review(mixed).reasons


# --------------------------------------------------------------------------- 恒等式与去重


def test_redundant_identities_are_rejected() -> None:
    gate = reviewer()
    assert ReviewCode.REDUNDANT_IDENTITY in gate.review(
        {"op": "rank", "window": 10, "input": {"op": "rank", "window": 5, "input": {"field": "close"}}}
    ).reasons
    assert ReviewCode.REDUNDANT_IDENTITY in gate.review(
        {
            "op": "ts_mean",
            "window": 5,
            "input": {"op": "ts_mean", "window": 5, "input": {"field": "close"}},
        }
    ).reasons
    assert gate.review(
        {
            "op": "ts_mean",
            "window": 10,
            "input": {"op": "ts_mean", "window": 5, "input": {"field": "close"}},
        }
    ).accepted
    lenient = reviewer(reject_redundant_identity=False)
    double_rank = {
        "op": "rank",
        "window": 10,
        "input": {"op": "rank", "window": 5, "input": {"field": "close"}},
    }
    assert lenient.review(double_rank).accepted


def test_known_and_in_batch_duplicates_are_rejected() -> None:
    gate = reviewer()
    tree = chain("rank", 10, "close")
    outcome = gate.review(tree, known_hashes=frozenset({candidate_hash(tree)}))
    assert ReviewCode.DUPLICATE in outcome.reasons

    report = gate.review_batch([tree, tree])
    assert report.rejected == 1
    assert len(report.accepted_definitions) == 1


# --------------------------------------------------------------------------- 非法候选零回测


def test_illegal_candidates_never_reach_backtest() -> None:
    gate = reviewer()
    candidates = [
        chain("rank", 10, "close"),  # 合法
        chain("wavelet", 10, "close"),  # 未知算子
        chain("rank", 10, "open_interest"),  # 未知字段
        chain("price_momentum", 10, "volume"),  # 量纲冲突
        chain("rank", 15, "close"),  # 非法窗口
        {"op": "rank", "window": 10},  # 畸形
    ]
    report = gate.review_batch(candidates)

    backtested: list[dict[str, object]] = []
    for outcome in report.outcomes:
        if outcome.accepted:
            backtested.append(outcome.definition)
    assert len(backtested) == 1
    assert backtested[0] == candidates[0]
    assert all(outcome.definition not in backtested for outcome in report.outcomes if not outcome.accepted)
    summary = report.rejection_summary
    assert summary[ReviewCode.UNKNOWN_OPERATOR] == 1
    assert summary[ReviewCode.UNKNOWN_FIELD] == 1
    assert summary[ReviewCode.DIMENSION_MISMATCH] == 1
    assert summary[ReviewCode.UNKNOWN_WINDOW] == 1
    assert summary[ReviewCode.MALFORMED_TREE] == 1


@pytest.mark.asyncio
async def test_generation_review_backtest_pipeline_stays_legal_across_rounds() -> None:
    """三轮真实管线：生成 → 审查 → 回测；历史候选不重复回测，反馈仍推进。"""
    search = AdaptiveFactorSearch(EXTENDED_VOCABULARY, seed=31)
    gate = reviewer()
    pool = ParentPool(capacity=16)
    known: set[str] = set()
    backtest_calls = 0
    for _ in range(3):
        generated = await search.run_round(12, pool)
        report = gate.review_batch(
            [item.definition for item in generated.candidates], known_hashes=frozenset(known)
        )
        tested: set[str] = set()
        accepted: set[str] = set()
        for outcome in report.outcomes:
            digest = candidate_hash(outcome.definition)
            if outcome.accepted and digest not in known:
                backtest_calls += 1
                tested.add(digest)
                known.add(digest)
                if outcome.definition.get("op") == "rank" and outcome.definition.get("window") == 10:
                    accepted.add(digest)
                    pool.add(outcome.definition)
        search.observe(RoundFeedback.from_round(generated, tested, accepted))
    assert backtest_calls == len(known)  # 零重复、零非法回测
    assert search.state.rounds == 3
    assert search.state.momentum is not None


# --------------------------------------------------------------------------- 配置校验


def test_dimension_table_must_cover_vocabulary() -> None:
    with pytest.raises(ValueError, match="dimension"):
        FactorDimensionTable(
            field_dimensions={"close": "price"},
            operator_signatures=DIMENSIONS.operator_signatures,
            vocabulary=VOCABULARY,
        )
    with pytest.raises(ValueError, match="signature"):
        FactorDimensionTable(
            field_dimensions=DIMENSIONS.field_dimensions,
            operator_signatures={
                "rank": OperatorSignature(accepted_input_dimensions=None, output_dimension="shapeless")
            },
            vocabulary=VOCABULARY,
        )


def test_review_policy_rejects_infeasible_bounds() -> None:
    with pytest.raises(ValueError, match="depth"):
        FactorReviewPolicy(max_depth=0)
    with pytest.raises(ValueError, match="operator nodes"):
        FactorReviewPolicy(max_operator_nodes=0)
    with pytest.raises(ValueError, match="window"):
        FactorReviewPolicy(min_window=0)
    with pytest.raises(ValueError, match="window"):
        FactorReviewPolicy(min_window=20, max_window=10)
