"""Normalization rules for the three domain vocabulary encodings."""

from __future__ import annotations

import re
from dataclasses import dataclass

VERSIONS = ("v1", "v2", "v3")
SEGMENT_SEPARATOR = "::"


@dataclass(frozen=True)
class NormalizedIdentifier:
    version: str
    raw: str
    segments: tuple[str, ...]

    @property
    def token(self) -> str:
        return SEGMENT_SEPARATOR.join(self.segments)


def render_identifier(segments: tuple[str, ...] | list[str], version: str) -> str:
    """Render canonical segments using one of the three on-disk formats."""
    if version not in VERSIONS:
        raise ValueError(f"unsupported vocabulary version: {version}")
    if not segments:
        raise ValueError("an identifier must contain at least one segment")

    if version == "v1":
        return "_".join(segments)
    if version == "v2":
        rendered = segments[0]
        for index, segment in enumerate(segments[1:]):
            delimiter = "-" if index % 2 == 0 else "/"
            rendered += delimiter + segment
        return rendered

    groups: list[str] = []
    for segment in segments:
        if groups and segment.isdigit() and groups[-1].isdigit():
            groups[-1] += segment
        else:
            groups.append(segment)
    return ".".join(groups)


def _split_merged_numeric(block: str, width: int = 4) -> list[str]:
    if not block.isdigit() or len(block) <= width:
        return [block]
    return [block[index : index + width] for index in range(0, len(block), width)]


def normalize_identifier(raw: str, version: str) -> NormalizedIdentifier:
    """Normalize delimiters while preserving the selected version's semantics.

    v1 uses ``_`` as its separator. v2 uses both ``-`` and ``/`` as
    separators. v3 uses ``.`` and merges adjacent numeric segments; because
    domain numeric atoms are four digits, a merged run is split every four
    digits. A run with a different implied width is intentionally surfaced as
    a conflict rather than silently guessed by a learned tokenizer.
    """
    if version not in VERSIONS:
        raise ValueError(f"unsupported vocabulary version: {version}")
    if not raw:
        raise ValueError("identifier must not be empty")

    if version == "v1":
        segments = raw.split("_")
    elif version == "v2":
        segments = re.split(r"[-/]", raw)
    else:
        groups = raw.split(".")
        segments = []
        for group in groups:
            segments.extend(_split_merged_numeric(group))

    if any(segment == "" for segment in segments):
        raise ValueError(f"empty segment after normalizing {raw!r} as {version}")
    return NormalizedIdentifier(version=version, raw=raw, segments=tuple(segments))


def compare_version_segmentations(
    segmentations: dict[str, NormalizedIdentifier],
) -> dict[str, object]:
    """Compare version-wise splits using both ordered sequences and token sets."""
    ordered = {version: list(segmentations[version].segments) for version in VERSIONS}
    token_sets = {
        version: sorted(set(segmentations[version].segments)) for version in VERSIONS
    }
    ordered_differences = {
        f"{left}_vs_{right}": ordered[left] != ordered[right]
        for left, right in (("v1", "v2"), ("v1", "v3"), ("v2", "v3"))
    }
    set_differences = {
        f"{left}_vs_{right}": token_sets[left] != token_sets[right]
        for left, right in (("v1", "v2"), ("v1", "v3"), ("v2", "v3"))
    }

    reasons: list[str] = []
    if set_differences["v1_vs_v2"]:
        reasons.append("delimiter_ambiguity")
    if set_differences["v1_vs_v3"]:
        reasons.append("numeric_merge_ambiguity")

    return {
        "has_difference": any(set_differences.values()) or any(ordered_differences.values()),
        "token_set_difference": any(set_differences.values()),
        "ordered_segment_difference": any(ordered_differences.values()),
        "ordered_differences": ordered_differences,
        "token_set_differences": set_differences,
        "reasons": reasons,
        "version_segments": ordered,
        "version_token_sets": token_sets,
    }

