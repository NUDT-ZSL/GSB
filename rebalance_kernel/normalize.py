"""Normalize the three consumer-group schemas into one canonical model."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from .model import MemberSpec, NormalizedProject, Provenance, TopicSpec, canonical_json


SUPPORTED_SCHEMAS = ("v1", "v2", "v3")


class NormalizationError(ValueError):
    pass


class _Builder:
    def __init__(self, source_schema: str) -> None:
        self.source_schema = source_schema
        self.provenance: list[Provenance] = []
        self._field_counts: dict[str, int] = {}

    def add(
        self,
        field: str,
        source_path: str,
        raw_value: Any,
        note: str = "",
    ) -> None:
        self.provenance.append(
            Provenance(
                field=field,
                source_path=source_path,
                source_schema=self.source_schema,
                raw_value=deepcopy(raw_value),
                note=note,
            )
        )

    def finalize(self, group_id: str, members: dict[str, MemberSpec],
                 topics: dict[str, TopicSpec]) -> NormalizedProject:
        self._validate(group_id, members, topics)
        return NormalizedProject(
            group_id=group_id,
            members=members,
            topics=topics,
            provenance=self.provenance,
            source_schema=self.source_schema,
        )

    @staticmethod
    def _validate(group_id: str, members: dict[str, MemberSpec],
                  topics: dict[str, TopicSpec]) -> None:
        if not group_id:
            raise NormalizationError("consumer group id must not be empty")
        if not members:
            raise NormalizationError("consumer group must declare members")
        if not topics:
            raise NormalizationError("consumer group must declare topics")
        for topic, spec in topics.items():
            if not spec.partitions:
                raise NormalizationError(f"topic {topic!r} has no partitions")
            if tuple(sorted(spec.partitions)) != spec.partitions:
                raise NormalizationError(f"topic {topic!r} partitions are not sorted")
            if len(set(spec.partitions)) != len(spec.partitions):
                raise NormalizationError(f"topic {topic!r} has duplicate partitions")
            if any(partition < 0 for partition in spec.partitions):
                raise NormalizationError(
                    f"topic {topic!r} has a negative normalized partition"
                )
        for member_id, member in members.items():
            if member.processing_rate < 0:
                raise NormalizationError(
                    f"member {member_id!r} has a negative processing rate"
                )
            if member.session_timeout_ms <= 0:
                raise NormalizationError(
                    f"member {member_id!r} has a non-positive session timeout"
                )
            unknown = set(member.subscriptions) - set(topics)
            if unknown:
                raise NormalizationError(
                    f"member {member_id!r} subscribes to unknown topics: "
                    f"{sorted(unknown)}"
                )


def _load_v1(raw: dict[str, Any]) -> NormalizedProject:
    builder = _Builder("v1")
    group_id = str(raw["consumer_group"])
    builder.add("group_id", "$.consumer_group", raw["consumer_group"])

    members: dict[str, MemberSpec] = {}
    for index, item in enumerate(raw["members"]):
        path = f"$.members[{index}]"
        member_id = str(item["member_id"])
        rate = float(item["processing_rate"])
        timeout_ms = int(item["session_timeout_ms"])
        subscriptions = tuple(sorted(str(topic) for topic in item["subscriptions"]))
        members[member_id] = MemberSpec(
            member_id=member_id,
            processing_rate=rate,
            session_timeout_ms=timeout_ms,
            subscriptions=subscriptions,
        )
        builder.add(
            f"members.{member_id}.member_id", f"{path}.member_id", item["member_id"]
        )
        builder.add(
            f"members.{member_id}.processing_rate",
            f"{path}.processing_rate",
            item["processing_rate"],
        )
        builder.add(
            f"members.{member_id}.session_timeout_ms",
            f"{path}.session_timeout_ms",
            item["session_timeout_ms"],
        )
        builder.add(
            f"members.{member_id}.subscriptions",
            f"{path}.subscriptions",
            item["subscriptions"],
        )

    topics: dict[str, list[int]] = {}
    seen_topics: set[str] = set()
    for index, item in enumerate(raw["partitions"]):
        path = f"$.partitions[{index}]"
        topic = str(item["topic"])
        if topic not in seen_topics:
            builder.add(f"topics.{topic}.name", f"{path}.topic", item["topic"])
            seen_topics.add(topic)
        raw_partition = int(item["partition"])
        normalized_partition = raw_partition
        topics.setdefault(topic, []).append(normalized_partition)
        builder.add(
            f"topics.{topic}.partitions",
            f"{path}.partition",
            raw_partition,
            "partition already 0-based",
        )
    topic_specs = {
        topic: TopicSpec(topic, tuple(sorted(partitions)))
        for topic, partitions in topics.items()
    }
    return builder.finalize(group_id, members, topic_specs)


def _load_v2(raw: dict[str, Any]) -> NormalizedProject:
    builder = _Builder("v2")
    group_id = str(raw["group"]["id"])
    builder.add("group_id", "$.group.id", raw["group"]["id"])

    members: dict[str, MemberSpec] = {}
    for index, item in enumerate(raw["members"]):
        path = f"$.members[{index}]"
        member_id = str(item["id"])
        rate = float(item["rate_per_second"])
        timeout_seconds = float(item["lease"]["session_timeout_sec"])
        timeout_ms = int(round(timeout_seconds * 1000))
        subscriptions = tuple(sorted(str(topic) for topic in item["topics"]))
        members[member_id] = MemberSpec(
            member_id=member_id,
            processing_rate=rate,
            session_timeout_ms=timeout_ms,
            subscriptions=subscriptions,
        )
        builder.add(f"members.{member_id}.member_id", f"{path}.id", item["id"])
        builder.add(
            f"members.{member_id}.processing_rate",
            f"{path}.rate_per_second",
            item["rate_per_second"],
        )
        builder.add(
            f"members.{member_id}.session_timeout_ms",
            f"{path}.lease.session_timeout_sec",
            timeout_seconds,
            "seconds_to_milliseconds",
        )
        builder.add(
            f"members.{member_id}.subscriptions", f"{path}.topics", item["topics"]
        )

    topics: dict[str, list[int]] = {}
    for topic, items in raw["partition_tree"].items():
        builder.add(
            f"topics.{topic}.name",
            f"$.partition_tree.{topic}",
            topic,
            "object_key_to_topic_name",
        )
        for index, item in enumerate(items):
            path = f"$.partition_tree.{topic}[{index}]"
            raw_partition = int(item["number"])
            normalized_partition = raw_partition - 1
            topics.setdefault(topic, []).append(normalized_partition)
            builder.add(
                f"topics.{topic}.partitions",
                f"{path}.number",
                raw_partition,
                "partition_base_1_to_0",
            )
    topic_specs = {
        topic: TopicSpec(topic, tuple(sorted(partitions)))
        for topic, partitions in topics.items()
    }
    return builder.finalize(group_id, members, topic_specs)


def _load_v3(raw: dict[str, Any]) -> NormalizedProject:
    builder = _Builder("v3")
    group_id = str(raw["group_id"])
    builder.add("group_id", "$.group_id", raw["group_id"])

    members: dict[str, MemberSpec] = {}
    for index, item in enumerate(raw["consumer_specs"]):
        path = f"$.consumer_specs[{index}]"
        member_id = str(item["consumer_id"])
        rate = float(item["handling_rate"])
        timeout_value = float(item["heartbeat"]["session_timeout"]["value"])
        timeout_unit = str(item["heartbeat"]["session_timeout"]["unit"])
        if timeout_unit in {"ms", "millisecond", "milliseconds"}:
            timeout_ms = int(round(timeout_value))
            note = "milliseconds_unchanged"
        elif timeout_unit in {"s", "sec", "second", "seconds"}:
            timeout_ms = int(round(timeout_value * 1000))
            note = "seconds_to_milliseconds"
        else:
            raise NormalizationError(
                f"unsupported v3 timeout unit {timeout_unit!r}"
            )
        subscriptions = tuple(sorted(str(topic) for topic in item["subscribe"]))
        members[member_id] = MemberSpec(
            member_id=member_id,
            processing_rate=rate,
            session_timeout_ms=timeout_ms,
            subscriptions=subscriptions,
        )
        builder.add(
            f"members.{member_id}.member_id", f"{path}.consumer_id", item["consumer_id"]
        )
        builder.add(
            f"members.{member_id}.processing_rate",
            f"{path}.handling_rate",
            item["handling_rate"],
        )
        builder.add(
            f"members.{member_id}.session_timeout_ms",
            f"{path}.heartbeat.session_timeout.value",
            timeout_value,
            note,
        )
        builder.add(
            f"members.{member_id}.subscriptions",
            f"{path}.subscribe",
            item["subscribe"],
        )

    topics: dict[str, list[int]] = {}
    for topic, partitions in raw["topic_partitions"].items():
        builder.add(
            f"topics.{topic}.name",
            f"$.topic_partitions.{topic}",
            topic,
            "object_key_to_topic_name",
        )
        for index, value in enumerate(partitions):
            path = f"$.topic_partitions.{topic}[{index}]"
            raw_partition = int(value)
            normalized_partition = raw_partition
            topics.setdefault(topic, []).append(normalized_partition)
            builder.add(
                f"topics.{topic}.partitions",
                path,
                raw_partition,
                "partition already 0-based",
            )
    topic_specs = {
        topic: TopicSpec(topic, tuple(sorted(partitions)))
        for topic, partitions in topics.items()
    }
    return builder.finalize(group_id, members, topic_specs)


_LOADERS = {"v1": _load_v1, "v2": _load_v2, "v3": _load_v3}


def normalize_project(raw: dict[str, Any]) -> NormalizedProject:
    schema = str(
        raw.get("schema", raw.get("schema_version", ""))
    ).lower()
    if schema not in _LOADERS:
        raise NormalizationError(
            f"unsupported schema {schema!r}; expected one of {SUPPORTED_SCHEMAS}"
        )
    try:
        return _LOADERS[schema](raw)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, NormalizationError):
            raise
        raise NormalizationError(
            f"failed to normalize {schema} project: {exc}"
        ) from exc


def load_project_file(path: str | Path) -> NormalizedProject:
    with Path(path).open("r", encoding="utf-8") as handle:
        return normalize_project(json.load(handle))


def normalization_report(projects: dict[str, NormalizedProject]) -> dict[str, Any]:
    """Compare canonical models and expose raw-field provenance differences."""
    ordered_versions = sorted(projects)
    content = {
        version: projects[version].content_dict() for version in ordered_versions
    }
    hashes = {
        version: canonical_json(content[version])
        for version in ordered_versions
    }
    canonical_hashes = {
        version: projects[version].content_sha256()
        for version in ordered_versions
    }
    reference_version = ordered_versions[0]
    reference = content[reference_version]
    canonical_differences: list[dict[str, Any]] = []
    for version in ordered_versions[1:]:
        if hashes[version] != hashes[reference_version]:
            canonical_differences.append(
                {
                    "left_schema": reference_version,
                    "right_schema": version,
                    "left_sha256": canonical_hashes[reference_version],
                    "right_sha256": canonical_hashes[version],
                    "left": reference,
                    "right": content[version],
                }
            )

    fields: dict[str, dict[str, Any]] = {}
    for version in ordered_versions:
        for entry in projects[version].provenance:
            record = fields.setdefault(entry.field, {})
            record.setdefault(version, []).append(entry.as_dict())

    field_mappings = []
    for field in sorted(fields):
        by_version = fields[field]
        raw_values = {
            version: sorted(
                {canonical_json(item["raw_value"]) for item in by_version[version]}
            )
            for version in ordered_versions
        }
        field_mappings.append(
            {
                "normalized_field": field,
                "by_version": by_version,
                "raw_values": raw_values,
                "raw_values_differ": len(
                    {tuple(values) for values in raw_values.values()}
                )
                > 1,
            }
        )

    variants = []
    for version in ordered_versions:
        project = projects[version]
        variants.append(
            {
                "source_schema": project.source_schema,
                "comparison_key": version,
                "canonical_sha256": canonical_hashes[version],
                "normalized": content[version],
                "provenance": project.as_dict()["provenance"],
            }
        )

    return {
        "canonical_equal": not canonical_differences,
        "reference_schema": projects[reference_version].source_schema,
        "canonical_sha256": canonical_hashes,
        "canonical_differences": canonical_differences,
        "raw_difference_count": sum(
            1 for item in field_mappings if item["raw_values_differ"]
        ),
        "field_mappings": field_mappings,
        "variants": variants,
        "verdict": (
            "所有 schema 版本归一后模型一致"
            if not canonical_differences
            else "归一模型存在差异"
        ),
    }
