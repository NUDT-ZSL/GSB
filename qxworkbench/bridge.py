"""Cross-language term bridge resource parsing and version diffs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import format_corrupt, unsupported_version
from .models import BridgeEntry, BridgeResource, LanguagePair
from .util import checksum_object, require_float, require_int, stable_json_bytes

_BRIDGE_FORMAT_VERSION = "bridge-v1"


def bridge_checksum_payload(resource: BridgeResource) -> dict[str, Any]:
    """Content checksum excludes both the release number and stored checksum."""

    return {
        "format_version": resource.format_version,
        "language_pairs": [pair.to_dict() for pair in resource.language_pairs],
        "entries": [entry.to_dict() for entry in resource.entries],
    }


def compute_bridge_checksum(resource: BridgeResource) -> str:
    return checksum_object(bridge_checksum_payload(resource))


def parse_bridge(path: str | Path) -> BridgeResource:
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise format_corrupt(
            "cannot read bridge resource",
            source=str(source),
            reason=str(exc),
        ) from exc
    return parse_bridge_text(raw, source=str(source))


def parse_bridge_text(text: str, *, source: str = "<memory>") -> BridgeResource:
    import json

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise format_corrupt(
            "invalid bridge JSON",
            source=source,
            line=exc.lineno,
            column=exc.colno,
            reason=exc.msg,
        ) from exc
    if not isinstance(document, dict):
        raise format_corrupt("bridge resource must be an object", source=source)

    # Snapshot manifests wrap the resource in a "resource" member.
    if "resource" in document:
        wrapped_checksum = document.get("checksum")
        document = document["resource"]
        if not isinstance(document, dict):
            raise format_corrupt("wrapped bridge resource is invalid", source=source)
        if wrapped_checksum is not None:
            document = dict(document)
            document.setdefault("checksum", wrapped_checksum)

    format_version = document.get("format_version", _BRIDGE_FORMAT_VERSION)
    if format_version != _BRIDGE_FORMAT_VERSION:
        raise unsupported_version(
            "unsupported bridge resource format",
            source=source,
            version=format_version,
        )
    version = require_int(
        document.get("version"),
        field="version",
        minimum=1,
        location={"source": source},
    )

    pairs_raw = document.get("language_pairs")
    if not isinstance(pairs_raw, list):
        raise format_corrupt(
            "bridge language_pairs must be an array",
            source=source,
        )
    pairs: list[LanguagePair] = []
    pair_keys: set[tuple[str, str]] = set()
    for index, pair_raw in enumerate(pairs_raw):
        if not isinstance(pair_raw, dict):
            raise format_corrupt(
                "language pair must be an object",
                source=source,
                pair_index=index,
            )
        source_lang = pair_raw.get("source_lang")
        target_lang = pair_raw.get("target_lang")
        if (
            not isinstance(source_lang, str)
            or not source_lang
            or not isinstance(target_lang, str)
            or not target_lang
        ):
            raise format_corrupt(
                "language pair requires source_lang and target_lang",
                source=source,
                pair_index=index,
            )
        key = (source_lang, target_lang)
        if key in pair_keys:
            raise format_corrupt(
                "duplicate language pair",
                source=source,
                source_lang=source_lang,
                target_lang=target_lang,
            )
        pair_keys.add(key)
        pairs.append(LanguagePair(source_lang, target_lang))
    pairs.sort(key=lambda pair: (pair.source_lang, pair.target_lang))

    entries_raw = document.get("entries")
    if not isinstance(entries_raw, list):
        raise format_corrupt("bridge entries must be an array", source=source)
    entries: list[BridgeEntry] = []
    entry_keys: set[tuple[str, str, str, str]] = set()
    for index, entry_raw in enumerate(entries_raw):
        if not isinstance(entry_raw, dict):
            raise format_corrupt(
                "bridge entry must be an object",
                source=source,
                entry_index=index,
            )
        required = ("source_lang", "target_lang", "source_term", "target_term")
        if any(
            not isinstance(entry_raw.get(field), str) or not entry_raw[field]
            for field in required
        ):
            raise format_corrupt(
                "bridge entry is missing required string fields",
                source=source,
                entry_index=index,
                required_fields=list(required),
            )
        source_lang = entry_raw["source_lang"]
        target_lang = entry_raw["target_lang"]
        if (source_lang, target_lang) not in pair_keys:
            raise format_corrupt(
                "bridge entry direction is absent from language_pairs",
                source=source,
                entry_index=index,
                source_lang=source_lang,
                target_lang=target_lang,
            )
        confidence = require_float(
            entry_raw.get("confidence"),
            field="confidence",
            minimum=0.0,
            maximum=1.0,
            location={"source": source, "entry_index": index},
        )
        entry = BridgeEntry(
            source_lang=source_lang,
            target_lang=target_lang,
            source_term=entry_raw["source_term"],
            target_term=entry_raw["target_term"],
            confidence=confidence,
        )
        if entry.key in entry_keys:
            raise format_corrupt(
                "duplicate bridge entry",
                source=source,
                entry=entry.to_dict(),
            )
        entry_keys.add(entry.key)
        entries.append(entry)
    entries.sort(key=lambda entry: entry.key)

    provisional = BridgeResource(
        version=version,
        format_version=format_version,
        language_pairs=tuple(pairs),
        entries=tuple(entries),
        checksum="",
    )
    checksum = compute_bridge_checksum(provisional)
    stored_checksum = document.get("checksum")
    if stored_checksum is not None and stored_checksum != checksum:
        raise format_corrupt(
            "bridge checksum does not match content",
            source=source,
            stored_checksum=stored_checksum,
            computed_checksum=checksum,
        )
    return BridgeResource(
        version=provisional.version,
        format_version=provisional.format_version,
        language_pairs=provisional.language_pairs,
        entries=provisional.entries,
        checksum=checksum,
    )


def serialize_bridge(resource: BridgeResource, *, include_checksum: bool = True) -> bytes:
    return stable_json_bytes(resource.to_dict(include_checksum=include_checksum))


def affected_partitions(
    old: BridgeResource | None,
    new: BridgeResource,
) -> tuple[str, ...]:
    """Return target-language partitions touched by changed bridge terms.

    Confidence-only changes count because scoring changes. Unchanged terms in a
    language do not cause that language's partition to be flagged.
    """

    if old is None:
        return tuple(sorted({entry.target_lang for entry in new.entries}))
    old_entries = {entry.key: entry for entry in old.entries}
    new_entries = {entry.key: entry for entry in new.entries}
    changed_languages: set[str] = set()
    for key in sorted(set(old_entries) | set(new_entries)):
        old_entry = old_entries.get(key)
        new_entry = new_entries.get(key)
        if old_entry != new_entry:
            changed_languages.add((new_entry or old_entry).target_lang)
    return tuple(sorted(changed_languages))


def entries_for(
    resource: BridgeResource,
    source_lang: str,
    target_lang: str,
    source_term: str,
) -> tuple[BridgeEntry, ...]:
    return tuple(
        entry
        for entry in resource.entries
        if entry.source_lang == source_lang
        and entry.target_lang == target_lang
        and entry.source_term == source_term
    )


def language_pair_keys(resource: BridgeResource) -> set[tuple[str, str]]:
    return {(pair.source_lang, pair.target_lang) for pair in resource.language_pairs}


def bridge_entries_dict(resource: BridgeResource) -> dict[tuple[str, str, str], list[BridgeEntry]]:
    grouped: dict[tuple[str, str, str], list[BridgeEntry]] = {}
    for entry in resource.entries:
        grouped.setdefault(
            (entry.source_lang, entry.target_lang, entry.source_term),
            [],
        ).append(entry)
    return grouped
