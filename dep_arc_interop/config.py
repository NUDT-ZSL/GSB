from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode, fail
from .models import ArcGraph
from .source import SourceLike, read_source


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_payload(payload: Any) -> str:
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def section_checksum(section: dict[str, Any]) -> str:
    payload = {key: value for key, value in section.items() if key != "checksum"}
    return sha256_payload(payload)


@dataclass(frozen=True)
class LabelSetSnapshot:
    name: str
    version: int
    checksum: str
    values: tuple[str, ...]

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(self.values)


@dataclass(frozen=True)
class MigrationPolicy:
    version: int
    checksum: str
    confidence_low: float
    confidence_high: float
    confidence_tolerance: float
    forward_check: bool
    reverse_check: bool
    discard_rules: dict[str, tuple[str, ...]]
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def discard_fields_for(self, target_version: str) -> tuple[str, ...]:
        return tuple(self.discard_rules.get(f"downgrade_to_{target_version}", ()))


@dataclass(frozen=True)
class ControlledSnapshot:
    pos_tags: LabelSetSnapshot
    deprels: LabelSetSnapshot
    policy: MigrationPolicy
    source_name: str
    file_checksum: str

    @property
    def pos_labels(self) -> frozenset[str]:
        return self.pos_tags.labels

    @property
    def deprel_labels(self) -> frozenset[str]:
        return self.deprels.labels


@dataclass(frozen=True)
class SnapshotTrace:
    sequence: int
    source_name: str
    pos_version: int
    pos_checksum: str
    deprel_version: int
    deprel_checksum: str
    policy_version: int
    policy_checksum: str
    file_checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "source_name": self.source_name,
            "pos_tags": {
                "version": self.pos_version,
                "checksum": self.pos_checksum,
            },
            "deprels": {
                "version": self.deprel_version,
                "checksum": self.deprel_checksum,
            },
            "migration_policy": {
                "version": self.policy_version,
                "checksum": self.policy_checksum,
            },
            "file_checksum": self.file_checksum,
        }


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(
                ErrorCode.SNAPSHOT_CORRUPT,
                "snapshot contains a duplicate JSON key",
                field=key,
                category="DUPLICATE_JSON_KEY",
            )
        result[key] = value
    return result


def _load_json(source: SourceLike) -> tuple[dict[str, Any], str]:
    source_text = read_source(source)
    try:
        data = json.loads(
            source_text.text.lstrip("\ufeff"),
            object_pairs_hook=_no_duplicate_keys,
        )
    except BusinessError:
        raise
    except json.JSONDecodeError as exc:
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "snapshot is not valid JSON",
            field="root",
            line=exc.lineno,
            column=exc.colno,
            category="INVALID_JSON",
        )
    if not isinstance(data, dict):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "snapshot root must be a JSON object",
            field="root",
            category="INVALID_ROOT",
        )
    return data, source_text.name


def _read_section(
    root: dict[str, Any],
    name: str,
) -> tuple[int, str, list[str], dict[str, Any]]:
    section = root.get(name)
    if not isinstance(section, dict):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot section {name!r} is missing or is not an object",
            field=name,
            category="MISSING_SECTION",
        )
    version = section.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot section {name!r} has an invalid monotonic version",
            field=f"{name}.version",
            category="INVALID_VERSION",
        )
    provided = section.get("checksum")
    expected = section_checksum(section)
    if not isinstance(provided, str) or provided != expected:
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot section {name!r} checksum does not match its content",
            field=f"{name}.checksum",
            expected=expected,
            actual=provided,
            category="CHECKSUM_MISMATCH",
        )
    values = section.get("values")
    if not isinstance(values, list) or not values:
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot section {name!r} must contain a non-empty values list",
            field=f"{name}.values",
            category="INVALID_VALUES",
        )
    if any(not isinstance(value, str) or not value for value in values):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot section {name!r} contains a non-string or empty label",
            field=f"{name}.values",
            category="INVALID_LABEL",
        )
    if len(values) != len(set(values)):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot section {name!r} contains duplicate labels",
            field=f"{name}.values",
            category="DUPLICATE_LABEL",
        )
    return version, provided, list(values), section


