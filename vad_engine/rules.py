"""V1/V2 VAD rule readers, alignment, and bidirectional migration."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    E_FORMAT_CORRUPT,
    E_STATE_INCONSISTENT,
    E_TIMELINE_OVERLAP,
    E_VERSION_UNSUPPORTED,
    M_DROP_GENERATION,
    M_DROP_SCOPE,
    M_DROP_TOMBSTONE,
    EngineError,
)
from .util import (
    atomic_write_json,
    load_json_file,
    object_hash,
    parse_float,
    parse_int,
    quantize,
    require_field,
    require_list,
    require_mapping,
    seconds_to_ms,
)


@dataclass
class Rule:
    rule_id: str
    speaker_id: str
    effective_ms: int
    end_ms: int | None
    energy_threshold: float
    min_silence_ms: int
    rule_generation: int
    scope: str
    tombstone: bool
    shard_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "speaker_id": self.speaker_id,
            "effective_ms": self.effective_ms,
            "end_ms": self.end_ms,
            "energy_threshold": quantize(self.energy_threshold),
            "min_silence_ms": self.min_silence_ms,
            "rule_generation": self.rule_generation,
            "scope": self.scope,
            "tombstone": self.tombstone,
            "shard_index": self.shard_index,
        }


@dataclass
class RuleSet:
    format_version: str
    session_id: str
    sample_rate: int
    frame_ms: int
    hop_ms: int
    noise_gate: float
    rules: list[Rule]
    source_path: str
    source_kind: str
    revision_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rules.sort(
            key=lambda rule: (
                rule.shard_index,
                rule.effective_ms,
                rule.rule_generation,
                rule.rule_id,
            )
        )
        if not self.revision_hash:
            self.revision_hash = object_hash(
                {
                    "format_version": self.format_version,
                    "session_id": self.session_id,
                    "sample_rate": self.sample_rate,
                    "frame_ms": self.frame_ms,
                    "hop_ms": self.hop_ms,
                    "noise_gate": quantize(self.noise_gate),
                    "rules": [rule.to_dict() for rule in self.rules],
                }
            )

    @property
    def version_id(self) -> str:
        return f"{self.format_version}@{self.revision_hash[:16]}"

    def active_rule(self, speaker_id: str, time_ms: int) -> Rule | None:
        candidates = [
            rule
            for rule in self.rules
            if rule.speaker_id == speaker_id
            and rule.effective_ms <= time_ms
            and (rule.end_ms is None or time_ms < rule.end_ms)
        ]
        if not candidates:
            return None
        selected = max(
            candidates,
            key=lambda rule: (rule.rule_generation, rule.effective_ms, rule.rule_id),
        )
        return None if selected.tombstone else selected

    def active_tombstone(self, speaker_id: str, time_ms: int) -> Rule | None:
        candidates = [
            rule
            for rule in self.rules
            if rule.tombstone
            and rule.speaker_id == speaker_id
            and rule.effective_ms <= time_ms
            and (rule.end_ms is None or time_ms < rule.end_ms)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda rule: (rule.rule_generation, rule.effective_ms, rule.rule_id),
        )

    def curve_at(self, speaker_id: str, time_ms: int) -> tuple[float, int]:
        rule = self.active_rule(speaker_id, time_ms)
        if rule is None:
            return quantize(self.noise_gate), 100
        return rule.energy_threshold, rule.min_silence_ms

    def speakers(self) -> tuple[str, ...]:
        return tuple(sorted({rule.speaker_id for rule in self.rules}))


@dataclass
class MigrationResult:
    source: RuleSet
    migrated: RuleSet
    output_path: str
    direction: str
    business_code: str
    provenance: list[dict[str, Any]] = field(default_factory=list)
    discarded: list[dict[str, Any]] = field(default_factory=list)
    dropped_counts: dict[str, int] = field(default_factory=dict)
    semantic_checks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "business_code": self.business_code,
            "source": {
                "format": self.source.format_version,
                "revision": self.source.revision_hash,
                "rule_count": len(self.source.rules),
            },
            "migrated": {
                "format": self.migrated.format_version,
                "revision": self.migrated.revision_hash,
                "rule_count": len(self.migrated.rules),
                "path": self.output_path,
            },
            "provenance": self.provenance,
            "discarded": self.discarded,
            "dropped_counts": self.dropped_counts,
            "semantic_checks": self.semantic_checks,
        }


def _rule_sort_key(rule: Rule) -> tuple[int, str, int, str]:
    return (rule.effective_ms, rule.speaker_id, rule.rule_generation, rule.rule_id)


def _check_overlaps(rules: Iterable[Rule]) -> None:
    grouped: dict[tuple[str, int], list[Rule]] = {}
    for rule in rules:
        grouped.setdefault((rule.speaker_id, rule.rule_generation), []).append(rule)
    for (speaker_id, generation), group in grouped.items():
        ordered = sorted(group, key=lambda item: (item.effective_ms, item.rule_id))
        previous: Rule | None = None
        previous_end: int | None = None
        for rule in ordered:
            if previous is not None and previous.effective_ms == rule.effective_ms:
                raise EngineError(
                    E_TIMELINE_OVERLAP,
                    "same speaker/generation has duplicate effective time",
                    {
                        "speaker_id": speaker_id,
                        "rule_generation": generation,
                        "effective_ms": rule.effective_ms,
                        "first_rule": previous.rule_id,
                        "second_rule": rule.rule_id,
                    },
                )
            if (
                previous is not None
                and previous_end is not None
                and previous_end > rule.effective_ms
            ):
                raise EngineError(
                    E_TIMELINE_OVERLAP,
                    "same speaker/generation intervals overlap",
                    {
                        "speaker_id": speaker_id,
                        "rule_generation": generation,
                        "previous_rule": previous.rule_id,
                        "previous_end_ms": previous_end,
                        "current_rule": rule.rule_id,
                        "current_start_ms": rule.effective_ms,
                    },
                )
            previous = rule
            previous_end = rule.end_ms
            if previous_end is not None and previous_end <= rule.effective_ms:
                raise EngineError(
                    E_FORMAT_CORRUPT,
                    "rule interval end must be greater than start",
                    {
                        "rule_id": rule.rule_id,
                        "effective_ms": rule.effective_ms,
                        "end_ms": rule.end_ms,
                    },
                )


def _v1_rule_ids(rule: Rule, index: int) -> str:
    return f"v1:{index:06d}:{rule.speaker_id}:{rule.effective_ms}"


def parse_v1_payload(payload: Any, source_path: str = "<memory>") -> RuleSet:
    data = require_mapping(payload, "v1 root")
    session_id = str(require_field(data, "session_id", "v1 root"))
    sample_rate = parse_int(require_field(data, "sample_rate", "v1 root"), "sample_rate", minimum=1)
    frame_ms = parse_int(require_field(data, "frame_ms", "v1 root"), "frame_ms", minimum=1)
    hop_ms = parse_int(require_field(data, "hop_ms", "v1 root"), "hop_ms", minimum=1)
    noise_gate = parse_float(
        require_field(data, "noise_gate", "v1 root"), "noise_gate", minimum=0.0
    )
    raw_rules = require_list(require_field(data, "rules", "v1 root"), "rules")
    if not raw_rules:
        raise EngineError(E_FORMAT_CORRUPT, "v1 rules array is empty", {"path": source_path})

    parsed: list[Rule] = []
    for index, raw in enumerate(raw_rules):
        context = f"rules[{index}]"
        rule_data = require_mapping(raw, context)
        speaker_id = str(require_field(rule_data, "speaker_id", context))
        start_ms = seconds_to_ms(require_field(rule_data, "start_sec", context), f"{context}.start_sec")
        end_value = rule_data.get("end_sec")
        end_ms = (
            seconds_to_ms(end_value, f"{context}.end_sec") if end_value is not None else None
        )
        threshold = parse_float(
            require_field(rule_data, "energy_threshold", context),
            f"{context}.energy_threshold",
            minimum=0.0,
        )
        min_silence = parse_int(
            require_field(rule_data, "min_silence_ms", context),
            f"{context}.min_silence_ms",
            minimum=0,
        )
        parsed.append(
            Rule(
                rule_id=str(rule_data.get("rule_id") or f"v1:{index:06d}"),
                speaker_id=speaker_id,
                effective_ms=start_ms,
                end_ms=end_ms,
                energy_threshold=threshold,
                min_silence_ms=min_silence,
                rule_generation=1,
                scope="speaker",
                tombstone=False,
                shard_index=0,
            )
        )

    grouped: dict[str, list[Rule]] = {}
    for rule in parsed:
        grouped.setdefault(rule.speaker_id, []).append(rule)
    for group in grouped.values():
        group.sort(key=lambda item: (item.effective_ms, item.rule_id))
        for index, rule in enumerate(group):
            if rule.end_ms is None:
                rule.end_ms = group[index + 1].effective_ms if index + 1 < len(group) else None

    _check_overlaps(parsed)
    return RuleSet(
        format_version="v1",
        session_id=session_id,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        hop_ms=hop_ms,
        noise_gate=noise_gate,
        rules=parsed,
        source_path=source_path,
        source_kind="v1-json",
        extra={"speakers": data.get("speakers", [])},
    )


def _load_v2_manifest(manifest_path: Path) -> RuleSet:
    manifest = require_mapping(load_json_file(manifest_path, context="v2 manifest"), "v2 manifest")
    format_name = require_field(manifest, "format", "v2 manifest")
    if format_name != "vad-rules/v2":
        raise EngineError(
            E_VERSION_UNSUPPORTED,
            "unsupported VAD rule format",
            {"format": format_name, "path": str(manifest_path)},
        )
    version = parse_int(
        require_field(manifest, "version", "v2 manifest"), "version", minimum=1
    )
    if version != 2:
        raise EngineError(
            E_VERSION_UNSUPPORTED,
            "unsupported VAD rule version",
            {"version": version, "path": str(manifest_path)},
        )
    session_id = str(require_field(manifest, "session_id", "v2 manifest"))
    sample_rate = parse_int(
        require_field(manifest, "sample_rate", "v2 manifest"), "sample_rate", minimum=1
    )
    frame_ms = parse_int(require_field(manifest, "frame_ms", "v2 manifest"), "frame_ms", minimum=1)
    hop_ms = parse_int(require_field(manifest, "hop_ms", "v2 manifest"), "hop_ms", minimum=1)
    noise_gate = parse_float(
        require_field(manifest, "noise_gate", "v2 manifest"),
        "noise_gate",
        minimum=0.0,
    )
    database_name = str(require_field(manifest, "metadata_db", "v2 manifest"))
    shards = require_list(require_field(manifest, "shards", "v2 manifest"), "shards")
    if not shards:
        raise EngineError(E_FORMAT_CORRUPT, "v2 shards array is empty")

    shard_rules: dict[str, dict[str, Any]] = {}
    for index, raw_shard in enumerate(shards):
        shard_ref = require_mapping(raw_shard, f"shards[{index}]")
        shard_index = parse_int(
            require_field(shard_ref, "shard_index", f"shards[{index}]"),
            f"shards[{index}].shard_index",
            minimum=0,
        )
        relative = str(require_field(shard_ref, "path", f"shards[{index}]"))
        shard_path = manifest_path.parent / relative
        shard = require_mapping(load_json_file(shard_path, context="v2 shard"), "v2 shard")
        shard_format = require_field(shard, "format", "v2 shard")
        if shard_format != "vad-rule-shard/v2":
            raise EngineError(
                E_VERSION_UNSUPPORTED,
                "unsupported VAD shard format",
                {"format": shard_format, "path": str(shard_path)},
            )
        loaded_shard_index = parse_int(
            require_field(shard, "shard_index", "v2 shard"), "shard_index", minimum=0
        )
        if loaded_shard_index != shard_index:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "manifest and shard indices disagree",
                {"manifest": shard_index, "shard": loaded_shard_index},
            )
        for rule_index, raw_rule in enumerate(require_list(require_field(shard, "rules", "v2 shard"), "rules")):
            rule_data = require_mapping(raw_rule, f"shard[{shard_index}].rules[{rule_index}]")
            rule_id = str(require_field(rule_data, "rule_id", "v2 shard rule"))
            if rule_id in shard_rules:
                raise EngineError(
                    E_FORMAT_CORRUPT,
                    "duplicate rule_id across v2 shards",
                    {"rule_id": rule_id},
                )
            shard_rules[rule_id] = rule_data

    database_path = manifest_path.parent / database_name
    if not database_path.is_file():
        raise EngineError(
            E_FORMAT_CORRUPT,
            "v2 metadata database is missing",
            {"path": str(database_path)},
        )
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        try:
            schema_rows = connection.execute("SELECT key, value FROM schema_meta").fetchall()
            metadata_rows = connection.execute(
                """
                SELECT rule_id, shard_index, speaker_id, effective_ms, end_ms,
                       rule_generation, scope, tombstone
                FROM rule_metadata
                ORDER BY shard_index, effective_ms, rule_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "v2 metadata database schema is invalid",
                {"path": str(database_path), "error": str(exc)},
            ) from exc
    finally:
        connection.close()

    schema = {str(row["key"]): str(row["value"]) for row in schema_rows}
    schema_format = schema.get("format")
    if schema_format != "vad-rule-metadata/v2":
        if schema_format is None:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "v2 metadata format marker is missing",
                {"path": str(database_path)},
            )
        raise EngineError(
            E_VERSION_UNSUPPORTED,
            "unsupported v2 metadata format",
            {"format": schema_format},
        )

    rules: list[Rule] = []
    for row in metadata_rows:
        rule_id = str(row["rule_id"])
        shard_data = shard_rules.get(rule_id)
        if shard_data is None:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "metadata rule has no matching shard rule",
                {"rule_id": rule_id},
            )
        metadata_shard_index = parse_int(row["shard_index"], "metadata.shard_index", minimum=0)
        shard_value = parse_int(
            require_field(shard_data, "shard_index", f"shard rule {rule_id}"),
            f"{rule_id}.shard_index",
            minimum=0,
        )
        if metadata_shard_index != shard_value:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "metadata and shard index disagree",
                {"rule_id": rule_id},
            )
        speaker_id = str(row["speaker_id"])
        shard_speaker = str(require_field(shard_data, "speaker_id", f"shard rule {rule_id}"))
        if speaker_id != shard_speaker:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "metadata and shard speaker disagree",
                {"rule_id": rule_id, "metadata": speaker_id, "shard": shard_speaker},
            )
        effective_ms = parse_int(row["effective_ms"], "metadata.effective_ms", minimum=0)
        shard_effective = parse_int(
            require_field(shard_data, "effective_ms", f"shard rule {rule_id}"),
            f"{rule_id}.effective_ms",
            minimum=0,
        )
        if effective_ms != shard_effective:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "metadata and shard effective time disagree",
                {"rule_id": rule_id},
            )
        raw_end = row["end_ms"]
        raw_shard_end = require_field(shard_data, "end_ms", f"shard rule {rule_id}")
        end_ms = (
            None
            if raw_end is None
            else parse_int(raw_end, "metadata.end_ms", minimum=0)
        )
        shard_end = (
            None
            if raw_shard_end is None
            else parse_int(
                raw_shard_end,
                f"{rule_id}.end_ms",
                minimum=0,
            )
        )
        if end_ms != shard_end:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "metadata and shard interval end disagree",
                {"rule_id": rule_id},
            )
        generation = parse_int(
            row["rule_generation"], "metadata.rule_generation", minimum=1
        )
        shard_generation = parse_int(
            require_field(shard_data, "rule_generation", f"shard rule {rule_id}"),
            f"{rule_id}.rule_generation",
            minimum=1,
        )
        if generation != shard_generation:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "metadata and shard generation disagree",
                {"rule_id": rule_id},
            )
        scope = str(row["scope"])
        shard_scope = str(require_field(shard_data, "scope", f"shard rule {rule_id}"))
        if scope != shard_scope:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "metadata and shard scope disagree",
                {"rule_id": rule_id},
            )
        tombstone = bool(parse_int(row["tombstone"], "metadata.tombstone", minimum=0))
        shard_tombstone = bool(
            require_field(shard_data, "tombstone", f"shard rule {rule_id}")
        )
        if tombstone != shard_tombstone:
            raise EngineError(
                E_FORMAT_CORRUPT,
                "metadata and shard tombstone flag disagree",
                {"rule_id": rule_id},
            )
        threshold = parse_float(
            require_field(shard_data, "energy_threshold", f"shard rule {rule_id}"),
            f"{rule_id}.energy_threshold",
            minimum=0.0,
        )
        min_silence_ms = parse_int(
            require_field(shard_data, "min_silence_ms", f"shard rule {rule_id}"),
            f"{rule_id}.min_silence_ms",
            minimum=0,
        )
        rules.append(
            Rule(
                rule_id=rule_id,
                speaker_id=speaker_id,
                effective_ms=effective_ms,
                end_ms=end_ms,
                energy_threshold=threshold,
                min_silence_ms=min_silence_ms,
                rule_generation=generation,
                scope=scope,
                tombstone=tombstone,
                shard_index=metadata_shard_index,
            )
        )

    missing = sorted(set(shard_rules) - {rule.rule_id for rule in rules})
    if missing:
        raise EngineError(
            E_FORMAT_CORRUPT,
            "shard rule has no metadata row",
            {"rule_ids": missing[:10]},
        )
    if not rules:
        raise EngineError(E_FORMAT_CORRUPT, "v2 metadata contains no rules")
    _check_overlaps(rules)
    return RuleSet(
        format_version="v2",
        session_id=session_id,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        hop_ms=hop_ms,
        noise_gate=noise_gate,
        rules=rules,
        source_path=str(manifest_path.parent),
        source_kind="v2-shards-sqlite",
        extra={
            "manifest_path": str(manifest_path),
            "metadata_db": str(database_path),
            "speaker_labels": manifest.get("speaker_labels", []),
        },
    )


