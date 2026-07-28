"""Deterministic, contact-balanced sampling for global writing profiles."""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


STYLE_SAMPLING_POLICY_VERSION = 1
STYLE_SANITIZER_VERSION = 1
STYLE_MAX_SAMPLES = 100
STYLE_MAX_SAMPLES_PER_PEER = 10
STYLE_CANDIDATE_ROWS_PER_PEER = 40
STYLE_MIN_SAMPLES = 8
STYLE_MIN_SAMPLE_PEERS = 8
STYLE_SHORT_MESSAGE_MAX_SEMANTIC_CHARS = 2
STYLE_SHORT_MESSAGE_NUMERATOR = 1
STYLE_SHORT_MESSAGE_DENOMINATOR = 5

_STYLE_FILLER_TOKENS = frozenset(
    {
        "ok",
        "嗯",
        "嗯嗯",
        "啊",
        "哦",
        "噢",
        "好",
        "好呀",
        "好的",
        "收到",
        "知道了",
        "谢谢",
        "晚安",
        "哈哈",
        "哈哈哈",
        "嘿嘿",
        "嘻嘻",
        "呵呵",
    }
)
_STYLE_SYSTEM_MARKERS = (
    "我们已经是好友了，来聊天吧",
    "我们已经是好友了,来聊天吧",
    "你们已经是好友了，来聊天吧",
    "关注你了,快去看看吧",
    "关注你了，快去看看吧",
)


class StyleSamplingError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class StyleSampleCandidate:
    peer_key: str
    text: str
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StyleSampleSelection:
    items: tuple[StyleSampleCandidate, ...]
    source_peer_count: int
    source_last_message_at: datetime | None

    @property
    def samples(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.items)


def _normalized_style_text(value: object) -> tuple[str, str] | None:
    text = " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().split()
    )[:1000]
    if not text:
        return None
    compact = "".join(text.split())
    semantic = "".join(
        character.casefold()
        for character in compact
        if unicodedata.category(character)[:1] in {"L", "N"}
    )
    if (
        not semantic
        or semantic.isdecimal()
        or semantic in _STYLE_FILLER_TOKENS
        or any(marker in compact for marker in _STYLE_SYSTEM_MARKERS)
    ):
        return None
    return text, semantic


def _round_robin_take(
    *,
    buckets: dict[str, list[StyleSampleCandidate]],
    peer_order: tuple[str, ...],
    selected: list[StyleSampleCandidate],
    selected_per_peer: Counter[str],
    offsets: dict[str, int],
    target_total: int,
    maximum_per_peer: int,
) -> None:
    while len(selected) < target_total:
        changed = False
        for peer in peer_order:
            if len(selected) >= target_total:
                break
            if selected_per_peer[peer] >= maximum_per_peer:
                continue
            offset = offsets[peer]
            candidates = buckets.get(peer, [])
            if offset >= len(candidates):
                continue
            selected.append(candidates[offset])
            selected_per_peer[peer] += 1
            offsets[peer] = offset + 1
            changed = True
        if not changed:
            break


