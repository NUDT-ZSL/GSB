from __future__ import annotations

import base64
import copy
import hashlib
import heapq
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import CycleError, PolicyError
from .util import canonical_json, stable_hash


ALLOWED_KEY_FIELDS = ("path", "tenant", "encoding")
ALLOWED_RULES = ("concat", "sha256_concat", "json_manifest")
ALLOWED_EVICTION_RULES = ("lru", "fifo", "ttl")


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_object(value: Any, location: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{what} must be a JSON object", location=location)
    return value


def _path_key(path: str) -> str:
    if not path.startswith("/"):
        raise PolicyError("content path must start with '/'", location=path)
    if "\n" in path or "\r" in path or "\x00" in path:
        raise PolicyError("content path contains a forbidden control character", location=path)
    return path


@dataclass(frozen=True)
class Policy:
    schema_version: int
    policy_version: str
    key_fields: tuple[str, ...]
    ttl_seconds_by_path: dict[str, int]
    node_capacity: int
    eviction_rule: str
    raw: dict[str, Any]

    @property
    def policy_hash(self) -> str:
        return stable_hash(self.raw)

    def ttl_ms_for(self, path: str) -> int:
        try:
            return self.ttl_seconds_by_path[path] * 1000
        except KeyError as exc:
            raise PolicyError(
                f"no TTL rule for path {path!r}",
                location=f"ttl_seconds_by_path[{path!r}]",
            ) from exc

    def key_for(self, path: str, values: Mapping[str, Any] | None = None) -> str:
        values = values or {}
        components: list[list[str]] = []
        for field in self.key_fields:
            value = path if field == "path" else values.get(field)
            if value is None:
                raise PolicyError(
                    f"cache key requires field {field!r}",
                    location=f"request.key_values.{field}",
                    path=path,
                )
            if not isinstance(value, (str, int, float, bool)):
                raise PolicyError(
                    f"cache key field {field!r} must be a scalar value",
                    location=f"request.key_values.{field}",
                )
            components.append([field, str(value)])
        return canonical_json(components)

    def to_doc(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    @classmethod
    def from_doc(cls, doc: Any, content_paths: Iterable[str]) -> "Policy":
        obj = _require_object(doc, "$", "policy")
        if obj.get("schema_version") != 1:
            raise PolicyError("schema_version must be 1", location="$.schema_version")

        policy_version = obj.get("policy_version")
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise PolicyError(
                "policy_version must be a non-empty string",
                location="$.policy_version",
            )

        key_fields = obj.get("key_fields")
        if not isinstance(key_fields, list) or not key_fields:
            raise PolicyError(
                "key_fields must be a non-empty array",
                location="$.key_fields",
            )
        seen_fields: set[str] = set()
        normalized_fields: list[str] = []
        for index, field in enumerate(key_fields):
            location = f"$.key_fields[{index}]"
            if not isinstance(field, str):
                raise PolicyError("cache key field must be a string", location=location)
            if field not in ALLOWED_KEY_FIELDS:
                raise PolicyError(
                    f"unsupported cache key field {field!r}; allowed={list(ALLOWED_KEY_FIELDS)}",
                    location=location,
                )
            if field in seen_fields:
                raise PolicyError(
                    f"duplicate cache key field {field!r}",
                    location=location,
                )
            seen_fields.add(field)
            normalized_fields.append(field)
        if "path" not in seen_fields:
            raise PolicyError(
                "key_fields must include 'path' to avoid cross-path collisions",
                location="$.key_fields",
            )

        ttl_map = obj.get("ttl_seconds_by_path")
        if not isinstance(ttl_map, dict):
            raise PolicyError(
                "ttl_seconds_by_path must be a JSON object",
                location="$.ttl_seconds_by_path",
            )
        content_path_set = set(content_paths)
        ttl_normalized: dict[str, int] = {}
        for path, ttl in ttl_map.items():
            location = f"$.ttl_seconds_by_path[{path!r}]"
            if not isinstance(path, str) or not path.startswith("/"):
                raise PolicyError("TTL path must start with '/'", location=location)
            if not _is_positive_int(ttl):
                raise PolicyError(
                    "TTL must be a positive integer number of seconds",
                    location=location,
                )
            if path not in content_path_set:
                raise PolicyError(
                    f"TTL rule references unknown content path {path!r}",
                    location=location,
                )
            ttl_normalized[path] = ttl
        missing_paths = sorted(content_path_set - set(ttl_normalized))
        if missing_paths:
            raise PolicyError(
                "every content path needs an explicit TTL",
                location="$.ttl_seconds_by_path",
                missing_paths=missing_paths,
            )

        capacity = obj.get("node_capacity")
        if not _is_positive_int(capacity):
            raise PolicyError(
                "node_capacity must be a positive integer",
                location="$.node_capacity",
            )

        eviction_rule = obj.get("eviction_rule")
        if eviction_rule not in ALLOWED_EVICTION_RULES:
            raise PolicyError(
                f"eviction_rule must be one of {list(ALLOWED_EVICTION_RULES)}",
                location="$.eviction_rule",
            )

        return cls(
            schema_version=1,
            policy_version=policy_version,
            key_fields=tuple(normalized_fields),
            ttl_seconds_by_path=ttl_normalized,
            node_capacity=capacity,
            eviction_rule=eviction_rule,
            raw=copy.deepcopy(obj),
        )


@dataclass(frozen=True)
class EffectiveContent:
    path: str
    etag: str
    body: bytes
    media_type: str
    sources: tuple[str, ...] = ()
    rule: str | None = None

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def body_encoding(self) -> str:
        try:
            self.body.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            return "base64"

    def body_text(self) -> str:
        if self.body_encoding == "utf-8":
            return self.body.decode("utf-8")
        return base64.b64encode(self.body).decode("ascii")


@dataclass(frozen=True)
class ContentState:
    schema_version: int
    content_version: str
    raw: dict[str, Any]
    effective: dict[str, EffectiveContent]
    sources: dict[str, tuple[str, ...]]
    dependents: dict[str, tuple[str, ...]]

    @property
    def content_set_hash(self) -> str:
        return stable_hash({path: value.etag for path, value in self.effective.items()})

    @property
    def versions(self) -> dict[str, str]:
        return {path: value.etag for path, value in self.effective.items()}

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.effective))

    def to_doc(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    def descendants(self, trigger_path: str) -> tuple[str, ...]:
        """Return a stable topological order of trigger plus all descendants."""

        if trigger_path not in self.effective:
            raise PolicyError(
                f"unknown trigger path {trigger_path!r}",
                location="derivations",
            )
        affected = {trigger_path}
        queue = [trigger_path]
        while queue:
            current = queue.pop(0)
            for dependent in self.dependents.get(current, ()):  # already sorted
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)

        indegree: dict[str, int] = {path: 0 for path in affected}
        for path in affected:
            for source in self.sources.get(path, ()):
                if source in affected:
                    indegree[path] += 1
        ready = [path for path, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        ordered: list[str] = []
        while ready:
            path = heapq.heappop(ready)
            ordered.append(path)
            for dependent in self.dependents.get(path, ()):
                if dependent not in indegree:
                    continue
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, dependent)
        if len(ordered) != len(affected):
            # The loader rejects cycles, so reaching this branch indicates a bug
            # or a corrupted runtime graph.
            raise CycleError(
                "derived graph is not acyclic during propagation",
                location=f"derivations[{trigger_path!r}]",
            )
        return tuple(ordered)

    def affected_order(self, trigger_path: str) -> tuple[tuple[str, int], ...]:
        ordered = self.descendants(trigger_path)
        depths: dict[str, int] = {trigger_path: 0}
        for path in ordered:
            if path == trigger_path:
                continue
            parent_depths = [depths[source] for source in self.sources[path] if source in depths]
            depths[path] = (max(parent_depths) + 1) if parent_depths else 0
        return tuple((path, depths[path]) for path in ordered)

    def evolution_document(
        self,
        path: str,
        new_etag: str,
        new_body: str | bytes,
        body_encoding: str = "utf-8",
    ) -> dict[str, Any]:
        if path not in self.effective:
            raise PolicyError(f"unknown content path {path!r}", location="contents")
        if self.sources.get(path):
            raise PolicyError(
                f"derived path {path!r} cannot be mutated directly",
                location=f"contents[{path!r}]",
            )
        if not isinstance(new_etag, str) or not new_etag.strip():
            raise PolicyError("new ETag must be a non-empty string", location="evolve.etag")
        if body_encoding not in ("utf-8", "base64"):
            raise PolicyError(
                "body_encoding must be 'utf-8' or 'base64'",
                location="evolve.body_encoding",
            )
        if isinstance(new_body, bytes):
            if body_encoding != "base64":
                try:
                    new_body = new_body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise PolicyError(
                        "binary body requires body_encoding='base64'",
                        location="evolve.body",
                    ) from exc
            else:
                new_body = base64.b64encode(new_body).decode("ascii")

        doc = copy.deepcopy(self.raw)
        for item in doc["contents"]:
            if item["path"] == path:
                old_body = _decode_body(item)
                new_body_bytes = _decode_body({"body": new_body, "body_encoding": body_encoding})
                if old_body == new_body_bytes and item.get("etag") != new_etag:
                    raise PolicyError(
                        "ETag changed without a content change",
                        location=f"contents[{path!r}].etag",
                    )
                if old_body != new_body_bytes and item.get("etag") == new_etag:
                    raise PolicyError(
                        "content changed without a new ETag; downstream versions would be ambiguous",
                        location=f"contents[{path!r}].etag",
                    )
                item["etag"] = new_etag
                item["body"] = new_body
                item["body_encoding"] = body_encoding
                break
        return doc

    def with_evolved_base(
        self,
        path: str,
        new_etag: str,
        new_body: str | bytes,
        body_encoding: str = "utf-8",
    ) -> "ContentState":
        doc = self.evolution_document(path, new_etag, new_body, body_encoding)
        return ContentState._from_doc(doc, normalize_derived_etags=True)

    @classmethod
    def from_doc(cls, doc: Any) -> "ContentState":
        return cls._from_doc(doc, normalize_derived_etags=False)

    @classmethod
    def _from_doc(cls, doc: Any, normalize_derived_etags: bool) -> "ContentState":
        obj = _require_object(doc, "$", "content declaration")
        if obj.get("schema_version") != 1:
            raise PolicyError("schema_version must be 1", location="$.schema_version")
        content_version = obj.get("content_version")
        if not isinstance(content_version, str) or not content_version.strip():
            raise PolicyError(
                "content_version must be a non-empty string",
                location="$.content_version",
            )
        contents = obj.get("contents")
        if not isinstance(contents, list) or not contents:
            raise PolicyError("contents must be a non-empty array", location="$.contents")

        by_path: dict[str, dict[str, Any]] = {}
        locations: dict[str, str] = {}
        for index, item in enumerate(contents):
            location = f"$.contents[{index}]"
            item = _require_object(item, location, "content entry")
            path = item.get("path")
            if not isinstance(path, str):
                raise PolicyError("content path must be a string", location=f"{location}.path")
            _path_key(path)
            if path in by_path:
                raise PolicyError(
                    f"duplicate content path {path!r}",
                    location=f"{location}.path",
                )
            by_path[path] = item
            locations[path] = location

        sources: dict[str, tuple[str, ...]] = {}
        rules: dict[str, str | None] = {}
        for path, item in by_path.items():
            location = locations[path]
            has_derivation = "derived" in item
            if has_derivation:
                if "body" in item:
                    raise PolicyError(
                        "derived entries must not declare body; it is computed deterministically",
                        location=location,
                    )
                if "etag" in item and not isinstance(item["etag"], str):
                    raise PolicyError(
                        "derived ETag declaration must be a string when present",
                        location=f"{location}.etag",
                    )
                derived = _require_object(item["derived"], f"{location}.derived", "derived")
                refs = derived.get("sources")
                if not isinstance(refs, list) or not refs:
                    raise PolicyError(
                        "derived.sources must be a non-empty array",
                        location=f"{location}.derived.sources",
                    )
                normalized_refs: list[str] = []
                seen_refs: set[str] = set()
                for ref_index, ref in enumerate(refs):
                    ref_location = f"{location}.derived.sources[{ref_index}]"
                    if not isinstance(ref, str):
                        raise PolicyError("source reference must be a path string", location=ref_location)
                    if ref not in by_path:
                        raise PolicyError(
                            f"derived source path {ref!r} does not exist",
                            location=ref_location,
                        )
                    if ref in seen_refs:
                        raise PolicyError(f"duplicate derived source {ref!r}", location=ref_location)
                    if ref == path:
                        raise CycleError("derived entry references itself", location=ref_location)
                    seen_refs.add(ref)
                    normalized_refs.append(ref)
                rule = derived.get("rule")
                if rule not in ALLOWED_RULES:
                    raise PolicyError(
                        f"derived.rule must be one of {list(ALLOWED_RULES)}",
                        location=f"{location}.derived.rule",
                    )
                sources[path] = tuple(normalized_refs)
                rules[path] = rule
            else:
                if "body" not in item:
                    raise PolicyError("base content requires body", location=f"{location}.body")
                if not isinstance(item.get("body"), str):
                    raise PolicyError("body must be a JSON string", location=f"{location}.body")
                body_encoding = item.get("body_encoding", "utf-8")
                if body_encoding not in ("utf-8", "base64"):
                    raise PolicyError(
                        "body_encoding must be 'utf-8' or 'base64'",
                        location=f"{location}.body_encoding",
                    )
                _decode_body(item)
                etag = item.get("etag")
                if not isinstance(etag, str) or not etag.strip():
                    raise PolicyError(
                        "base content requires a non-empty etag",
                        location=f"{location}.etag",
                    )
                sources[path] = ()
                rules[path] = None
            media_type = item.get("media_type", "application/octet-stream")
            if not isinstance(media_type, str) or not media_type:
                raise PolicyError("media_type must be a non-empty string", location=f"{location}.media_type")

        # Cycle detection uses a deterministic path order and reports the exact
        # declaration location of the edge that closes the cycle.
        state: dict[str, int] = {path: 0 for path in by_path}
        stack: list[str] = []

        def visit(path: str) -> None:
            if state[path] == 1:
                cycle_start = stack.index(path) if path in stack else 0
                cycle = stack[cycle_start:] + [path]
                raise CycleError(
                    "derived relationship contains a cycle: " + " -> ".join(cycle),
                    location=f"{locations[path]}.derived.sources",
                    cycle=cycle,
                )
            if state[path] == 2:
                return
            state[path] = 1
            stack.append(path)
            for ref in sources.get(path, ()):
                visit(ref)
            stack.pop()
            state[path] = 2

        for path in sorted(sources):
            visit(path)

        dependents: dict[str, list[str]] = {path: [] for path in by_path}
        for path, refs in sources.items():
            for ref in refs:
                dependents[ref].append(path)
        normalized_dependents = {
            path: tuple(sorted(values)) for path, values in dependents.items()
        }

        effective: dict[str, EffectiveContent] = {}
        visiting: set[str] = set()

        def compute(path: str) -> EffectiveContent:
            if path in effective:
                return effective[path]
            if path in visiting:
                raise CycleError("cycle while computing derived content", location=locations[path])
            visiting.add(path)
            item = by_path[path]
            media_type = item.get("media_type", "application/octet-stream")
            refs = sources[path]
            if refs:
                upstream = [compute(ref) for ref in refs]
                rule = rules[path] or ""
                if rule == "concat":
                    body = b"".join(value.body for value in upstream)
                elif rule == "sha256_concat":
                    digest = hashlib.sha256(b"".join(value.body for value in upstream)).hexdigest()
                    body = digest.encode("ascii")
                elif rule == "json_manifest":
                    manifest = [
                        {
                            "path": value.path,
                            "etag": value.etag,
                            "sha256": value.body_sha256,
                            "size": len(value.body),
                        }
                        for value in upstream
                    ]
                    body = canonical_json(manifest).encode("utf-8")
                else:  # pragma: no cover - validated above
                    raise PolicyError("unsupported derivation rule", location=locations[path])
                etag = '"sha256-' + hashlib.sha256(body).hexdigest() + '"'
                declared_etag = item.get("etag")
                if normalize_derived_etags:
                    item["etag"] = etag
                elif declared_etag is not None and declared_etag != etag:
                    raise PolicyError(
                        "derived ETag does not match deterministic content",
                        location=f"{locations[path]}.etag",
                        expected=etag,
                        actual=declared_etag,
                    )
                value = EffectiveContent(
                    path=path,
                    etag=etag,
                    body=body,
                    media_type=media_type,
                    sources=refs,
                    rule=rule,
                )
            else:
                value = EffectiveContent(
                    path=path,
                    etag=item["etag"],
                    body=_decode_body(item),
                    media_type=media_type,
                )
            visiting.remove(path)
            effective[path] = value
            return value

        for path in sorted(by_path):
            compute(path)

        return cls(
            schema_version=1,
            content_version=content_version,
            raw=copy.deepcopy(obj),
            effective=effective,
            sources=sources,
            dependents=normalized_dependents,
        )


def _decode_body(item: Mapping[str, Any]) -> bytes:
    body = item.get("body")
    encoding = item.get("body_encoding", "utf-8")
    if not isinstance(body, str):
        raise PolicyError("body must be a string", location="body")
    if encoding == "utf-8":
        return body.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(body.encode("ascii"), validate=True)
        except Exception as exc:
            raise PolicyError("body is not valid base64", location="body") from exc
    raise PolicyError("unknown body_encoding", location="body_encoding")


def load_policy_document(policy_doc: Any, content_doc: Any) -> tuple[Policy, ContentState]:
    content = ContentState.from_doc(content_doc)
    policy = Policy.from_doc(policy_doc, content.paths)
    return policy, content


def policy_and_content_hashes(policy: Policy, content: ContentState) -> tuple[str, str]:
    return policy.policy_hash, content.content_set_hash


def parse_json_document(text: str, location: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(
            f"invalid {what} JSON: {exc.msg}",
            location=f"{location}:{exc.lineno}:{exc.colno}",
        ) from exc
    return _require_object(value, location, what)