def _read_policy(root: dict[str, Any]) -> MigrationPolicy:
    name = "migration_policy"
    section = root.get(name)
    if not isinstance(section, dict):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "snapshot migration_policy section is missing",
            field=name,
            category="MISSING_SECTION",
        )
    version = section.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "migration_policy has an invalid monotonic version",
            field=f"{name}.version",
            category="INVALID_VERSION",
        )
    provided = section.get("checksum")
    expected = section_checksum(section)
    if not isinstance(provided, str) or provided != expected:
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "migration_policy checksum does not match its content",
            field=f"{name}.checksum",
            expected=expected,
            actual=provided,
            category="CHECKSUM_MISMATCH",
        )
    defaults = section.get("defaults")
    if not isinstance(defaults, dict):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "migration_policy.defaults must be an object",
            field=f"{name}.defaults",
            category="INVALID_DEFAULTS",
        )
    low = defaults.get("confidence_low")
    high = defaults.get("confidence_high")
    tolerance = defaults.get("confidence_tolerance")
    for field_name, value in (
        ("confidence_low", low),
        ("confidence_high", high),
        ("confidence_tolerance", tolerance),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            fail(
                ErrorCode.SNAPSHOT_CORRUPT,
                f"migration_policy.defaults.{field_name} must be numeric",
                field=f"{name}.defaults.{field_name}",
                category="INVALID_DEFAULT",
            )
    if not (0.0 <= float(low) <= float(high) <= 1.0):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "migration_policy confidence defaults must satisfy 0 <= low <= high <= 1",
            field=f"{name}.defaults",
            category="INVALID_CONFIDENCE_RANGE",
        )
    if float(tolerance) <= 0:
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "migration_policy confidence_tolerance must be positive",
            field=f"{name}.defaults.confidence_tolerance",
            category="INVALID_TOLERANCE",
        )
    forward_check = defaults.get("forward_check")
    reverse_check = defaults.get("reverse_check")
    if not isinstance(forward_check, bool) or not isinstance(reverse_check, bool):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "migration_policy dependency check defaults must be boolean",
            field=f"{name}.defaults.forward_check",
            category="INVALID_DEFAULT",
        )
    discard_rules_raw = section.get("discard_rules")
    if not isinstance(discard_rules_raw, dict):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "migration_policy.discard_rules must be an object",
            field=f"{name}.discard_rules",
            category="INVALID_DISCARD_RULES",
        )
    discard_rules: dict[str, tuple[str, ...]] = {}
    for key, value in discard_rules_raw.items():
        if not isinstance(key, str) or not isinstance(value, list):
            fail(
                ErrorCode.SNAPSHOT_CORRUPT,
                "migration_policy.discard_rules entries must be string-to-list",
                field=f"{name}.discard_rules.{key}",
                category="INVALID_DISCARD_RULES",
            )
        if any(not isinstance(item, str) for item in value):
            fail(
                ErrorCode.SNAPSHOT_CORRUPT,
                "migration_policy discard fields must be strings",
                field=f"{name}.discard_rules.{key}",
                category="INVALID_DISCARD_RULES",
            )
        discard_rules[key] = tuple(value)
    return MigrationPolicy(
        version=version,
        checksum=provided,
        confidence_low=float(low),
        confidence_high=float(high),
        confidence_tolerance=float(tolerance),
        forward_check=forward_check,
        reverse_check=reverse_check,
        discard_rules=discard_rules,
        raw=section,
    )


def parse_snapshot(source: SourceLike) -> ControlledSnapshot:
    root, source_name = _load_json(source)
    if root.get("schema") not in (None, "deparc-controlled-snapshot-v1"):
        fail(
            ErrorCode.SNAPSHOT_CORRUPT,
            "unsupported controlled snapshot schema",
            field="schema",
            category="UNSUPPORTED_SCHEMA",
        )
    pos_version, pos_checksum, pos_values, _ = _read_section(root, "pos_tags")
    dep_version, dep_checksum, dep_values, _ = _read_section(root, "deprels")
    policy = _read_policy(root)
    file_checksum = sha256_payload(root)
    return ControlledSnapshot(
        pos_tags=LabelSetSnapshot(
            name="pos_tags",
            version=pos_version,
            checksum=pos_checksum,
            values=tuple(pos_values),
        ),
        deprels=LabelSetSnapshot(
            name="deprels",
            version=dep_version,
            checksum=dep_checksum,
            values=tuple(dep_values),
        ),
        policy=policy,
        source_name=source_name,
        file_checksum=file_checksum,
    )