def read_rules(path: str | Path) -> RuleSet:
    candidate = Path(path)
    if candidate.is_dir():
        return _load_v2_manifest(candidate / "manifest.json")
    if not candidate.is_file():
        raise EngineError(
            E_FORMAT_CORRUPT,
            "rule path does not exist",
            {"path": str(candidate)},
        )
    payload = load_json_file(candidate, context="VAD rule file")
    mapping = require_mapping(payload, "VAD rule file")
    if "format" in mapping:
        format_name = mapping["format"]
        if format_name == "vad-rules/v2":
            return _load_v2_manifest(candidate)
        raise EngineError(
            E_VERSION_UNSUPPORTED,
            "unsupported VAD rule format header",
            {"format": format_name, "path": str(candidate)},
        )
    if {"session_id", "sample_rate", "rules"}.issubset(mapping.keys()):
        return parse_v1_payload(mapping, str(candidate))
    raise EngineError(
        E_VERSION_UNSUPPORTED,
        "rule field set does not identify a supported generation",
        {"path": str(candidate), "fields": sorted(mapping)},
    )


def _write_v2_rule_set(
    rules: list[Rule],
    *,
    session_id: str,
    sample_rate: int,
    frame_ms: int,
    hop_ms: int,
    noise_gate: float,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_dir = output_dir / "shards"
    if shard_dir.exists():
        shutil.rmtree(shard_dir)
    shard_dir.mkdir(parents=True, exist_ok=True)
    rules = sorted(rules, key=_rule_sort_key)
    shard_capacity = 64
    shard_refs: list[dict[str, Any]] = []
    for shard_index, offset in enumerate(range(0, len(rules), shard_capacity)):
        shard_rules = rules[offset : offset + shard_capacity]
        for rule in shard_rules:
            rule.shard_index = shard_index
        filename = f"shard_{shard_index:03d}.json"
        atomic_write_json(
            shard_dir / filename,
            {
                "format": "vad-rule-shard/v2",
                "shard_index": shard_index,
                "rules": [rule.to_dict() for rule in shard_rules],
            },
        )
        shard_refs.append(
            {
                "shard_index": shard_index,
                "path": f"shards/{filename}",
                "rule_count": len(shard_rules),
            }
        )

    database_path = output_dir / "metadata.sqlite3"
    if database_path.exists():
        database_path.unlink()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE rule_metadata (
                rule_id TEXT PRIMARY KEY,
                shard_index INTEGER NOT NULL,
                speaker_id TEXT NOT NULL,
                effective_ms INTEGER NOT NULL,
                end_ms INTEGER,
                rule_generation INTEGER NOT NULL,
                scope TEXT NOT NULL,
                tombstone INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("format", "vad-rule-metadata/v2"),
                ("session_id", session_id),
                ("rule_generation", str(max(rule.rule_generation for rule in rules))),
            ),
        )
        connection.executemany(
            """
            INSERT INTO rule_metadata(
                rule_id, shard_index, speaker_id, effective_ms, end_ms,
                rule_generation, scope, tombstone
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    rule.rule_id,
                    rule.shard_index,
                    rule.speaker_id,
                    rule.effective_ms,
                    rule.end_ms,
                    rule.rule_generation,
                    rule.scope,
                    1 if rule.tombstone else 0,
                )
                for rule in rules
            ],
        )
        connection.commit()
    finally:
        connection.close()

    manifest = {
        "format": "vad-rules/v2",
        "version": 2,
        "session_id": session_id,
        "sample_rate": sample_rate,
        "frame_ms": frame_ms,
        "hop_ms": hop_ms,
        "noise_gate": quantize(noise_gate),
        "metadata_db": "metadata.sqlite3",
        "rule_generation": max(rule.rule_generation for rule in rules),
        "shards": shard_refs,
        "speaker_labels": sorted({rule.speaker_id for rule in rules}),
    }
    atomic_write_json(output_dir / "manifest.json", manifest)
    return output_dir


def _semantic_points(source: RuleSet, speakers: Iterable[str]) -> list[int]:
    points: set[int] = set()
    for rule in source.rules:
        points.add(rule.effective_ms)
        if rule.end_ms is not None:
            points.add(max(0, rule.end_ms - 1))
            points.add(rule.end_ms)
    ordered = sorted(points)
    midpoints = [
        (left + right) // 2 for left, right in zip(ordered, ordered[1:]) if right > left + 1
    ]
    return sorted(set(ordered + midpoints))


def assert_semantics_equal(
    source: RuleSet,
    migrated: RuleSet,
    *,
    speakers: Iterable[str] | None = None,
    retained_rule_ids: set[str] | None = None,
) -> int:
    speakers_to_check = sorted(set(speakers or source.speakers()))
    checks = 0
    for time_ms in _semantic_points(source, speakers_to_check):
        for speaker_id in speakers_to_check:
            source_rule = source.active_rule(speaker_id, time_ms)
            migrated_rule = migrated.active_rule(speaker_id, time_ms)
            if source_rule is not None and retained_rule_ids is not None:
                if source_rule.rule_id not in retained_rule_ids:
                    continue
            if (
                source_rule is None
                and retained_rule_ids is not None
                and source.active_tombstone(speaker_id, time_ms) is not None
            ):
                continue
            source_signature = (
                None
                if source_rule is None
                else (
                    quantize(source_rule.energy_threshold),
                    source_rule.min_silence_ms,
                    source_rule.speaker_id,
                )
            )
            migrated_signature = (
                None
                if migrated_rule is None
                else (
                    quantize(migrated_rule.energy_threshold),
                    migrated_rule.min_silence_ms,
                    migrated_rule.speaker_id,
                )
            )
            if source_signature != migrated_signature:
                raise EngineError(
                    E_STATE_INCONSISTENT,
                    "migration changed rule semantics",
                    {
                        "direction": f"{source.format_version}->{migrated.format_version}",
                        "speaker_id": speaker_id,
                        "time_ms": time_ms,
                        "source": source_signature,
                        "migrated": migrated_signature,
                    },
                )
            checks += 1
    return checks


def migrate_v1_to_v2(source: RuleSet, output_dir: str | Path) -> MigrationResult:
    if source.format_version != "v1":
        raise EngineError(
            E_VERSION_UNSUPPORTED,
            "v1->v2 migration requires a v1 source",
            {"format": source.format_version},
        )
    migrated_rules: list[Rule] = []
    provenance: list[dict[str, Any]] = []
    for index, rule in enumerate(sorted(source.rules, key=_rule_sort_key), start=1):
        rule_id = f"{source.session_id}:migrated:g1:{index:04d}"
        migrated = Rule(
            rule_id=rule_id,
            speaker_id=rule.speaker_id,
            effective_ms=rule.effective_ms,
            end_ms=rule.end_ms,
            energy_threshold=rule.energy_threshold,
            min_silence_ms=rule.min_silence_ms,
            rule_generation=1,
            scope="speaker",
            tombstone=False,
            shard_index=0,
        )
        migrated_rules.append(migrated)
        provenance.extend(
            (
                {
                    "rule_id": rule_id,
                    "field": "rule_id",
                    "value": rule_id,
                    "source": "generated deterministic v2 rule identifier",
                },
                {
                    "rule_id": rule_id,
                    "field": "effective_ms",
                    "value": rule.effective_ms,
                    "source": "v1.start_sec * 1000, rounded half-up",
                },
                {
                    "rule_id": rule_id,
                    "field": "end_ms",
                    "value": rule.end_ms,
                    "source": (
                        "v1.end_sec * 1000 (or implicit next same-speaker start)"
                        if rule.end_ms is not None
                        else "open interval"
                    ),
                },
                {
                    "rule_id": rule_id,
                    "field": "rule_generation",
                    "value": 1,
                    "source": "generated default for v1 rule",
                },
                {
                    "rule_id": rule_id,
                    "field": "scope",
                    "value": "speaker",
                    "source": "generated from flat v1 per-speaker rule",
                },
                {
                    "rule_id": rule_id,
                    "field": "tombstone",
                    "value": False,
                    "source": "generated default; v1 has no tombstone",
                },
                {
                    "rule_id": rule_id,
                    "field": "shard_index",
                    "value": None,
                    "source": "generated shard partition index",
                },
            )
        )
    destination = Path(output_dir)
    _write_v2_rule_set(
        migrated_rules,
        session_id=source.session_id,
        sample_rate=source.sample_rate,
        frame_ms=source.frame_ms,
        hop_ms=source.hop_ms,
        noise_gate=source.noise_gate,
        output_dir=destination,
    )
    migrated = read_rules(destination)
    shard_by_rule_id = {rule.rule_id: rule.shard_index for rule in migrated.rules}
    for item in provenance:
        if item["field"] == "shard_index":
            item["value"] = shard_by_rule_id.get(item["rule_id"])
    checks = assert_semantics_equal(source, migrated)
    return MigrationResult(
        source=source,
        migrated=migrated,
        output_path=str(destination),
        direction="v1->v2",
        business_code="M_V1_TO_V2_OK",
        provenance=provenance,
        semantic_checks=checks,
    )


def migrate_v2_to_v1(source: RuleSet, output_path: str | Path) -> MigrationResult:
    if source.format_version != "v2":
        raise EngineError(
            E_VERSION_UNSUPPORTED,
            "v2->v1 migration requires a v2 source",
            {"format": source.format_version},
        )
    retained: list[Rule] = []
    discarded: list[dict[str, Any]] = []
    retained_ids: set[str] = set()
    for rule in sorted(source.rules, key=_rule_sort_key):
        reason: str | None = None
        code: str | None = None
        if rule.tombstone:
            reason = "scope tombstone has no v1 representation"
            code = M_DROP_TOMBSTONE
        elif rule.rule_generation != 1:
            reason = "v1 cannot represent rule generation != 1"
            code = M_DROP_GENERATION
        elif rule.scope not in ("speaker", f"speaker:{rule.speaker_id}"):
            reason = "v1 cannot represent non-speaker scope"
            code = M_DROP_SCOPE
        if reason is not None:
            discarded.append(
                {
                    "rule_id": rule.rule_id,
                    "speaker_id": rule.speaker_id,
                    "effective_ms": rule.effective_ms,
                    "end_ms": rule.end_ms,
                    "rule_generation": rule.rule_generation,
                    "scope": rule.scope,
                    "tombstone": rule.tombstone,
                    "business_code": code,
                    "reason": reason,
                }
            )
            continue
        retained.append(rule)
        retained_ids.add(rule.rule_id)

    if not retained:
        raise EngineError(
            E_FORMAT_CORRUPT,
            "v2 source has no generation-1 speaker rules representable in v1",
        )

    output_rules: list[dict[str, Any]] = []
    for rule in retained:
        output_rules.append(
            {
                "speaker_id": rule.speaker_id,
                "start_sec": quantize(rule.effective_ms / 1000.0, 6),
                "end_sec": (
                    quantize(rule.end_ms / 1000.0, 6) if rule.end_ms is not None else None
                ),
                "energy_threshold": quantize(rule.energy_threshold),
                "min_silence_ms": rule.min_silence_ms,
                "source_rule_id": rule.rule_id,
            }
        )
    payload = {
        "session_id": source.session_id,
        "sample_rate": source.sample_rate,
        "frame_ms": source.frame_ms,
        "hop_ms": source.hop_ms,
        "noise_gate": quantize(source.noise_gate),
        "speakers": sorted({rule.speaker_id for rule in retained}),
        "rules": output_rules,
    }
    destination = Path(output_path)
    atomic_write_json(destination, payload)
    migrated = parse_v1_payload(payload, str(destination))
    checks = assert_semantics_equal(
        source,
        migrated,
        retained_rule_ids=retained_ids,
    )
    dropped_counts: dict[str, int] = {}
    for item in discarded:
        dropped_counts[item["business_code"]] = dropped_counts.get(item["business_code"], 0) + 1
    return MigrationResult(
        source=source,
        migrated=migrated,
        output_path=str(destination),
        direction="v2->v1",
        business_code="M_V2_TO_V1_OK",
        discarded=discarded,
        dropped_counts=dropped_counts,
        semantic_checks=checks,
    )


def rule_curve_rows(rule_set: RuleSet, speakers: Iterable[str], duration_ms: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for time_ms in range(0, duration_ms + 1):
        row: dict[str, Any] = {"time_ms": time_ms}
        for speaker_id in sorted(speakers):
            threshold, min_silence = rule_set.curve_at(speaker_id, time_ms)
            row[speaker_id] = {
                "threshold": quantize(threshold),
                "min_silence_ms": min_silence,
            }
        rows.append(row)
    return rows

