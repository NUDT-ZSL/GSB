from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Iterable

from .codes import E_CANONICAL_ALIAS, E_PATH_ESCAPE, E_PATH_SYNTAX

ILLEGAL_WINDOWS_CHARS = set('<>:"|?*')
RESERVED_DOS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class PathSyntaxError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PathPolicy:
    policy_id: str
    casefold: bool = True
    unicode_form: str = "NFC"  # NFC, NFD, NFKC, NFKD, NONE

    @classmethod
    def default_windows(cls) -> "PathPolicy":
        return cls(policy_id="windows-casefold-nfc-v1", casefold=True, unicode_form="NFC")

    @classmethod
    def strict(cls) -> "PathPolicy":
        return cls(policy_id="strict-dot-only-v1", casefold=False, unicode_form="NONE")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "casefold": self.casefold,
            "unicode_form": self.unicode_form,
        }


@dataclass(frozen=True)
class PathInfo:
    raw_path: str
    raw_bytes_hex: str
    canonical_key: str
    drive: str
    absolute: bool
    segments: tuple[str, ...]
    syntax_sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_path": self.raw_path,
            "raw_path_bytes_hex": self.raw_bytes_hex,
            "canonical_key": self.canonical_key,
            "drive": self.drive,
            "absolute": self.absolute,
            "segments": list(self.segments),
            "syntax_sources": list(self.syntax_sources),
        }


def raw_bytes_hex(path: str | bytes) -> str:
    if isinstance(path, bytes):
        return path.hex()
    return path.encode("utf-8").hex()


def _decode_path(path: str | bytes, path_hex: str | None) -> tuple[str, str]:
    if path_hex is not None:
        try:
            raw_bytes = bytes.fromhex(path_hex)
        except ValueError as exc:
            raise PathSyntaxError(E_PATH_SYNTAX, "path_bytes_hex is not valid hex") from exc
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PathSyntaxError(E_PATH_SYNTAX, "path bytes are not valid UTF-8") from exc
        if isinstance(path, str) and path != text:
            # The explicit byte form is authoritative.
            return text, path_hex.lower()
        return text, raw_bytes.hex()
    if isinstance(path, bytes):
        return path.decode("utf-8"), path.hex()
    if not isinstance(path, str):
        raise PathSyntaxError(E_PATH_SYNTAX, "path must be a string")
    return path, raw_bytes_hex(path)


def validate_syntax(text: str) -> tuple[str, bool, list[str]]:
    if not text:
        raise PathSyntaxError(E_PATH_SYNTAX, "empty path")
    sources: list[str] = []
    if "\x00" in text:
        raise PathSyntaxError(E_PATH_SYNTAX, "NUL byte in path")
    for index, char in enumerate(text):
        if ord(char) < 32:
            raise PathSyntaxError(E_PATH_SYNTAX, "control character in path")
        if char in ILLEGAL_WINDOWS_CHARS:
            if char == ":" and index == 1 and len(text) >= 2 and text[0].isalpha():
                continue
            raise PathSyntaxError(E_PATH_SYNTAX, f"illegal NTFS path character {char!r}")

    normalized_separators = text
    if "\\" in text:
        sources.append("backslash_separator")
        normalized_separators = text.replace("\\", "/")

    drive = ""
    rest = normalized_separators
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        drive = text[0].upper() + ":"
        rest = normalized_separators[2:]
        # The only permitted colon is the drive separator.
        if ":" in rest:
            raise PathSyntaxError(E_PATH_SYNTAX, "unexpected colon in path")
    elif ":" in text:
        raise PathSyntaxError(E_PATH_SYNTAX, "colon is only valid in a drive prefix")

    absolute = rest.startswith("/")
    raw_segments = rest.split("/")
    if absolute and raw_segments and raw_segments[0] == "":
        raw_segments = raw_segments[1:]
    if absolute and raw_segments and raw_segments[-1] == "":
        raw_segments = raw_segments[:-1]

    for index, segment in enumerate(raw_segments):
        if segment == "":
            sources.append("repeated_or_trailing_separator")
            # Repeated separators are accepted as harmless non-canonical syntax.
            continue
        if segment in {".", ".."}:
            continue
        stem = segment.split(".", 1)[0].upper()
        if stem in RESERVED_DOS_NAMES:
            raise PathSyntaxError(E_PATH_SYNTAX, f"reserved DOS device name: {segment}")
        if segment.endswith((" ", ".")) and index == len(raw_segments) - 1:
            # Windows trims trailing dots/spaces; reject ambiguity rather than silently trim.
            raise PathSyntaxError(E_PATH_SYNTAX, f"path segment has unsafe trailing character: {segment!r}")
    return drive, absolute, sources


