"""Deterministic synthetic model copies and the shared acceptance rules.

The project deliberately avoids real neural models.  A model distribution is a
pure function of the model seed, the role (draft/shared/target) and the token
context.  Probabilities are integer basis points, so a replay on another Python
3 interpreter does not depend on floating-point rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Iterable, Mapping, Sequence


SPECIAL_NAMES = ("pad", "bos", "eos", "unk", "ctx_end")
SPECIAL_IDS: dict[str, int] = {name: index for index, name in enumerate(SPECIAL_NAMES)}
UNK_ID = SPECIAL_IDS["unk"]

MARKER_TEMPLATE = 5
MARKER_REROUTE = 6
MARKER_TRANSFER = 7
MARKER_BY_CLASS = {
    "template": MARKER_TEMPLATE,
    "reroute": MARKER_REROUTE,
    "transfer": MARKER_TRANSFER,
}
CLASS_BY_MARKER = {value: key for key, value in MARKER_BY_CLASS.items()}

DRAFT_VOCAB_SIZE = 64
GENERATION_TOKEN_IDS = tuple(range(8, DRAFT_VOCAB_SIZE))
TARGET_ONLY_IDS = (64, 65, 66, 67)
TARGET_VOCAB_SIZE = 68

# The verification rule is intentionally explicit and integer-only:
#   ratio_bps = floor(10000 * target_p(candidate) / draft_p(candidate))
#   accept iff distributions are exactly equal, or ratio_bps >= threshold_bps.
ACCEPTANCE_MODE = "exact-distribution-else-ratio-bps"
FALLBACK_RULE = "first_reject_resample_target_residual"

_MASK64 = (1 << 64) - 1


def stable_u64(*parts: object) -> int:
    """Stable 64-bit digest used instead of process-randomized ``hash``."""

    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return int.from_bytes(digest.digest()[:8], "big") & _MASK64


def distribution_digest(probabilities: Sequence[int]) -> str:
    payload = ",".join(str(value) for value in probabilities).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def build_token_mapping(seed: int) -> dict[str, object]:
    """Build a target-vocabulary to internal/draft-vocabulary mapping.

    Internal IDs 0..4 are the shared special tokens.  IDs 5..63 are ordinary
    tokens and are permuted into target IDs 5..63.  Target IDs 64..67 are
    deliberately target-only and therefore unmappable to the draft vocabulary.
    """

    target_ordinary = list(range(5, DRAFT_VOCAB_SIZE))
    target_order = sorted(
        target_ordinary, key=lambda token_id: (stable_u64("map", seed, token_id), token_id)
    )

    target_to_internal: dict[str, int] = {}
    internal_to_target: dict[str, int] = {}
    for name, token_id in SPECIAL_IDS.items():
        target_to_internal[str(token_id)] = token_id
        internal_to_target[str(token_id)] = token_id

    for internal_id, target_id in zip(range(5, DRAFT_VOCAB_SIZE), target_order):
        target_to_internal[str(target_id)] = internal_id
        internal_to_target[str(internal_id)] = target_id

    return {
        "target_to_draft": target_to_internal,
        "draft_to_target": internal_to_target,
        "target_only_ids": list(TARGET_ONLY_IDS),
    }


def _context_key(context: Sequence[int]) -> str:
    return ",".join(str(token_id) for token_id in context)


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    vocab_size: int
    special_tokens: Mapping[str, int]
    forward_ticks_per_token: int
    verification_launch_ticks: int = 0
    verification_ticks_per_candidate: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.model_id,
            "vocab_size": self.vocab_size,
            "special_tokens": dict(self.special_tokens),
            "forward_ticks_per_token": self.forward_ticks_per_token,
            "verification_launch_ticks": self.verification_launch_ticks,
            "verification_ticks_per_candidate": self.verification_ticks_per_candidate,
        }


class ModelSuite:
    """Two deterministic model copies over one internal vocabulary."""

    def __init__(
        self,
        seed: int,
        draft: ModelSpec,
        target: ModelSpec,
        mapping: Mapping[str, object],
    ) -> None:
        self.seed = seed
        self.draft = draft
        self.target = target
        self.mapping = dict(mapping)
        self._target_to_internal = {
            int(key): int(value)
            for key, value in dict(mapping["target_to_draft"]).items()
        }
        self._internal_to_target = {
            int(key): int(value)
            for key, value in dict(mapping["draft_to_target"]).items()
        }
        self._cache: dict[tuple[str, tuple[int, ...]], tuple[int, ...]] = {}

    @property
    def vocab_size(self) -> int:
        return self.draft.vocab_size

    def normalize_target_ids(
        self, raw_target_ids: Iterable[int]
    ) -> tuple[list[int], list[int]]:
        """Map target IDs to internal IDs and list unmappable positions."""

        normalized: list[int] = []
        unmappable: list[int] = []
        for position, raw_id in enumerate(raw_target_ids):
            internal_id = self._target_to_internal.get(int(raw_id))
            if internal_id is None:
                normalized.append(UNK_ID)
                unmappable.append(position)
            else:
                normalized.append(internal_id)
        return normalized, unmappable

    def target_token_id(self, internal_token_id: int) -> int:
        try:
            return self._internal_to_target[int(internal_token_id)]
        except KeyError as exc:
            raise ValueError(
                f"internal token {internal_token_id} has no target-vocabulary mapping"
            ) from exc

    def _role_for_context(self, role: str, context: tuple[int, ...]) -> str:
        # Template requests deliberately make the two model copies identical.
        if MARKER_TEMPLATE in context:
            return "shared"
        return role

    def _rank_tokens(self, role: str, context: tuple[int, ...]) -> list[int]:
        context_key = _context_key(context)

        def score(token_id: int) -> int:
            common = stable_u64("intent", self.seed, context_key, token_id) % 1_000_003
            if role == "shared":
                return common * 10
            if role == "draft":
                noise = stable_u64("draft", self.seed, context_key, token_id) % 1_000_003
                return common * 10 + noise * 3
            noise = stable_u64("target", self.seed, context_key, token_id) % 1_000_003
            return common * 10 + noise * 2

        return sorted(GENERATION_TOKEN_IDS, key=lambda token_id: (-score(token_id), token_id))

    def _top_share_bps(
        self, role: str, context: tuple[int, ...], context_key: str
    ) -> int:
        if role == "shared":
            return 3200 + stable_u64("share", self.seed, context_key) % 3301
        return 3200 + stable_u64("share", self.seed, role, context_key) % 3301

    def _base_weights(
        self, role: str, context: tuple[int, ...], ranked: Sequence[int]
    ) -> dict[int, int]:
        context_key = _context_key(context)
        remaining = 10000 - self._top_share_bps(role, context, context_key)
        weights: list[int] = [10000 - remaining]

        # 90% of the non-leading mass is assigned to ranks 1..7.  The final
        # 10% is spread over the long tail.
        fractions = (35, 20, 12, 8, 6, 5, 4)
        used = 0
        for fraction in fractions:
            value = remaining * fraction // 100
            weights.append(value)
            used += value

        tail_ranks = len(ranked) - len(weights)
        tail_mass = remaining - used
        if tail_ranks > 0:
            base, extra = divmod(tail_mass, tail_ranks)
            weights.extend(
                base + (1 if index < extra else 0) for index in range(tail_ranks)
            )
        else:
            weights[-1] += tail_mass

        if sum(weights) != 10000:
            raise AssertionError("internal probability allocation must sum to 10000")
        return {token_id: weight for token_id, weight in zip(ranked, weights)}

    def distribution(self, role: str, context: Sequence[int]) -> tuple[int, ...]:
        """Return a full internal-vocabulary distribution in basis points."""

        if role not in {"draft", "target"}:
            raise ValueError(f"unknown model role: {role}")
        context_tuple = tuple(int(token_id) for token_id in context)
        effective_role = self._role_for_context(role, context_tuple)
        cache_key = (effective_role, context_tuple)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        ranked = self._rank_tokens(effective_role, context_tuple)
        weighted = self._base_weights(effective_role, context_tuple, ranked)
        probabilities = [0] * self.vocab_size
        for token_id, weight in weighted.items():
            probabilities[token_id] = weight
        result = tuple(probabilities)
        if sum(result) != 10000:
            raise AssertionError("model distribution must sum to 10000 basis points")
        self._cache[cache_key] = result
        return result

    def draft_token(self, context: Sequence[int]) -> tuple[int, int, tuple[int, ...]]:
        """Greedily draft one token from draft-model scores only."""

        probabilities = self.distribution("draft", context)
        token_id = max(
            GENERATION_TOKEN_IDS,
            key=lambda candidate: (probabilities[candidate], -candidate),
        )
        return token_id, probabilities[token_id], probabilities

    def residual_resample(
        self,
        draft_probabilities: Sequence[int],
        target_probabilities: Sequence[int],
        draw_u64: int,
    ) -> tuple[int, int, str]:
        """Sample from normalised ``max(0, target - draft)``.

        This is the standard speculative-decoding correction after the first
        rejection.  If the residual is empty, target probabilities are used.
        Returns ``(token_id, residual_mass_bps, method)``.
        """

        residual = [
            max(0, int(target) - int(draft))
            for draft, target in zip(draft_probabilities, target_probabilities)
        ]
        mass = sum(residual)
        method = "target_residual"
        if mass == 0:
            residual = [int(value) for value in target_probabilities]
            mass = sum(residual)
            method = "target_distribution_fallback"
        if mass == 0:
            raise ValueError("cannot resample from an empty target distribution")

        offset = draw_u64 % mass
        cumulative = 0
        for token_id, value in enumerate(residual):
            cumulative += value
            if offset < cumulative:
                return token_id, mass, method
        raise AssertionError("weighted resampling failed to select a token")

    def model_specs(self) -> dict[str, object]:
        return {
            "draft": self.draft.as_dict(),
            "target": self.target.as_dict(),
            "mapping": dict(self.mapping),
        }


def default_model_specs(seed: int) -> tuple[ModelSpec, ModelSpec, dict[str, object]]:
    draft = ModelSpec(
        model_id="draft-transit-lite",
        vocab_size=DRAFT_VOCAB_SIZE,
        special_tokens=dict(SPECIAL_IDS),
        forward_ticks_per_token=2,
    )
    target = ModelSpec(
        model_id="target-transit-full",
        vocab_size=TARGET_VOCAB_SIZE,
        special_tokens=dict(SPECIAL_IDS),
        forward_ticks_per_token=20,
        verification_launch_ticks=3,
        verification_ticks_per_candidate=2,
    )
    return draft, target, build_token_mapping(seed)


__all__ = [
    "ACCEPTANCE_MODE",
    "CLASS_BY_MARKER",
    "DRAFT_VOCAB_SIZE",
    "FALLBACK_RULE",
    "GENERATION_TOKEN_IDS",
    "MARKER_BY_CLASS",
    "MARKER_REROUTE",
    "MARKER_TEMPLATE",
    "MARKER_TRANSFER",
    "ModelSpec",
    "ModelSuite",
    "SPECIAL_IDS",
    "SPECIAL_NAMES",
    "TARGET_ONLY_IDS",
    "TARGET_VOCAB_SIZE",
    "UNK_ID",
    "build_token_mapping",
    "default_model_specs",
    "distribution_digest",
    "stable_u64",
]
