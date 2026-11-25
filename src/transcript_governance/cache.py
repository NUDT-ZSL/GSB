"""Content-addressed artifact cache and dependency-chain replay."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .codes import Code, Diagnostic, GovernanceError
from .hashing import canonical_json, digest_json


CACHE_ARTIFACT_VERSION = "1"


@dataclass(frozen=True)
class CacheKey:
    key: str
    namespace: str
    inputs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        namespace: str,
        *,
        pcm_hash: str | None = None,
        transcript_digest: str | dict[str, str] | None = None,
        format_version: str | int | None = None,
        config: dict[str, Any] | None = None,
        upstream_hashes: dict[str, str] | None = None,
        artifact_version: str | int = CACHE_ARTIFACT_VERSION,
        interval: tuple[int, int] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "CacheKey":
        inputs: dict[str, Any] = {
            "artifact_version": artifact_version,
            "namespace": namespace,
            "pcm_hash": pcm_hash,
            "transcript_digest": transcript_digest,
            "format_version": format_version,
            "config": config or {},
            "upstream_hashes": upstream_hashes or {},
            "interval": list(interval) if interval is not None else None,
        }
        if extra:
            inputs["extra"] = extra
        return cls(key=digest_json(inputs), namespace=namespace, inputs=inputs)

    def short(self, length: int = 12) -> str:
        return self.key[:length]


class ContentAddressedCache:
    """A small deterministic JSON object cache.

    Values are stored at ``objects/<prefix>/<key>.json``.  The cache never mutates an
    existing key; changed inputs produce a new key while unrelated objects remain on disk.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str | CacheKey) -> Path:
        value = key.key if isinstance(key, CacheKey) else key
        return self.objects / value[:2] / f"{value}.json"

    def has(self, key: str | CacheKey) -> bool:
        return self.path_for(key).exists()

    def put(
        self,
        key: CacheKey,
        value: Any,
        *,
        dependencies: Iterable[str] = (),
        interval: tuple[int, int] | None = None,
    ) -> str:
        path = self.path_for(key.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "key": key.key,
            "namespace": key.namespace,
            "inputs": key.inputs,
            "dependencies": list(dependencies),
            "interval": list(interval) if interval is not None else key.inputs.get("interval"),
            "value": value,
            "value_digest": digest_json(value),
        }
        encoded = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.write_text(encoded, encoding="utf-8")
        return key.key

    def get(self, key: str | CacheKey) -> Any | None:
        try:
            envelope = self.get_envelope(key)
        except GovernanceError:
            return None
        return envelope.get("value")

    def get_envelope(self, key: str | CacheKey) -> dict[str, Any]:
        key_value = key.key if isinstance(key, CacheKey) else key
        path = self.path_for(key_value)
        if not path.exists():
            raise GovernanceError(Code.E_CACHE_MISS, "cache key does not exist", key=key_value)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GovernanceError(Code.E_CACHE_CORRUPT, "cache object is not readable JSON", key=key_value) from exc
        if envelope.get("key") != key_value or digest_json(envelope.get("value")) != envelope.get("value_digest"):
            raise GovernanceError(Code.E_CACHE_CORRUPT, "cache object checksum verification failed", key=key_value)
        return envelope

    def get_or_compute(
        self,
        key: CacheKey,
        compute: Callable[[], Any],
        *,
        dependencies: Iterable[str] = (),
        interval: tuple[int, int] | None = None,
    ) -> tuple[Any, bool]:
        if self.has(key.key):
            return self.get(key.key), True
        value = compute()
        self.put(key, value, dependencies=dependencies, interval=interval)
        return value, False

    def iter_envelopes(self) -> Iterable[dict[str, Any]]:
        for path in sorted(self.objects.glob("*/*.json")):
            try:
                yield json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                yield {"key": path.stem, "corrupt": True, "path": str(path)}

    def invalidate_intervals(
        self,
        intervals: Iterable[tuple[int, int]],
        *,
        namespace: str | None = None,
    ) -> list[str]:
        targets = list(intervals)
        removed: list[str] = []
        for envelope in self.iter_envelopes():
            if envelope.get("corrupt"):
                continue
            if namespace is not None and envelope.get("namespace") != namespace:
                continue
            raw_interval = envelope.get("interval")
            if raw_interval is None:
                continue
            start, end = int(raw_interval[0]), int(raw_interval[1])
            intersects = any(max(start, target_start) < min(end, target_end) for target_start, target_end in targets)
            if intersects:
                key = envelope["key"]
                path = self.path_for(key)
                path.unlink(missing_ok=True)
                removed.append(key)
        return removed

    def keys(self) -> list[str]:
        return [str(envelope.get("key")) for envelope in self.iter_envelopes() if envelope.get("key")]

    def stats(self) -> dict[str, int]:
        keys = self.keys()
        namespaces: dict[str, int] = {}
        for envelope in self.iter_envelopes():
            namespace = str(envelope.get("namespace", "unknown"))
            namespaces[namespace] = namespaces.get(namespace, 0) + 1
        return {"objects": len(keys), **{f"namespace_{name}": count for name, count in namespaces.items()}}

    def replay_chain(self, root_key: str) -> dict[str, Any]:
        visited: set[str] = set()
        chain: list[dict[str, Any]] = []

        def visit(key: str, stack: set[str]) -> None:
            if key in stack:
                raise GovernanceError(Code.E_DEPENDENCY_CYCLE, "cache dependency cycle", key=key)
            if key in visited:
                return
            envelope = self.get_envelope(key)
            stack.add(key)
            for parent in envelope.get("dependencies", []):
                visit(str(parent), stack)
            stack.remove(key)
            visited.add(key)
            chain.append(
                {
                    "key": key,
                    "namespace": envelope.get("namespace"),
                    "value_digest": envelope.get("value_digest"),
                    "dependencies": envelope.get("dependencies", []),
                }
            )

        try:
            visit(root_key, set())
        except GovernanceError as exc:
            return {"ok": False, "code": exc.code, "diagnostic": exc.diagnostic.to_dict(), "chain": chain}
        return {"ok": True, "code": Code.OK, "root_key": root_key, "chain": chain}


def cache_diagnostic(code: str, message: str, **details: Any) -> Diagnostic:
    return Diagnostic(code=code, message=message, details=details)


def config_hash(config: dict[str, Any]) -> str:
    return digest_json(config or {})


def flatten_config(config: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in sorted(config.items()):
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            result.update(flatten_config(value, name))
        else:
            result[name] = canonical_json(value)
    return result


def config_changed_keys(old: dict[str, Any], new: dict[str, Any]) -> set[str]:
    old_flat = flatten_config(old or {})
    new_flat = flatten_config(new or {})
    return {key for key in old_flat.keys() | new_flat.keys() if old_flat.get(key) != new_flat.get(key)}
