"""Post-install smoke for L-003: seeded generation, quota split, pool fallback."""
import asyncio

from domain_sdk.factor_generation import (
    DEFAULT_RATIOS,
    FactorCandidateFactory,
    FactorVocabulary,
    GenerationQuota,
    GenerationStrategy,
    ParentPool,
)

VOCAB = FactorVocabulary(
    fields=("close", "volume"), operators=("rank", "ts_mean"), windows=(5, 20)
)


class Proposer:
    async def propose(self, count: int) -> list[dict[str, object]]:
        return [{"op": "ts_mean", "window": 20, "input": {"field": "close"}}] * count


async def smoke() -> None:
    pool = ParentPool(capacity=4)
    pool.add({"op": "rank", "window": 5, "input": {"field": "close"}})
    factory = FactorCandidateFactory(VOCAB, seed=2026, mechanism_proposer=Proposer())
    batch = await factory.generate(20, pool)
    counts: dict[GenerationStrategy, int] = {}
    for item in batch:
        counts[item.strategy] = counts.get(item.strategy, 0) + 1
    allocation = GenerationQuota(dict(DEFAULT_RATIOS)).allocate(20)
    assert all(counts[s] == allocation[s] for s in GenerationStrategy), counts

    replay_pool = ParentPool(capacity=4)
    replay_pool.add({"op": "rank", "window": 5, "input": {"field": "close"}})
    replay = FactorCandidateFactory(VOCAB, seed=2026, mechanism_proposer=Proposer())
    again = await replay.generate(20, replay_pool)
    assert [i.definition for i in again] == [i.definition for i in batch], "seed replay drifted"

    cold = FactorCandidateFactory(VOCAB, seed=1, mechanism_proposer=Proposer())
    cold_batch = await cold.generate(6, ParentPool(capacity=2))
    assert all(
        i.strategy in {GenerationStrategy.RANDOM_EXPLORE, GenerationStrategy.LLM_MECHANISM}
        for i in cold_batch
    )
    print("WSL packaging smoke PASS: quota", {s.value: counts[s] for s in GenerationStrategy})


asyncio.run(smoke())
