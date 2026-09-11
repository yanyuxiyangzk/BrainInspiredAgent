"""Post-install smoke for L-005: deterministic review gate keeps backtests legal."""
import asyncio

from domain_sdk.factor_adaptation import AdaptiveFactorSearch, RoundFeedback
from domain_sdk.factor_generation import (
    FactorVocabulary,
    ParentPool,
    candidate_hash,
)
from domain_sdk.factor_review import (
    CandidateReviewer,
    FactorDimensionTable,
    FactorReviewPolicy,
    OperatorSignature,
)

VOCAB = FactorVocabulary(
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
    },
)


async def smoke() -> None:
    gate = CandidateReviewer(
        VOCAB, DIMENSIONS, policy=FactorReviewPolicy(min_window=5, max_window=20)
    )
    assert not gate.review("rank(close)").accepted, "non-tree input must be rejected"
    assert not gate.review({"op": "wavelet", "window": 10, "input": {"field": "close"}}).accepted

    search = AdaptiveFactorSearch(VOCAB, seed=7)
    pool = ParentPool(capacity=16)
    known: set[str] = set()
    backtests = 0
    rejections = 0
    for _ in range(4):
        generated = await search.run_round(12, pool)
        report = gate.review_batch(
            [item.definition for item in generated.candidates], known_hashes=known
        )
        rejections += report.rejected
        tested: set[str] = set()
        accepted: set[str] = set()
        for outcome in report.outcomes:
            if not outcome.accepted:
                continue
            digest = candidate_hash(outcome.definition)
            if digest in known:
                raise AssertionError("duplicate candidate reached backtest")
            backtests += 1
            known.add(digest)
            tested.add(digest)
            if outcome.definition.get("op") == "rank" and outcome.definition.get("window") == 10:
                accepted.add(digest)
                pool.add(outcome.definition)
        search.observe(RoundFeedback.from_round(generated, tested, accepted))
    assert backtests == len(known), "only reviewed-legal candidates may be backtested"
    assert rejections > 0, "review gate must have rejected malformed or redundant work"
    assert search.state.rounds == 4
    print(
        "WSL packaging smoke PASS: backtests",
        backtests,
        "rejections",
        rejections,
        "rounds",
        search.state.rounds,
    )


asyncio.run(smoke())
