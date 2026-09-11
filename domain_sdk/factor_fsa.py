"""Frequent-subtree avoidance: skeleton statistics, versioned bans, release (L-008).

架构 §5：FSA 定期统计表达式算子子树；某骨架在统计窗口内占比超过阈值且入库
增量价值低于下限时进入版本化禁止列表；同骨架参数变体设上限；列表记录原因、
统计窗口与解除条件；拦截是生成后的确定性审查环节，LLM 不得绕过；FSA 只影响
新搜索——已入库因子与历史记录永远不被改写。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

_SNAPSHOT_FORMAT = 1

BAN_DOMINANT = "DOMINANT_WITHOUT_VALUE"


def _check_tree(definition: object) -> None:
    if not isinstance(definition, Mapping):
        raise TypeError(f"malformed definition: expected a mapping node, got {type(definition)!r}")
    keys = set(definition)
    if "field" in keys:
        if not isinstance(definition["field"], str):
            raise TypeError("malformed definition: leaf field must be a string")
        if keys != {"field"}:
            raise ValueError("malformed definition: leaf must be exactly {'field': str}")
        return
    if keys != {"op", "window", "input"}:
        raise ValueError("malformed definition: operator node must be exactly {op, window, input}")
    if not isinstance(definition["op"], str):
        raise TypeError("malformed definition: op must be a string")
    if isinstance(definition["window"], bool) or not isinstance(definition["window"], int):
        raise TypeError("malformed definition: window must be an integer")
    _check_tree(definition["input"])


def skeleton_key(definition: Mapping[str, object]) -> str:
    """算子骨架：抽象掉窗口与字段的树形结构键。"""
    if "field" in definition:
        return "(field)"
    child = cast(Mapping[str, object], definition["input"])
    return f"({definition['op']} {skeleton_key(child)})"


def subtree_skeletons(definition: Mapping[str, object]) -> tuple[str, ...]:
    """全部算子子树的骨架键，根在前；纯叶骨架不参与统计。"""
    keys: list[str] = []
    node: Mapping[str, object] = definition
    while "op" in node:
        keys.append(_subtree_key(node))
        node = cast(Mapping[str, object], node["input"])
    return tuple(keys)


def _subtree_key(node: Mapping[str, object]) -> str:
    if "field" in node:
        return "(field)"
    return f"({node['op']} {_subtree_key(cast(Mapping[str, object], node['input']))})"


def variant_signature(definition: Mapping[str, object]) -> tuple[int, ...]:
    """参数变体签名：沿链的窗口序列（骨架相同、窗口不同即为不同变体）。"""
    signature: list[int] = []
    node: Mapping[str, object] = definition
    while "op" in node:
        signature.append(cast(int, node["window"]))
        node = cast(Mapping[str, object], node["input"])
    return tuple(signature)


@dataclass(frozen=True, slots=True)
class FsaPolicy:
    """FSA 硬门槛参数：统计窗口、占比/价值阈值、变体上限与解除条件。"""

    window_rounds: int = 5
    share_threshold: float = 0.15
    min_accept_rate: float = 0.02
    max_variants: int = 3
    min_window_observations: int = 20
    release_share: float = 0.05
    release_rounds: int = 3

    def __post_init__(self) -> None:
        if self.window_rounds < 1:
            raise ValueError("window_rounds must be positive")
        if not 0.0 < self.share_threshold <= 1.0:
            raise ValueError("share_threshold must stay within (0, 1]")
        if not 0.0 <= self.min_accept_rate <= 1.0:
            raise ValueError("min_accept_rate must stay within [0, 1]")
        if self.max_variants < 1:
            raise ValueError("max_variants must be positive")
        if self.min_window_observations < 1:
            raise ValueError("min_window_observations must be positive")
        if not 0.0 <= self.release_share < self.share_threshold:
            raise ValueError("release_share must stay below share_threshold")
        if self.release_rounds < 1 or self.release_rounds > self.window_rounds:
            raise ValueError("release_rounds must fit inside the statistics window")


@dataclass(frozen=True, slots=True)
class BanEntry:
    """一条版本化禁止记录：原因、证据、颁发轮次、解除条件与解除轮次。"""

    skeleton: str
    reason: str
    version: int
    banned_round: int
    window_rounds: int
    stats: Mapping[str, float]
    release_condition: Mapping[str, float]
    released_round: int | None = None

    @property
    def active(self) -> bool:
        return self.released_round is None

    def to_dict(self) -> dict[str, object]:
        return {
            "skeleton": self.skeleton,
            "reason": self.reason,
            "version": self.version,
            "banned_round": self.banned_round,
            "window_rounds": self.window_rounds,
            "stats": dict(self.stats),
            "release_condition": dict(self.release_condition),
            "released_round": self.released_round,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> BanEntry:
        return cls(
            skeleton=str(payload["skeleton"]),
            reason=str(payload["reason"]),
            version=int(cast(int, payload["version"])),
            banned_round=int(cast(int, payload["banned_round"])),
            window_rounds=int(cast(int, payload["window_rounds"])),
            stats={
                str(k): float(cast(float, v))
                for k, v in cast(Mapping[str, object], payload["stats"]).items()
            },
            release_condition={
                str(k): float(cast(float, v))
                for k, v in cast(Mapping[str, object], payload["release_condition"]).items()
            },
            released_round=(
                None
                if payload["released_round"] is None
                else int(cast(int, payload["released_round"]))
            ),
        )


@dataclass(frozen=True, slots=True)
class FsaDecision:
    """拦截裁决：OK / BANNED_SKELETON / VARIANT_CAP。"""

    allowed: bool
    code: str
    skeleton: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "skeleton": self.skeleton,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FsaDecision:
        return cls(
            allowed=bool(payload["allowed"]),
            code=str(payload["code"]),
            skeleton=str(payload["skeleton"]),
            detail=str(payload["detail"]),
        )


@dataclass(frozen=True, slots=True)
class FsaRoundSummary:
    """一轮观察后的窗口摘要：颁发/解除的禁止数、骨架多样性与占比。"""

    round: int
    bans_issued: int
    bans_released: int
    distinct_skeletons: int
    total_observations: int
    shares: Mapping[str, float] = field(default_factory=dict)

    def skeleton_share(self, skeleton: str) -> float:
        return self.shares.get(skeleton, 0.0)


class FsaTracker:
    """窗口化子树统计器：颁发版本化禁止、评估解除条件、拦截新候选。"""

    def __init__(self, policy: FsaPolicy | None = None) -> None:
        self._policy = policy if policy is not None else FsaPolicy()
        self._round = 0
        self._window: deque[dict[str, object]] = deque(maxlen=self._policy.window_rounds)
        self._history: list[BanEntry] = []
        self._active: dict[str, BanEntry] = {}
        self._low_streak: dict[str, int] = {}
        self._next_version = 1

    @property
    def policy(self) -> FsaPolicy:
        return self._policy

    # ---------------------------------------------------------------- 观察

    def observe(
        self,
        definitions: Iterable[Mapping[str, object]],
        *,
        accepted_hashes: Iterable[str] = (),
    ) -> FsaRoundSummary:
        counts: dict[str, int] = {}
        accepts: dict[str, int] = {}
        variants: dict[str, set[tuple[int, ...]]] = {}
        accepted = frozenset(accepted_hashes)
        total_observations = 0
        for definition in definitions:
            _check_tree(definition)
            tree = definition
            skeletons = subtree_skeletons(tree)
            total_observations += len(skeletons)
            digest_of = _hash_of(tree)
            for skeleton in skeletons:
                counts[skeleton] = counts.get(skeleton, 0) + 1
                if digest_of in accepted:
                    accepts[skeleton] = accepts.get(skeleton, 0) + 1
                variants.setdefault(skeleton, set()).add(variant_signature(tree))
        self._window.append(
            {
                "counts": counts,
                "accepts": accepts,
                "variants": {skeleton: set(values) for skeleton, values in variants.items()},
            }
        )
        self._round += 1

        window_counts, window_accepts, _ = self._aggregate()
        issued = self._issue_bans(window_counts, window_accepts)
        released = self._evaluate_releases(window_counts, window_accepts)
        total = sum(window_counts.values())
        shares = {
            skeleton: window_counts[skeleton] / total
            for skeleton in sorted(window_counts)
            if total > 0
        }
        current = self._window[-1]
        return FsaRoundSummary(
            round=self._round,
            bans_issued=issued,
            bans_released=released,
            distinct_skeletons=len(cast(dict[str, int], current["counts"])),
            total_observations=total_observations,
            shares=shares,
        )

    # ---------------------------------------------------------------- 拦截

    def intercept(self, definition: object) -> FsaDecision:
        _check_tree(definition)
        tree = cast(Mapping[str, object], definition)
        for skeleton in subtree_skeletons(tree):
            ban = self._active.get(skeleton)
            if ban is not None:
                return FsaDecision(
                    False,
                    "BANNED_SKELETON",
                    skeleton,
                    f"banned at version {ban.version} since round {ban.banned_round}",
                )
        root = skeleton_key(tree)
        seen = self._seen_variants(root)
        signature = variant_signature(tree)
        if seen and signature not in seen and len(seen) >= self._policy.max_variants:
            return FsaDecision(
                False,
                "VARIANT_CAP",
                root,
                f"{len(seen)} window variants already observed; cap is {self._policy.max_variants}",
            )
        return FsaDecision(True, "OK", root, "")

    # ---------------------------------------------------------------- 查询与解除

    def active_bans(self) -> tuple[BanEntry, ...]:
        return tuple(self._active[skeleton] for skeleton in sorted(self._active))

    def ban_history(self) -> tuple[BanEntry, ...]:
        return tuple(self._history)

    def release(self, skeleton: str, *, reason: str) -> None:
        ban = self._active.get(skeleton)
        if ban is None:
            raise ValueError(f"skeleton {skeleton!r} is not currently banned")
        self._retire(ban, reason)

    # ---------------------------------------------------------------- 内部

    def _aggregate(self) -> tuple[dict[str, int], dict[str, int], dict[str, set[tuple[int, ...]]]]:
        counts: dict[str, int] = {}
        accepts: dict[str, int] = {}
        variants: dict[str, set[tuple[int, ...]]] = {}
        for record in self._window:
            for skeleton, count in cast(dict[str, int], record["counts"]).items():
                counts[skeleton] = counts.get(skeleton, 0) + count
            for skeleton, count in cast(dict[str, int], record["accepts"]).items():
                accepts[skeleton] = accepts.get(skeleton, 0) + count
            for skeleton, values in cast(
                dict[str, set[tuple[int, ...]]], record["variants"]
            ).items():
                variants.setdefault(skeleton, set()).update(values)
        return counts, accepts, variants

    def _issue_bans(self, window_counts: Mapping[str, int], window_accepts: Mapping[str, int]) -> int:
        total = sum(window_counts.values())
        if total < self._policy.min_window_observations:
            return 0
        issued = 0
        for skeleton in sorted(window_counts):
            if skeleton in self._active:
                continue
            count = window_counts[skeleton]
            share = count / total
            accept_rate = (window_accepts.get(skeleton, 0) / count) if count else 0.0
            if share >= self._policy.share_threshold and accept_rate < self._policy.min_accept_rate:
                ban = BanEntry(
                    skeleton=skeleton,
                    reason=BAN_DOMINANT,
                    version=self._next_version,
                    banned_round=self._round,
                    window_rounds=self._policy.window_rounds,
                    stats={"share": share, "accept_rate": accept_rate},
                    release_condition={
                        "release_share": self._policy.release_share,
                        "release_rounds": self._policy.release_rounds,
                    },
                )
                self._active[skeleton] = ban
                self._history.append(ban)
                self._next_version += 1
                self._low_streak[skeleton] = 0
                issued += 1
        return issued

    def _evaluate_releases(
        self, window_counts: Mapping[str, int], window_accepts: Mapping[str, int]
    ) -> int:
        released = 0
        for skeleton in sorted(self._active):
            ban = self._active[skeleton]
            current = self._window[-1]
            round_counts = cast(dict[str, int], current["counts"])
            round_share = (
                round_counts.get(skeleton, 0) / sum(round_counts.values())
                if sum(round_counts.values())
                else 0.0
            )
            if round_share < self._policy.release_share:
                self._low_streak[skeleton] = self._low_streak.get(skeleton, 0) + 1
            else:
                self._low_streak[skeleton] = 0
            count = window_counts.get(skeleton, 0)
            window_accept_rate = (window_accepts.get(skeleton, 0) / count) if count else 0.0
            if window_accept_rate >= self._policy.min_accept_rate:
                self._retire(ban, ban.reason, note="VALUE_RETURNED")
                released += 1
            elif self._low_streak[skeleton] >= self._policy.release_rounds:
                self._retire(ban, ban.reason)
                released += 1
        return released

    def _retire(self, ban: BanEntry, reason: str, *, note: str | None = None) -> None:
        retired = BanEntry(
            skeleton=ban.skeleton,
            reason=reason,
            version=ban.version,
            banned_round=ban.banned_round,
            window_rounds=ban.window_rounds,
            stats=ban.stats if note is None else {**ban.stats, "release_note": 1.0},
            release_condition=ban.release_condition,
            released_round=self._round,
        )
        self._history = [
            retired if entry.version == ban.version and entry.skeleton == ban.skeleton else entry
            for entry in self._history
        ]
        if self._active.get(ban.skeleton) == ban:
            del self._active[ban.skeleton]

    def _seen_variants(self, skeleton: str) -> set[tuple[int, ...]]:
        seen: set[tuple[int, ...]] = set()
        for record in self._window:
            values = cast(dict[str, set[tuple[int, ...]]], record["variants"]).get(skeleton)
            if values:
                seen.update(values)
        return seen

    # ---------------------------------------------------------------- 快照

    def snapshot(self) -> dict[str, object]:
        return {
            "format": _SNAPSHOT_FORMAT,
            "round": self._round,
            "window": [
                {
                    "counts": cast(dict[str, int], record["counts"]).copy(),
                    "accepts": cast(dict[str, int], record["accepts"]).copy(),
                    "variants": {
                        skeleton: sorted(list(v) for v in values)
                        for skeleton, values in cast(
                            dict[str, set[tuple[int, ...]]], record["variants"]
                        ).items()
                    },
                }
                for record in self._window
            ],
            "history": [entry.to_dict() for entry in self._history],
            "active_skeletons": sorted(self._active),
            "low_streak": dict(self._low_streak),
            "next_version": self._next_version,
        }

    @classmethod
    def restore(cls, payload: Mapping[str, object], *, policy: FsaPolicy | None = None) -> FsaTracker:
        if payload.get("format") != _SNAPSHOT_FORMAT:
            raise ValueError("unsupported FSA snapshot format")
        tracker = cls(policy)
        tracker._round = int(cast(int, payload["round"]))
        for record in cast(list[object], payload["window"]):
            entry = cast(Mapping[str, object], record)
            tracker._window.append(
                {
                    "counts": {
                        str(k): int(cast(int, v))
                        for k, v in cast(Mapping[str, object], entry["counts"]).items()
                    },
                    "accepts": {
                        str(k): int(cast(int, v))
                        for k, v in cast(Mapping[str, object], entry["accepts"]).items()
                    },
                    "variants": {
                        str(k): {tuple(int(w) for w in variant) for variant in values}
                        for k, values in cast(
                            Mapping[str, Sequence[Sequence[int]]], entry["variants"]
                        ).items()
                    },
                }
            )
        tracker._history = [BanEntry.from_dict(cast(Mapping[str, object], e)) for e in cast(list[object], payload["history"])]
        for skeleton in cast(Sequence[str], payload["active_skeletons"]):
            match = [
                entry
                for entry in tracker._history
                if entry.skeleton == skeleton and entry.released_round is None
            ]
            if not match:
                raise ValueError(f"active skeleton {skeleton!r} missing from history")
            tracker._active[skeleton] = match[-1]
        tracker._low_streak = {
            str(k): int(cast(int, v))
            for k, v in cast(Mapping[str, object], payload["low_streak"]).items()
        }
        tracker._next_version = int(cast(int, payload["next_version"]))
        return tracker


def _hash_of(definition: Mapping[str, object]) -> str:
    from domain_sdk.factor_generation import candidate_hash

    return candidate_hash(definition)