def find_retired_labels(
    graphs: Iterable[ArcGraph],
    snapshot: ControlledSnapshot,
) -> list[dict[str, Any]]:
    valid = snapshot.deprel_labels
    affected: list[dict[str, Any]] = []
    for graph in graphs:
        for sent_id, arc in graph.iter_arcs():
            if arc.deprel not in valid:
                affected.append(
                    {
                        "sent_id": sent_id,
                        "token_id": arc.token_id,
                        "deprel": arc.deprel,
                        "format_version": str(graph.format_version),
                    }
                )
    return affected


class SnapshotStore:
    """Atomically replaces the complete controlled snapshot on every change."""

    def __init__(self) -> None:
        self._current: ControlledSnapshot | None = None
        self._history: list[SnapshotTrace] = []
        self._traces_by_checksum: dict[str, SnapshotTrace] = {}

    @property
    def current(self) -> ControlledSnapshot:
        if self._current is None:
            fail(
                ErrorCode.SNAPSHOT_CORRUPT,
                "no controlled snapshot has been loaded",
                field="snapshot",
                category="NOT_LOADED",
            )
        return self._current

    @property
    def history(self) -> tuple[SnapshotTrace, ...]:
        return tuple(self._history)

    def load(
        self,
        source: SourceLike,
        *,
        existing_graphs: Iterable[ArcGraph] | None = None,
    ) -> SnapshotTrace:
        incoming = parse_snapshot(source)
        if self._current is not None:
            self._validate_monotonic(incoming)
            if self._same_snapshot(incoming):
                return self._traces_by_checksum[incoming.file_checksum]
            if existing_graphs is not None:
                affected = find_retired_labels(existing_graphs, incoming)
                if affected:
                    fail(
                        ErrorCode.LABEL_RETIRED,
                        "controlled deprel snapshot retires labels used by existing arcs",
                        deprel_version=incoming.deprels.version,
                        deprel_checksum=incoming.deprels.checksum,
                        affected=affected,
                    )
        else:
            if existing_graphs is not None:
                affected = find_retired_labels(existing_graphs, incoming)
                if affected:
                    fail(
                        ErrorCode.LABEL_RETIRED,
                        "controlled deprel snapshot retires labels used by existing arcs",
                        deprel_version=incoming.deprels.version,
                        deprel_checksum=incoming.deprels.checksum,
                        affected=affected,
                    )
        self._current = incoming
        trace = SnapshotTrace(
            sequence=len(self._history) + 1,
            source_name=incoming.source_name,
            pos_version=incoming.pos_tags.version,
            pos_checksum=incoming.pos_tags.checksum,
            deprel_version=incoming.deprels.version,
            deprel_checksum=incoming.deprels.checksum,
            policy_version=incoming.policy.version,
            policy_checksum=incoming.policy.checksum,
            file_checksum=incoming.file_checksum,
        )
        self._history.append(trace)
        self._traces_by_checksum[incoming.file_checksum] = trace
        return trace

    def _validate_monotonic(self, incoming: ControlledSnapshot) -> None:
        assert self._current is not None
        comparisons = (
            ("pos_tags", self._current.pos_tags, incoming.pos_tags),
            ("deprels", self._current.deprels, incoming.deprels),
            ("migration_policy", self._current.policy, incoming.policy),
        )
        for name, old, new in comparisons:
            old_version = old.version
            new_version = new.version
            old_checksum = old.checksum
            new_checksum = new.checksum
            if new_version < old_version:
                fail(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    f"{name} version rollback is forbidden",
                    field=f"{name}.version",
                    previous=old_version,
                    incoming=new_version,
                    category="VERSION_ROLLBACK",
                )
            if new_version == old_version and new_checksum != old_checksum:
                fail(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    f"{name} content changed without a monotonic version increase",
                    field=f"{name}.checksum",
                    version=new_version,
                    previous_checksum=old_checksum,
                    incoming_checksum=new_checksum,
                    category="UNVERSIONED_REPLACEMENT",
                )

    def _same_snapshot(self, incoming: ControlledSnapshot) -> bool:
        assert self._current is not None
        return (
            incoming.pos_tags.version == self._current.pos_tags.version
            and incoming.pos_tags.checksum == self._current.pos_tags.checksum
            and incoming.deprels.version == self._current.deprels.version
            and incoming.deprels.checksum == self._current.deprels.checksum
            and incoming.policy.version == self._current.policy.version
            and incoming.policy.checksum == self._current.policy.checksum
        )


def load_snapshot_path(path: str | Path) -> ControlledSnapshot:
    return parse_snapshot(Path(path))
