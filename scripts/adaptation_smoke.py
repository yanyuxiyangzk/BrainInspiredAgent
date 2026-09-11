"""Post-install smoke for L-004: momentum, adaptive step and explore/exploit rebalancing."""
import asyncio

from domain_sdk.factor_adaptation import (
    AdaptiveFactorSearch,
    ExplorationPolicy,
    FactorSearchGovernor,
    GeneratedRound,
    RoundFeedback,
)
from domain_sdk.factor_generation import (
    FactorVocabulary,
    GenerationStrategy,
    ParentPool,
    candidate_hash,
)

VOCAB = FactorVocabulary(
    fields=("close", "volume", "high"),
    operators=("rank", "ts_mean", "ts_delta"),
    windows=(5, 10, 20),
)


def backtest(candidates: list[GeneratedRound]) -> tuple[set[str], set[str]]:
    tested: set[str] = set()
    accepted: set[str] = set()
    for item in candidates:
        digest = candidate_hash(item.definition)
        tested.add(digest)
        if item.definition.get("op") == "rank" and item.definition.get("window") == 10:
            accepted.add(digest)
    return tested, accepted


async def drive(seed: int, rounds: int, resume_after: int | None = None) -> list[GeneratedRound]:
    pool = ParentPool(capacity=16)
    search = AdaptiveFactorSearch(VOCAB, seed=seed)
    history: list[GeneratedRound] = []
    for index in range(rounds):
        if resume_after is not None and index == resume_after:
            search = AdaptiveFactorSearch.restore(search.snapshot(), VOCAB)
        generated = await search.run_round(12, pool)
        tested, accepted = backtest(list(generated.candidates))
        for item in generated.candidates:
            if candidate_hash(item.definition) in accepted:
                pool.add(item.definition)
        search.observe(RoundFeedback.from_round(generated, tested, accepted))
        history.append(generated)
    return history


async def smoke() -> None:
    policy = ExplorationPolicy()
    live = await drive(2026, rounds=6)
    replay = await drive(2026, rounds=6)
    assert [
        ([i.definition for i in r.candidates], dict(r.quota), r.window_step, r.duplicates)
        for r in live
    ] == [
        ([i.definition for i in r.candidates], dict(r.quota), r.window_step, r.duplicates)
        for r in replay
    ], "seed replay drifted"

    resumed = await drive(2026, rounds=6, resume_after=2)
    assert [
        ([i.definition for i in r.candidates], dict(r.quota), r.window_step, r.duplicates)
        for r in resumed
    ] == [
        ([i.definition for i in r.candidates], dict(r.quota), r.window_step, r.duplicates)
        for r in live
    ], "snapshot resume drifted"

    governor = FactorSearchGovernor()
    starving = RoundFeedback(
        tested_by_strategy={GenerationStrategy.MUTATE: 10},
        accepted_by_strategy={GenerationStrategy.MUTATE: 0},
        duplicates=0,
        generated=10,
    )
    for _ in range(6):
        state = governor.observe(starving)
        quota = governor.current_quota()
        assert abs(sum(quota.ratios.values()) - 1.0) < 1e-9
        assert all(
            policy.min_ratios[s] - 1e-9 <= ratio <= policy.max_ratios[s] + 1e-9
            for s, ratio in quota.ratios.items()
        )
    assert state.window_step == policy.max_window_step, "stagnation must widen the step"
    assert state.exploration_share == policy.max_exploration_share, "share must clamp at the cap"
    print(
        "WSL packaging smoke PASS: rounds",
        len(live),
        "share",
        round(governor.state.exploration_share, 4),
        "step",
        governor.state.window_step,
    )


asyncio.run(smoke())
