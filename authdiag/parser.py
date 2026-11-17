"""Item-by-item policy and event normalization with business diagnostics."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .constants import (
    ENTRY_KINDS,
    EVENT_ID_RE,
    EVENT_TYPES,
    MARKER_ID_RE,
    POLICY_ID_RE,
    SESSION_ID_RE,
    TOKEN_ID_RE,
    UPDATE_ID_RE,
    VERSION_RE,
)
from .diagnostics import Diagnostic, sort_diagnostics
from .util import (
    canonical_json_bytes,
    iter_json_array,
    normalize_timestamp,
    sha256_bytes,
    sha256_value,
    version_tuple,
)


@dataclass(frozen=True)
class PolicyEntry:
    id: str
    kind: str
    version: str
    entry_index: int
    parents: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    subject: str | None = None
    role: str | None = None
    token_rule: str | None = None
    session_policy: str | None = None
    description: str | None = None
    source: str = ""

    def dependency_edges(self) -> list[tuple[str, str]]:
        if self.kind == "role":
            return [(target, "role_inheritance") for target in self.parents]
        if self.kind == "token_rule":
            return [(target, "token_rule_ref") for target in self.refs]
        if self.kind == "role_binding":
            result: list[tuple[str, str]] = []
            if self.role is not None:
                result.append((self.role, "binding_role_ref"))
            if self.token_rule is not None:
                result.append((self.token_rule, "binding_token_rule_ref"))
            if self.session_policy is not None:
                result.append((self.session_policy, "binding_session_policy_ref"))
            return result
        return []

    def runtime_dependencies(self) -> tuple[str, ...]:
        values = {
            item
            for item in (
                self.id if self.kind == "role_binding" else None,
                self.role,
                self.token_rule,
                self.session_policy,
                *self.parents,
                *self.refs,
            )
            if item
        }
        return tuple(sorted(values))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "version": self.version,
            "entry_index": self.entry_index,
        }
        if self.parents:
            result["parents"] = list(self.parents)
        if self.refs:
            result["refs"] = list(self.refs)
        if self.subject is not None:
            result["subject"] = self.subject
        if self.role is not None:
            result["role"] = self.role
        if self.token_rule is not None:
            result["token_rule"] = self.token_rule
        if self.session_policy is not None:
            result["session_policy"] = self.session_policy
        if self.description is not None:
            result["description"] = self.description
        return result


@dataclass(frozen=True)
class EntryDraft:
    doc: dict[str, Any]
    entry_index: int
    source: str

    @property
    def id(self) -> str:
        return str(self.doc["id"])

    @property
    def kind(self) -> str:
        return str(self.doc["kind"])

    @property
    def version(self) -> str:
        return str(self.doc["version"])


@dataclass(frozen=True)
class PolicyUpdate:
    update_id: str
    timestamp: str | None
    version: str
    op: str
    entry: EntryDraft | None
    entry_id: str
    source_index: int
    source: str
    content_sha256: str

    def sort_key(self) -> tuple[Any, ...]:
        parsed_version = version_tuple(self.version) or (0, 0, 0)
        return (
            1 if self.timestamp is None else 0,
            self.timestamp or "",
            parsed_version,
            self.update_id,
        )

    @property
    def stable_key(self) -> str:
        return json.dumps(
            [self.timestamp, self.version, self.update_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @property
    def changed_entry_ids(self) -> tuple[str, ...]:
        return (self.entry_id,)

    def checkpoint_record(self) -> dict[str, str]:
        return {"key": self.stable_key, "sha256": self.content_sha256}


@dataclass(frozen=True)
class SyncMarker:
    marker_id: str
    update_id: str
    before_version: str
    after_version: str
    changed_entry_ids: tuple[str, ...]
    expected_tokens: tuple[str, ...]
    expected_sessions: tuple[str, ...]
    source_index: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker_id": self.marker_id,
            "update_id": self.update_id,
            "before_version": self.before_version,
            "after_version": self.after_version,
            "changed_entry_ids": list(self.changed_entry_ids),
            "expected_tokens": list(self.expected_tokens),
            "expected_sessions": list(self.expected_sessions),
            "index": self.source_index,
            "ordinal": self.source_index + 1,
            "source": self.source,
        }


@dataclass
class PolicyParseResult:
    policy_version: str
    schema_version: str = "1.0"
    base_drafts: list[EntryDraft] = field(default_factory=list)
    updates: list[PolicyUpdate] = field(default_factory=list)
    markers: list[SyncMarker] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def policy_identity(self) -> str:
        return sha256_value(
            {
                "schema_version": self.schema_version,
                "policy_version": self.policy_version,
                "entries": [
                    draft.doc
                    for draft in sorted(
                        self.base_drafts,
                        key=lambda item: (item.entry_index, item.id),
                    )
                ],
            }
        )


@dataclass(frozen=True)
class RawEvent:
    event: dict[str, Any]
    source: str
    index: int
    offset: int | None

    @property
    def event_id(self) -> str:
        return str(self.event.get("event_id", ""))

    @property
    def event_type(self) -> str:
        return str(self.event.get("type", ""))

    @property
    def timestamp(self) -> str | None:
        value = self.event.get("timestamp")
        if value is None:
            return None
        return normalize_timestamp(value)

    def sort_key(self) -> tuple[Any, ...]:
        try:
            timestamp = self.timestamp
        except (TypeError, ValueError):
            timestamp = None
        return (
            1 if timestamp is None else 0,
            timestamp or "",
            self.event_id,
            self.source,
            self.index,
        )


@dataclass
class EventFileResult:
    source: str
    events: list[RawEvent] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    status: str = "ok"


def _entry_error(
    code: str,
    message: str,
    *,
    source: str,
    index: int,
    details: dict[str, Any] | None = None,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        message=message,
        source=source,
        index=index,
        stable_key=str((details or {}).get("entry_id", "")),
        details=details or {},
    )


def _parse_entry_item(
    item: Any,
    *,
    index: int,
    entry_index: int,
    source: str,
    diagnostics: list[Diagnostic],
) -> EntryDraft | None:
    before = len(diagnostics)
    if not isinstance(item, dict):
        diagnostics.append(
            _entry_error(
                "E_SCHEMA",
                "policy entry must be a JSON object",
                source=source,
                index=index,
            )
        )
        return None

    entry_id = item.get("id")
    if not isinstance(entry_id, str) or POLICY_ID_RE.fullmatch(entry_id) is None:
        diagnostics.append(
            _entry_error(
                "E_ID_FORMAT",
                "policy entry id is missing or malformed",
                source=source,
                index=index,
                details={"entry_id": entry_id if isinstance(entry_id, str) else None},
            )
        )
        entry_id = None

    kind = item.get("kind")
    if not isinstance(kind, str) or kind not in ENTRY_KINDS:
        diagnostics.append(
            _entry_error(
                "E_UNKNOWN_KIND",
                "policy entry kind is missing or unsupported",
                source=source,
                index=index,
                details={
                    "entry_id": entry_id,
                    "kind": kind if isinstance(kind, str) else None,
                },
            )
        )

    version = item.get("version")
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        diagnostics.append(
            _entry_error(
                "E_VERSION",
                "policy entry version must be a semantic version such as 1.2.3",
                source=source,
                index=index,
                details={
                    "entry_id": entry_id,
                    "version": version if isinstance(version, str) else None,
                },
            )
        )
        version = None

    description = item.get("description")
    if description is not None and not isinstance(description, str):
        description = None

    normalized: dict[str, Any] = {
        "id": entry_id,
        "kind": kind,
        "version": version,
        "entry_index": entry_index,
    }
    if description is not None:
        normalized["description"] = description

    if kind == "role":
        parents = item.get("parents", [])
        if not isinstance(parents, list):
            diagnostics.append(
                _entry_error(
                    "E_ROLE_REF",
                    "role parents must be a list of policy ids",
                    source=source,
                    index=index,
                    details={"entry_id": entry_id, "parents": parents},
                )
            )
            parents = []
        valid_parents: list[str] = []
        for parent in parents:
            if not isinstance(parent, str) or POLICY_ID_RE.fullmatch(parent) is None:
                diagnostics.append(
                    _entry_error(
                        "E_ROLE_REF",
                        "role parent reference is malformed",
                        source=source,
                        index=index,
                        details={"entry_id": entry_id, "parent": parent},
                    )
                )
            else:
                valid_parents.append(parent)
        normalized["parents"] = sorted(set(valid_parents))

    elif kind == "token_rule":
        refs = item.get("refs")
        if not isinstance(refs, list) or not refs:
            diagnostics.append(
                _entry_error(
                    "E_TOKEN_REF",
                    "token rule refs must be a non-empty list of policy ids",
                    source=source,
                    index=index,
                    details={"entry_id": entry_id, "refs": refs},
                )
            )
            refs = []
        valid_refs: list[str] = []
        for target in refs:
            if not isinstance(target, str) or POLICY_ID_RE.fullmatch(target) is None:
                diagnostics.append(
                    _entry_error(
                        "E_TOKEN_REF",
                        "token rule policy reference is malformed",
                        source=source,
                        index=index,
                        details={"entry_id": entry_id, "ref": target},
                    )
                )
            else:
                valid_refs.append(target)
        normalized["refs"] = sorted(set(valid_refs))

    elif kind == "role_binding":
        subject = item.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            diagnostics.append(
                _entry_error(
                    "E_BINDING_REF",
                    "role binding subject must be a non-empty string",
                    source=source,
                    index=index,
                    details={"entry_id": entry_id},
                )
            )
        else:
            normalized["subject"] = subject
        for field_name, expected in (
            ("role", "role"),
            ("token_rule", "token_rule"),
            ("session_policy", "session_policy"),
        ):
            target = item.get(field_name)
            if not isinstance(target, str) or POLICY_ID_RE.fullmatch(target) is None:
                diagnostics.append(
                    _entry_error(
                        "E_BINDING_REF",
                        f"role binding {field_name} must be a policy id",
                        source=source,
                        index=index,
                        details={
                            "entry_id": entry_id,
                            "field": field_name,
                            "expected_kind": expected,
                            "target": target,
                        },
                    )
                )
            else:
                normalized[field_name] = target

    if len(diagnostics) != before or entry_id is None or version is None:
        return None
    return EntryDraft(doc=normalized, entry_index=entry_index, source=source)


def _parse_update_item(
    item: Any,
    *,
    index: int,
    source: str,
    diagnostics: list[Diagnostic],
) -> PolicyUpdate | None:
    before = len(diagnostics)
    if not isinstance(item, dict):
        diagnostics.append(
            Diagnostic(
                code="E_SCHEMA",
                message="policy update must be a JSON object",
                source=source,
                index=index,
            )
        )
        return None

    update_id = item.get("update_id")
    if not isinstance(update_id, str) or UPDATE_ID_RE.fullmatch(update_id) is None:
        diagnostics.append(
            Diagnostic(
                code="E_UPDATE_ID_FORMAT",
                message="update_id is missing or malformed",
                source=source,
                index=index,
                details={"update_id": update_id if isinstance(update_id, str) else None},
            )
        )
        update_id = None

    version = item.get("version")
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        diagnostics.append(
            Diagnostic(
                code="E_VERSION",
                message="update version must be a semantic version",
                source=source,
                index=index,
                details={"update_id": update_id, "version": version},
            )
        )
        version = None

    raw_timestamp = item.get("timestamp")
    timestamp: str | None = None
    if raw_timestamp is not None:
        try:
            timestamp = normalize_timestamp(raw_timestamp)
        except (TypeError, ValueError):
            timestamp = None
        if timestamp is None:
            diagnostics.append(
                Diagnostic(
                    code="E_TIMESTAMP",
                    message="update timestamp must be an ISO-8601 value",
                    source=source,
                    index=index,
                    details={"update_id": update_id, "timestamp": raw_timestamp},
                )
            )

    op = item.get("op", "put")
    if op not in {"put", "delete"}:
        diagnostics.append(
            Diagnostic(
                code="E_UPDATE_OP",
                message="update op must be put or delete",
                source=source,
                index=index,
                details={"update_id": update_id, "op": op},
            )
        )

    entry_draft: EntryDraft | None = None
    entry_id: str | None = None
    if op == "delete":
        raw_entry_id = item.get("entry_id")
        if (
            not isinstance(raw_entry_id, str)
            or POLICY_ID_RE.fullmatch(raw_entry_id) is None
        ):
            diagnostics.append(
                Diagnostic(
                    code="E_SCHEMA",
                    message="delete update requires a valid entry_id",
                    source=source,
                    index=index,
                    details={"update_id": update_id, "entry_id": raw_entry_id},
                )
            )
        else:
            entry_id = raw_entry_id
    elif op == "put":
        entry_draft = _parse_entry_item(
            item.get("entry"),
            index=index,
            entry_index=100000 + index,
            source=f"{source}#updates[{index}]",
            diagnostics=diagnostics,
        )
        if entry_draft is not None:
            entry_id = entry_draft.id
    else:
        entry_id = None

    if (
        len(diagnostics) != before
        or update_id is None
        or version is None
        or entry_id is None
    ):
        return None

    content_sha = sha256_bytes(canonical_json_bytes(item))
    return PolicyUpdate(
        update_id=update_id,
        timestamp=timestamp,
        version=version,
        op=op,
        entry=entry_draft,
        entry_id=entry_id,
        source_index=index,
        source=source,
        content_sha256=content_sha,
    )


def _parse_marker_item(
    item: Any,
    *,
    index: int,
    source: str,
    diagnostics: list[Diagnostic],
) -> SyncMarker | None:
    before = len(diagnostics)
    if not isinstance(item, dict):
        diagnostics.append(
            Diagnostic(
                code="E_MARKER",
                message="sync marker must be a JSON object",
                source=source,
                index=index,
            )
        )
        return None

    marker_id = item.get("marker_id")
    if not isinstance(marker_id, str) or MARKER_ID_RE.fullmatch(marker_id) is None:
        diagnostics.append(
            Diagnostic(
                code="E_MARKER",
                message="marker_id is missing or malformed",
                source=source,
                index=index,
            )
        )
        marker_id = None

    update_id = item.get("update_id")
    if not isinstance(update_id, str) or UPDATE_ID_RE.fullmatch(update_id) is None:
        diagnostics.append(
            Diagnostic(
                code="E_MARKER",
                message="sync marker update_id is missing or malformed",
                source=source,
                index=index,
                details={"marker_id": marker_id},
            )
        )

    before_version = item.get("before_version")
    after_version = item.get("after_version")
    for field_name, value in (
        ("before_version", before_version),
        ("after_version", after_version),
    ):
        if not isinstance(value, str) or VERSION_RE.fullmatch(value) is None:
            diagnostics.append(
                Diagnostic(
                    code="E_MARKER_VERSION",
                    message=f"sync marker {field_name} must be a semantic version",
                    source=source,
                    index=index,
                    details={"marker_id": marker_id, field_name: value},
                )
            )

    changed = item.get("changed_entry_ids", [])
    if not isinstance(changed, list) or not changed:
        diagnostics.append(
            Diagnostic(
                code="E_MARKER",
                message="sync marker changed_entry_ids must be a non-empty list",
                source=source,
                index=index,
                details={"marker_id": marker_id},
            )
        )
        changed = []
    changed_values: list[str] = []
    for value in changed:
        if not isinstance(value, str) or POLICY_ID_RE.fullmatch(value) is None:
            diagnostics.append(
                Diagnostic(
                    code="E_MARKER",
                    message="sync marker contains a malformed entry id",
                    source=source,
                    index=index,
                    details={"marker_id": marker_id, "entry_id": value},
                )
            )
        else:
            changed_values.append(value)

    expected_tokens = item.get("expected_tokens", [])
    expected_sessions = item.get("expected_sessions", [])
    for field_name, values, pattern in (
        ("expected_tokens", expected_tokens, TOKEN_ID_RE),
        ("expected_sessions", expected_sessions, SESSION_ID_RE),
    ):
        if not isinstance(values, list):
            diagnostics.append(
                Diagnostic(
                    code="E_MARKER",
                    message=f"sync marker {field_name} must be a list",
                    source=source,
                    index=index,
                    details={"marker_id": marker_id},
                )
            )
            continue
        for value in values:
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                diagnostics.append(
                    Diagnostic(
                        code="E_MARKER",
                        message=f"sync marker {field_name} contains a malformed id",
                        source=source,
                        index=index,
                        details={"marker_id": marker_id, "value": value},
                    )
                )

    if len(diagnostics) != before or marker_id is None or not isinstance(
        update_id, str
    ):
        return None
    return SyncMarker(
        marker_id=marker_id,
        update_id=update_id,
        before_version=str(before_version),
        after_version=str(after_version),
        changed_entry_ids=tuple(sorted(set(changed_values))),
        expected_tokens=tuple(sorted(set(expected_tokens))),
        expected_sessions=tuple(sorted(set(expected_sessions))),
        source_index=index,
        source=source,
    )


def parse_policy_document(
    document: Any,
    source: str = "policy.json",
) -> PolicyParseResult:
    diagnostics: list[Diagnostic] = []
    if not isinstance(document, dict):
        diagnostics.append(
            Diagnostic(
                code="E_SCHEMA",
                message="policy document must be a JSON object",
                source=source,
            )
        )
        return PolicyParseResult(
            policy_version="0.0.0",
            diagnostics=sort_diagnostics(diagnostics),
        )

    schema_version = document.get("schema_version", "1.0")
    if schema_version != "1.0":
        diagnostics.append(
            Diagnostic(
                code="E_SCHEMA",
                message="unsupported policy schema_version",
                source=source,
                details={"schema_version": schema_version, "supported": "1.0"},
            )
        )
        schema_version = "1.0"

    policy_version = document.get("policy_version")
    if not isinstance(policy_version, str) or VERSION_RE.fullmatch(policy_version) is None:
        diagnostics.append(
            Diagnostic(
                code="E_VERSION",
                message="policy_version must be a semantic version",
                source=source,
                details={"policy_version": policy_version},
            )
        )
        policy_version = "0.0.0"

    raw_entries = document.get("entries", [])
    if not isinstance(raw_entries, list):
        diagnostics.append(
            Diagnostic(
                code="E_SCHEMA",
                message="policy entries must be a list",
                source=source,
            )
        )
        raw_entries = []

    base_drafts: list[EntryDraft] = []
    for index, item in enumerate(raw_entries):
        draft = _parse_entry_item(
            item,
            index=index,
            entry_index=index,
            source=source,
            diagnostics=diagnostics,
        )
        if draft is not None:
            base_drafts.append(draft)

    id_groups: dict[str, list[EntryDraft]] = {}
    for draft in base_drafts:
        id_groups.setdefault(draft.id, []).append(draft)
    duplicate_ids = {entry_id for entry_id, group in id_groups.items() if len(group) > 1}
    if duplicate_ids:
        kept: list[EntryDraft] = []
        for draft in base_drafts:
            if draft.id in duplicate_ids:
                diagnostics.append(
                    _entry_error(
                        "E_DUP",
                        "duplicate policy entry id; all copies are rejected",
                        source=source,
                        index=draft.entry_index,
                        details={"entry_id": draft.id},
                    )
                )
            else:
                kept.append(draft)
        base_drafts = kept

    raw_updates = document.get("updates", [])
    if not isinstance(raw_updates, list):
        diagnostics.append(
            Diagnostic(
                code="E_SCHEMA",
                message="policy updates must be a list",
                source=source,
            )
        )
        raw_updates = []
    updates: list[PolicyUpdate] = []
    for index, item in enumerate(raw_updates):
        update = _parse_update_item(
            item,
            index=index,
            source=f"{source}#updates",
            diagnostics=diagnostics,
        )
        if update is not None:
            updates.append(update)

    update_groups: dict[str, list[PolicyUpdate]] = {}
    for update in updates:
        update_groups.setdefault(update.update_id, []).append(update)
    duplicate_updates = {
        update_id for update_id, group in update_groups.items() if len(group) > 1
    }
    if duplicate_updates:
        retained_updates: list[PolicyUpdate] = []
        for update in updates:
            if update.update_id in duplicate_updates:
                diagnostics.append(
                    Diagnostic(
                        code="E_UPDATE_DUP",
                        message="duplicate update_id; all copies are rejected",
                        source=update.source,
                        index=update.source_index,
                        stable_key=update.update_id,
                    )
                )
            else:
                retained_updates.append(update)
        updates = retained_updates
    updates.sort(key=lambda update: update.sort_key())

    raw_markers = document.get("sync_markers", [])
    if not isinstance(raw_markers, list):
        diagnostics.append(
            Diagnostic(
                code="E_MARKER",
                message="sync_markers must be a list",
                source=source,
            )
        )
        raw_markers = []
    markers: list[SyncMarker] = []
    for index, item in enumerate(raw_markers):
        marker = _parse_marker_item(
            item,
            index=index,
            source=f"{source}#sync_markers",
            diagnostics=diagnostics,
        )
        if marker is not None:
            markers.append(marker)

    update_by_id = {update.update_id: update for update in updates}
    valid_markers: list[SyncMarker] = []
    for marker in markers:
        update = update_by_id.get(marker.update_id)
        if update is None:
            diagnostics.append(
                Diagnostic(
                    code="E_SYNC",
                    message="sync marker references an unknown or rejected update",
                    source=marker.source,
                    index=marker.source_index,
                    stable_key=marker.marker_id,
                    details={"marker_id": marker.marker_id, "update_id": marker.update_id},
                )
            )
            continue
        if set(marker.changed_entry_ids) != set(update.changed_entry_ids):
            diagnostics.append(
                Diagnostic(
                    code="E_SYNC",
                    message="sync marker changed_entry_ids does not match the update",
                    source=marker.source,
                    index=marker.source_index,
                    stable_key=marker.marker_id,
                    details={
                        "marker_id": marker.marker_id,
                        "expected": list(update.changed_entry_ids),
                        "observed": list(marker.changed_entry_ids),
                    },
                )
            )
        valid_markers.append(marker)
    markers = sorted(
        valid_markers,
        key=lambda marker: (
            update_by_id[marker.update_id].sort_key()
            if marker.update_id in update_by_id
            else (9, "", (0, 0, 0), marker.update_id),
            marker.marker_id,
        ),
    )

    return PolicyParseResult(
        policy_version=policy_version,
        schema_version="1.0",
        base_drafts=sorted(base_drafts, key=lambda item: (item.entry_index, item.id)),
        updates=updates,
        markers=markers,
        diagnostics=sort_diagnostics(diagnostics),
    )


def apply_update_documents(
    base_drafts: Iterable[EntryDraft],
    updates: Iterable[PolicyUpdate],
) -> list[EntryDraft]:
    current = {draft.id: draft for draft in base_drafts}
    for update in sorted(updates, key=lambda item: item.sort_key()):
        if update.op == "delete":
            current.pop(update.entry_id, None)
            continue
        if update.entry is None:
            continue
        existing = current.get(update.entry_id)
        if existing is not None:
            draft = EntryDraft(
                doc={**update.entry.doc, "entry_index": existing.entry_index},
                entry_index=existing.entry_index,
                source=update.entry.source,
            )
        else:
            draft = update.entry
        current[update.entry_id] = draft
    return sorted(current.values(), key=lambda item: (item.entry_index, item.id))


def validate_entry_drafts(
    drafts: Iterable[EntryDraft],
    *,
    default_source: str,
) -> tuple[dict[str, PolicyEntry], list[Diagnostic]]:
    """Validate references to a fixed point so cascading rejections are explicit."""

    active: dict[str, EntryDraft] = {draft.id: draft for draft in drafts}
    diagnostics: list[Diagnostic] = []
    reported: set[tuple[str, str, str]] = set()

    while True:
        invalid: dict[str, list[Diagnostic]] = {}
        entries = {entry_id: draft for entry_id, draft in active.items()}
        for entry_id, draft in sorted(
            entries.items(),
            key=lambda item: (item[1].entry_index, item[0]),
        ):
            local: list[Diagnostic] = []
            doc = draft.doc
            if draft.kind == "role":
                for parent in doc.get("parents", []):
                    target = entries.get(parent)
                    if target is None or target.kind != "role":
                        local.append(
                            _entry_error(
                                "E_ROLE_REF",
                                "role parent does not resolve to an accepted role",
                                source=draft.source,
                                index=draft.entry_index,
                                details={
                                    "entry_id": entry_id,
                                    "target_id": parent,
                                    "expected_kind": "role",
                                },
                            )
                        )
            elif draft.kind == "token_rule":
                for ref in doc.get("refs", []):
                    if ref not in entries:
                        local.append(
                            _entry_error(
                                "E_TOKEN_REF",
                                "token rule policy reference does not exist",
                                source=draft.source,
                                index=draft.entry_index,
                                details={
                                    "entry_id": entry_id,
                                    "target_id": ref,
                                },
                            )
                        )
            elif draft.kind == "role_binding":
                for field_name, expected_kind in (
                    ("role", "role"),
                    ("token_rule", "token_rule"),
                    ("session_policy", "session_policy"),
                ):
                    target_id = doc.get(field_name)
                    target = entries.get(target_id) if isinstance(target_id, str) else None
                    if target is None or target.kind != expected_kind:
                        local.append(
                            _entry_error(
                                "E_BINDING_REF",
                                "role binding reference does not resolve to the expected kind",
                                source=draft.source,
                                index=draft.entry_index,
                                details={
                                    "entry_id": entry_id,
                                    "field": field_name,
                                    "target_id": target_id,
                                    "expected_kind": expected_kind,
                                },
                            )
                        )
            if local:
                invalid[entry_id] = local

        if not invalid:
            break
        for entry_id, problems in invalid.items():
            del active[entry_id]
            for problem in problems:
                signature = (
                    problem.code,
                    entry_id,
                    str(problem.details.get("target_id", "")),
                )
                if signature not in reported:
                    diagnostics.append(problem)
                    reported.add(signature)

    entries: dict[str, PolicyEntry] = {}
    for entry_id, draft in active.items():
        doc = draft.doc
        entries[entry_id] = PolicyEntry(
            id=entry_id,
            kind=draft.kind,
            version=draft.version,
            entry_index=draft.entry_index,
            parents=tuple(doc.get("parents", [])),
            refs=tuple(doc.get("refs", [])),
            subject=doc.get("subject"),
            role=doc.get("role"),
            token_rule=doc.get("token_rule"),
            session_policy=doc.get("session_policy"),
            description=doc.get("description"),
            source=draft.source or default_source,
        )
    return entries, sort_diagnostics(diagnostics)


def parse_event_file(
    source: str,
    raw: str,
    *,
    load_error: Diagnostic | None = None,
) -> EventFileResult:
    result = EventFileResult(source=source)
    if load_error is not None:
        result.status = "failed"
        result.diagnostics.append(load_error)
        return result

    try:
        if raw.lstrip().startswith("["):
            records = list(iter_json_array(raw))
        else:
            records = _parse_json_lines(raw, result)
    except (ValueError, json.JSONDecodeError) as exc:
        position = getattr(exc, "pos", None)
        result.status = "failed"
        result.diagnostics.append(
            Diagnostic(
                code="E_JSON",
                message=f"event file is not valid JSON: {exc}",
                source=source,
                offset=position,
            )
        )
        return result

    for index, offset, event in records:
        if isinstance(event, dict):
            result.events.append(
                RawEvent(event=event, source=source, index=index, offset=offset)
            )
        else:
            result.status = "partial"
            result.diagnostics.append(
                Diagnostic(
                    code="E_SCHEMA",
                    message="event must be a JSON object",
                    source=source,
                    index=index,
                    offset=offset,
                )
            )

    event_groups: dict[str, list[RawEvent]] = {}
    for event in result.events:
        event_groups.setdefault(event.event_id, []).append(event)
    duplicates = {
        event_id
        for event_id, group in event_groups.items()
        if event_id and len(group) > 1
    }
    for event_id in sorted(duplicates):
        for event in event_groups[event_id]:
            result.status = "partial"
            result.diagnostics.append(
                Diagnostic(
                    code="E_EVENT_DUP",
                    message="duplicate event_id; all copies are rejected",
                    source=event.source,
                    index=event.index,
                    offset=event.offset,
                    stable_key=event_id,
                    details={"event_id": event_id},
                )
            )
    if duplicates:
        result.events = [
            event for event in result.events if event.event_id not in duplicates
        ]
    result.diagnostics = sort_diagnostics(result.diagnostics)
    return result


def _parse_json_lines(
    raw: str,
    file_result: EventFileResult,
) -> list[tuple[int, int, Any]]:
    records: list[tuple[int, int, Any]] = []
    offset = 0
    index = 0
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            line_offset = offset + len(line) - len(line.lstrip())
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                file_result.status = "partial"
                file_result.diagnostics.append(
                    Diagnostic(
                        code="E_JSON",
                        message=f"event JSONL line is invalid: {exc.msg}",
                        source=file_result.source,
                        index=index,
                        offset=line_offset + exc.pos,
                    )
                )
            else:
                records.append((index, line_offset, value))
            index += 1
        offset += len(line)
    if not raw.strip():
        return []
    return records


def validate_raw_event(event: RawEvent) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    event_id = event.event.get("event_id")
    if not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id) is None:
        diagnostics.append(
            Diagnostic(
                code="E_EVENT_ID_FORMAT",
                message="event_id is missing or malformed",
                source=event.source,
                index=event.index,
                offset=event.offset,
                stable_key=event_id if isinstance(event_id, str) else "",
                details={"event_id": event_id},
            )
        )

    event_type = event.event.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        diagnostics.append(
            Diagnostic(
                code="E_EVENT_TYPE",
                message="event type is missing or unsupported",
                source=event.source,
                index=event.index,
                offset=event.offset,
                stable_key=event_id if isinstance(event_id, str) else "",
                details={"event_id": event_id, "type": event_type},
            )
        )

    raw_timestamp = event.event.get("timestamp")
    if raw_timestamp is not None:
        try:
            normalize_timestamp(raw_timestamp)
        except (TypeError, ValueError):
            diagnostics.append(
                Diagnostic(
                    code="E_TIMESTAMP",
                    message="event timestamp must be an ISO-8601 value",
                    source=event.source,
                    index=event.index,
                    offset=event.offset,
                    stable_key=event_id if isinstance(event_id, str) else "",
                    details={"event_id": event_id, "timestamp": raw_timestamp},
                )
            )

    session_id = event.event.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID_RE.fullmatch(session_id) is None:
        diagnostics.append(
            Diagnostic(
                code="E_SESSION_FORMAT",
                message="session_id is missing or malformed",
                source=event.source,
                index=event.index,
                offset=event.offset,
                stable_key=event_id if isinstance(event_id, str) else "",
                details={"event_id": event_id, "session_id": session_id},
            )
        )

    if event_type in {"login", "renew", "token_revoke"}:
        token_id = event.event.get("token_id")
        if not isinstance(token_id, str) or TOKEN_ID_RE.fullmatch(token_id) is None:
            diagnostics.append(
                Diagnostic(
                    code="E_TOKEN_FORMAT",
                    message="token_id is missing or malformed",
                    source=event.source,
                    index=event.index,
                    offset=event.offset,
                    stable_key=event_id if isinstance(event_id, str) else "",
                    details={"event_id": event_id, "token_id": token_id},
                )
            )
    elif event.event.get("token_id") is not None:
        token_id = event.event.get("token_id")
        if not isinstance(token_id, str) or TOKEN_ID_RE.fullmatch(token_id) is None:
            diagnostics.append(
                Diagnostic(
                    code="E_TOKEN_FORMAT",
                    message="token_id is malformed",
                    source=event.source,
                    index=event.index,
                    offset=event.offset,
                    stable_key=event_id if isinstance(event_id, str) else "",
                    details={"event_id": event_id, "token_id": token_id},
                )
            )

    if event_type in {"login", "renew", "role_change"}:
        binding_id = event.event.get("binding_id")
        if not isinstance(binding_id, str) or POLICY_ID_RE.fullmatch(binding_id) is None:
            diagnostics.append(
                Diagnostic(
                    code="E_EVENT_REF",
                    message="binding_id is missing or malformed",
                    source=event.source,
                    index=event.index,
                    offset=event.offset,
                    stable_key=event_id if isinstance(event_id, str) else "",
                    details={"event_id": event_id, "binding_id": binding_id},
                )
            )

    return sort_diagnostics(diagnostics)
