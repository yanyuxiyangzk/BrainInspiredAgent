"""Post-install smoke for L-008: FSA windowed statistics, versioned bans, release."""
from domain_sdk.factor_fsa import (
    FsaPolicy,
    FsaTracker,
    skeleton_key,
)

POLICY = FsaPolicy(
    window_rounds=3,
    share_threshold=0.5,
    min_accept_rate=0.05,
    max_variants=2,
    min_window_observations=25,
    release_share=0.1,
    release_rounds=2,
)


def family(window: int) -> dict[str, object]:
    return {"op": "ts_delta", "window": window, "input": {"field": "close"}}


def other() -> dict[str, object]:
    return {"op": "ts_mean", "window": 5, "input": {"field": "volume"}}


def diverse() -> list[dict[str, object]]:
    return [
        other(),
        {"op": "ts_mean", "window": 10, "input": {"field": "volume"}},
        {"op": "ts_mean", "window": 20, "input": {"field": "close"}},
        {"op": "rank", "window": 5, "input": {"field": "volume"}},
    ]


tracker = FsaTracker(POLICY)
assert skeleton_key(family(5)) == skeleton_key(family(99)), "skeleton must abstract windows"

for _ in range(POLICY.window_rounds):
    summary = tracker.observe([family(5 + i % 2) for i in range(8)] + [other()] * 2)
assert summary.bans_issued == 1, "dominant valueless skeleton must be banned"
ban = tracker.active_bans()[0]
assert ban.version == 1 and ban.reason == "DOMINANT_WITHOUT_VALUE"
assert tracker.intercept(family(77)).code == "BANNED_SKELETON"

for _ in range(POLICY.release_rounds):
    batch = [item for item in ([family(5)] * 8) if tracker.intercept(item).allowed]
    tracker.observe(batch + diverse())
assert tracker.active_bans() == (), "release conditions must restore generation"
assert tracker.ban_history()[0].released_round is not None

for _ in range(POLICY.window_rounds):
    tracker.observe([family(5 + i % 2) for i in range(8)] + [other()] * 2)
assert tracker.active_bans()[0].version == 2, "re-ban must increment the version"
print(
    "WSL packaging smoke PASS: bans",
    len(tracker.ban_history()),
    "active",
    len(tracker.active_bans()),
    "skeleton",
    ban.skeleton,
)