def stratified_style_samples(
    candidates: Iterable[StyleSampleCandidate],
    *,
    maximum_samples: int = STYLE_MAX_SAMPLES,
    maximum_per_peer: int = STYLE_MAX_SAMPLES_PER_PEER,
) -> StyleSampleSelection:
    """Select deterministic samples without letting one contact dominate."""

    maximum = min(STYLE_MAX_SAMPLES, max(STYLE_MIN_SAMPLES, int(maximum_samples)))
    per_peer = min(
        STYLE_MAX_SAMPLES_PER_PEER,
        max(1, int(maximum_per_peer)),
    )
    peer_order_values: list[str] = []
    long_buckets: dict[str, list[StyleSampleCandidate]] = {}
    short_buckets: dict[str, list[StyleSampleCandidate]] = {}
    seen_texts: set[str] = set()
    for candidate in candidates:
        peer = str(candidate.peer_key or "").strip()
        normalized = _normalized_style_text(candidate.text)
        if not peer or normalized is None:
            continue
        text, semantic = normalized
        duplicate_key = "".join(text.casefold().split())
        if duplicate_key in seen_texts:
            continue
        seen_texts.add(duplicate_key)
        item = StyleSampleCandidate(peer, text, candidate.occurred_at)
        if peer not in long_buckets:
            peer_order_values.append(peer)
            long_buckets[peer] = []
            short_buckets[peer] = []
        bucket = (
            short_buckets
            if len(semantic) <= STYLE_SHORT_MESSAGE_MAX_SEMANTIC_CHARS
            else long_buckets
        )
        if len(bucket[peer]) < per_peer:
            bucket[peer].append(item)

    peer_order = tuple(peer_order_values)
    selected: list[StyleSampleCandidate] = []
    selected_per_peer: Counter[str] = Counter()
    long_offsets = {peer: 0 for peer in peer_order}
    short_offsets = {peer: 0 for peer in peer_order}
    initial_long_target = maximum * (
        STYLE_SHORT_MESSAGE_DENOMINATOR - STYLE_SHORT_MESSAGE_NUMERATOR
    ) // STYLE_SHORT_MESSAGE_DENOMINATOR
    _round_robin_take(
        buckets=long_buckets,
        peer_order=peer_order,
        selected=selected,
        selected_per_peer=selected_per_peer,
        offsets=long_offsets,
        target_total=initial_long_target,
        maximum_per_peer=per_peer,
    )
    long_count = len(selected)
    maximum_short_count = min(
        maximum - long_count,
        long_count
        * STYLE_SHORT_MESSAGE_NUMERATOR
        // (
            STYLE_SHORT_MESSAGE_DENOMINATOR
            - STYLE_SHORT_MESSAGE_NUMERATOR
        ),
    )
    _round_robin_take(
        buckets=short_buckets,
        peer_order=peer_order,
        selected=selected,
        selected_per_peer=selected_per_peer,
        offsets=short_offsets,
        target_total=long_count + maximum_short_count,
        maximum_per_peer=per_peer,
    )
    _round_robin_take(
        buckets=long_buckets,
        peer_order=peer_order,
        selected=selected,
        selected_per_peer=selected_per_peer,
        offsets=long_offsets,
        target_total=maximum,
        maximum_per_peer=per_peer,
    )

    if len(selected) < STYLE_MIN_SAMPLES:
        raise StyleSamplingError("insufficient_style_samples")
    selected_peer_count = len(selected_per_peer)
    if selected_peer_count < STYLE_MIN_SAMPLE_PEERS:
        raise StyleSamplingError("insufficient_style_diversity")
    if max(selected_per_peer.values()) * 100 > len(selected) * 15:
        raise StyleSamplingError("insufficient_style_diversity")

    short_count = sum(
        1
        for item in selected
        if len(_normalized_style_text(item.text)[1])
        <= STYLE_SHORT_MESSAGE_MAX_SEMANTIC_CHARS
    )
    if (
        short_count * STYLE_SHORT_MESSAGE_DENOMINATOR
        > len(selected) * STYLE_SHORT_MESSAGE_NUMERATOR
    ):
        raise RuntimeError("style short-message quota invariant failed")
    last_at = max(
        (item.occurred_at for item in selected if item.occurred_at is not None),
        default=None,
    )
    return StyleSampleSelection(
        items=tuple(selected),
        source_peer_count=selected_peer_count,
        source_last_message_at=last_at,
    )


def style_profile_is_current(row: object) -> bool:
    if row is None:
        return False
    try:
        source_count = int(getattr(row, "source_message_count", 0) or 0)
        peer_count = int(getattr(row, "source_peer_count", 0) or 0)
        sampling_version = int(
            getattr(row, "sampling_policy_version", 0) or 0
        )
        sanitizer_version = int(getattr(row, "sanitizer_version", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        sampling_version == STYLE_SAMPLING_POLICY_VERSION
        and sanitizer_version == STYLE_SANITIZER_VERSION
        and source_count >= STYLE_MIN_SAMPLES
        and STYLE_MIN_SAMPLE_PEERS <= peer_count <= source_count
    )
