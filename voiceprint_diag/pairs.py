"""Speaker-pair manifest parsing with stable original ordering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class PairManifestError(ValueError):
    pass


@dataclass(frozen=True)
class PairSpec:
    index: int
    pair_id: str
    left: str
    right: str
    relation: str


@dataclass(frozen=True)
class SpeakerGroup:
    speaker_id: str
    samples: tuple[str, ...]


def normalise_sample_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise PairManifestError("sample name must be a non-empty string")
    text = name.replace("\\", "/")
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts:
        raise PairManifestError(f"sample name must be relative: {name}")
    return pure.as_posix().lstrip("./")


def _pair_from_mapping(item: dict[str, Any], index: int, default_relation: str | None) -> PairSpec:
    left = item.get("left", item.get("a", item.get("sample_a")))
    right = item.get("right", item.get("b", item.get("sample_b")))
    relation = item.get("relation", item.get("kind", default_relation))
    if relation not in {"same", "cross"}:
        raise PairManifestError("pair relation must be 'same' or 'cross'")
    pair_id = item.get("id", f"{relation}-{index + 1:04d}")
    if not isinstance(pair_id, str) or not pair_id:
        raise PairManifestError("pair id must be a non-empty string")
    return PairSpec(index, pair_id, normalise_sample_name(left), normalise_sample_name(right), relation)


def _expand_pairs(value: Any, default_relation: str | None, start_index: int) -> list[PairSpec]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PairManifestError("pair collections must be arrays")
    result: list[PairSpec] = []
    for offset, item in enumerate(value):
        index = start_index + offset
        if isinstance(item, dict):
            result.append(_pair_from_mapping(item, index, default_relation))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            if default_relation is None:
                raise PairManifestError("list-form pairs require same_speaker_pairs/cross_speaker_pairs")
            result.append(
                PairSpec(
                    index,
                    f"{default_relation}-{index + 1:04d}",
                    normalise_sample_name(item[0]),
                    normalise_sample_name(item[1]),
                    default_relation,
                )
            )
        else:
            raise PairManifestError("each pair must be an object or a two-item array")
    return result


def load_pair_manifest(raw: Any) -> tuple[list[PairSpec], list[SpeakerGroup]]:
    if not isinstance(raw, dict):
        raise PairManifestError("pair manifest must be an object")
    pairs: list[PairSpec] = []
    if "pairs" in raw:
        pairs.extend(_expand_pairs(raw["pairs"], None, len(pairs)))
    if "same_speaker_pairs" in raw:
        pairs.extend(_expand_pairs(raw["same_speaker_pairs"], "same", len(pairs)))
    if "cross_speaker_pairs" in raw:
        pairs.extend(_expand_pairs(raw["cross_speaker_pairs"], "cross", len(pairs)))
    if not pairs:
        raise PairManifestError("pair manifest contains no pairs")
    ids: set[str] = set()
    for pair in pairs:
        if pair.pair_id in ids:
            raise PairManifestError(f"duplicate pair id: {pair.pair_id}")
        ids.add(pair.pair_id)
    groups: list[SpeakerGroup] = []
    raw_groups = raw.get("speakers", [])
    if isinstance(raw_groups, dict):
        for speaker_id in sorted(raw_groups):
            samples = raw_groups[speaker_id]
            if not isinstance(samples, list):
                raise PairManifestError("speaker samples must be arrays")
            groups.append(SpeakerGroup(str(speaker_id), tuple(normalise_sample_name(item) for item in samples)))
    elif isinstance(raw_groups, list):
        for item in raw_groups:
            if not isinstance(item, dict):
                raise PairManifestError("speaker groups must be objects")
            speaker_id = item.get("id", item.get("speaker"))
            samples = item.get("samples")
            if not isinstance(speaker_id, str) or not isinstance(samples, list):
                raise PairManifestError("speaker group needs id and samples")
            groups.append(SpeakerGroup(speaker_id, tuple(normalise_sample_name(value) for value in samples)))
    elif raw_groups not in (None, {}):
        raise PairManifestError("speakers must be an object or array")
    groups.sort(key=lambda group: group.speaker_id)
    return pairs, groups