def canonicalize_path(
    path: str | bytes,
    policy: PathPolicy | None = None,
    path_bytes_hex_value: str | None = None,
) -> PathInfo:
    policy = policy or PathPolicy.default_windows()
    text, hex_value = _decode_path(path, path_bytes_hex_value)
    drive, absolute, sources = validate_syntax(text)

    formed = text.replace("\\", "/")
    unicode_changed = False
    if policy.unicode_form and policy.unicode_form.upper() != "NONE":
        form = policy.unicode_form.upper()
        if form not in {"NFC", "NFD", "NFKC", "NFKD"}:
            raise PathSyntaxError(E_PATH_SYNTAX, f"unknown Unicode form: {policy.unicode_form}")
        normalized = unicodedata.normalize(form, formed)
        unicode_changed = normalized != formed
        formed = normalized
    if policy.casefold:
        folded = formed.casefold()
        if folded != formed:
            sources.append("casefold")
        if any(len(ch.casefold()) != len(ch) for ch in formed):
            sources.append("casefold_expansion")
        formed = folded

    if unicode_changed:
        sources.append("unicode_normalization")

    if len(text.encode("utf-8")) != len(formed.encode("utf-8")):
        if "normalization_or_case_length_delta" not in sources:
            sources.append("normalization_or_case_length_delta")

    rest = formed
    if drive:
        # Drive was removed before validation but formed still contains it.
        rest = formed[2:]
    if rest.startswith("/"):
        rest = rest[1:]
    if rest.endswith("/"):
        rest = rest[:-1]

    resolved: list[str] = []
    for segment in rest.split("/") if rest else []:
        if segment == "":
            continue
        if segment == ".":
            sources.append("dot_segment")
            continue
        if segment == "..":
            sources.append("parent_segment")
            if resolved and resolved[-1] != "..":
                resolved.pop()
            elif absolute or drive:
                raise PathSyntaxError(E_PATH_ESCAPE, "parent segment escapes path root")
            else:
                resolved.append("..")
            continue
        resolved.append(segment)

    prefix = drive + ("/" if absolute else "")
    key = prefix + "/".join(resolved)
    if absolute and not key.startswith(drive + "/"):
        key = prefix + "/".join(resolved)
    return PathInfo(
        raw_path=text,
        raw_bytes_hex=hex_value,
        canonical_key=key,
        drive=drive,
        absolute=absolute,
        segments=tuple(resolved),
        syntax_sources=tuple(sorted(set(sources))),
    )


def same_key_aliases(objects: Iterable[Any], policy: PathPolicy) -> list[dict[str, Any]]:
    groups: dict[str, list[Any]] = {}
    for obj in objects:
        key = obj.path_info[policy.policy_id].canonical_key if hasattr(obj, "path_info") else obj["canonical_keys"][policy.policy_id]
        groups.setdefault(key, []).append(obj)

    findings: list[dict[str, Any]] = []
    for key, members in sorted(groups.items()):
        byte_groups: dict[str, list[Any]] = {}
        for member in members:
            hex_value = (
                member.path_info[policy.policy_id].raw_bytes_hex
                if hasattr(member, "path_info")
                else member["raw_path_bytes_hex"]
            )
            byte_groups.setdefault(hex_value, []).append(member)
        if len(byte_groups) <= 1:
            continue
        pairs = []
        hexes = sorted(byte_groups)
        for i, left_hex in enumerate(hexes):
            for right_hex in hexes[i + 1 :]:
                for left in byte_groups[left_hex]:
                    for right in byte_groups[right_hex]:
                        if _id(left) == _id(right):
                            continue
                        pairs.append(
                            {
                                "object_id_a": _id(left),
                                "object_id_b": _id(right),
                                "raw_path_a": _raw(left),
                                "raw_path_b": _raw(right),
                                "path_bytes_hex_a": left_hex,
                                "path_bytes_hex_b": right_hex,
                            }
                        )
        findings.append(
            {
                "code": E_CANONICAL_ALIAS,
                "policy_id": policy.policy_id,
                "canonical_key": key,
                "object_ids": [_id(m) for m in members],
                "distinct_raw_byte_forms": len(byte_groups),
                "pairs": pairs,
            }
        )
    return findings


def _id(obj: Any) -> str:
    return obj.object_id if hasattr(obj, "object_id") else obj["object_id"]


def _raw(obj: Any) -> str:
    if hasattr(obj, "path"):
        return obj.path
    if hasattr(obj, "raw_path"):
        return obj.raw_path
    return obj["path"]


def quantify_policy_sources(objects: Iterable[Any], policy: PathPolicy) -> dict[str, Any]:
    objects = list(objects)
    source_counts: dict[str, int] = {}
    collision_groups = 0
    key_to_hex: dict[str, set[str]] = {}
    byte_deltas: list[int] = []
    changed_records = 0
    for obj in objects:
        info = obj.path_info[policy.policy_id] if hasattr(obj, "path_info") else obj["path_info"][policy.policy_id]
        for source in info.syntax_sources:
            source_counts[source] = source_counts.get(source, 0) + 1
        original_len = len(bytes.fromhex(info.raw_bytes_hex))
        canonical_len = len(info.canonical_key.encode("utf-8"))
        byte_deltas.append(abs(original_len - canonical_len))
        if info.raw_path != info.canonical_key:
            changed_records += 1
        key_to_hex.setdefault(info.canonical_key, set()).add(info.raw_bytes_hex)
    collision_groups = sum(1 for forms in key_to_hex.values() if len(forms) > 1)
    return {
        "policy": policy.to_dict(),
        "object_count": len(objects),
        "changed_record_count": changed_records,
        "same_key_distinct_byte_groups": collision_groups,
        "source_counts": dict(sorted(source_counts.items())),
        "max_byte_length_delta": max(byte_deltas, default=0),
        "comparison_error_sources": [
            {
                "strategy": "casefold",
                "active": policy.casefold,
                "quantification": source_counts.get("casefold", 0)
                + source_counts.get("casefold_expansion", 0),
                "unit": "path_records_with_case_mapping",
            },
            {
                "strategy": "unicode_normalization",
                "active": policy.unicode_form != "NONE",
                "form": policy.unicode_form,
                "quantification": source_counts.get("unicode_normalization", 0),
                "unit": "path_records_with_normalization_byte_change",
            },
            {
                "strategy": "dot_and_parent_segments",
                "active": True,
                "quantification": source_counts.get("dot_segment", 0)
                + source_counts.get("parent_segment", 0),
                "unit": "segment_occurrences",
            },
        ],
    }
