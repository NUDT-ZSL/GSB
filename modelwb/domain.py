from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .canonical import canonical_json_bytes, sha256_bytes
from .errors import fail


SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_name(value: Any, field_name: str, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(
            "VALIDATION_ERROR",
            f"{field_name} must be a non-empty string",
            field=field_name,
            path=path,
            value=value,
        )
    return value


def _parse_revision(entry: dict[str, Any], path: str) -> int:
    for key in ("revision", "sequence", "version_number"):
        if key in entry:
            value = entry[key]
            if not _is_int(value):
                fail("VALIDATION_ERROR", f"{key} must be an integer", path=path, field=key, value=value)
            return value
    version = str(entry.get("version", entry.get("id", "")))
    match = re.search(r"(\d+)$", version)
    return int(match.group(1)) if match else 0


def _normalise_string_list(value: Any, field_name: str, *, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        fail(
            "VALIDATION_ERROR",
            f"{field_name} must be a list of strings",
            field=field_name,
            path=path,
            value=value,
        )
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            fail(
                "VALIDATION_ERROR",
                f"{field_name} contains a non-string reference",
                field=field_name,
                path=path,
                value=item,
            )
        result.append(item)
    return tuple(dict.fromkeys(result))


def normalise_dependencies(raw: Any, *, path: str) -> dict[str, tuple[str, ...]]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        fail("VALIDATION_ERROR", "dependencies must be an object", path=path, value=raw)
    feature_tables = raw.get("feature_tables", raw.get("features", []))
    upstream_models = raw.get("upstream_models", raw.get("upstreams", []))
    return {
        "feature_tables": _normalise_string_list(
            feature_tables, "dependencies.feature_tables", path=path
        ),
        "upstream_models": _normalise_string_list(
            upstream_models, "dependencies.upstream_models", path=path
        ),
    }


@dataclass(frozen=True)
class Shard:
    layer: str
    shard_id: str
    index: int
    shard_count: int
    sha256: str
    size: int | None = None
    path: str | None = None
    byte_start: int | None = None
    byte_end: int | None = None

    def identity_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "shard_id": self.shard_id,
            "index": self.index,
            "shard_count": self.shard_count,
            "sha256": self.sha256,
            "size": self.size,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }


@dataclass(frozen=True)
class Layer:
    name: str
    parameters: int
    shards: tuple[Shard, ...]

    def identity_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "parameters": self.parameters,
            "shards": [shard.identity_dict() for shard in sorted(self.shards, key=lambda s: s.index)],
        }


@dataclass(frozen=True)
class Manifest:
    version: str
    source_format: str
    revision: int
    logical_clock: int
    generation: int
    absolute_updated_ms: int
    raw_time: dict[str, Any]
    time_provenance: dict[str, Any]
    layers: tuple[Layer, ...]
    preprocess: str
    postprocess: str
    feature_tables: tuple[str, ...] = ()
    upstream_models: tuple[str, ...] = ()
    tombstone: bool = False
    canonical_id: str = ""
    patch_digest: str = ""
    shard_json_sha256: str = ""
    source_path: str = ""
    source_dir: str = field(default="", compare=False)
    applied_by: str = ""

    def shard_map(self) -> dict[tuple[str, str], Shard]:
        return {(shard.layer, shard.shard_id): shard for layer in self.layers for shard in layer.shards}

    def layer_map(self) -> dict[str, Layer]:
        return {layer.name: layer for layer in self.layers}

    def shard_chain(self) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        for layer in sorted(self.layers, key=lambda item: item.name):
            for shard in sorted(layer.shards, key=lambda item: item.index):
                chain.append(shard.identity_dict())
        return chain

    def layer_chain(self) -> list[dict[str, Any]]:
        return [
            layer.identity_dict()
            for layer in sorted(self.layers, key=lambda item: item.name)
        ]

    def directory_payload(self) -> dict[str, Any]:
        return {"version": self.version, "layers": self.layer_chain()}

    def directory_digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.directory_payload()))

    def time_interop(self) -> dict[str, Any]:
        canonical = canonical_json_bytes({"absolute_updated_ms": self.absolute_updated_ms})
        return {
            "absolute_updated_ms": self.absolute_updated_ms,
            "normalized_json": canonical.decode("utf-8"),
            "normalized_sha256": sha256_bytes(canonical),
            "provenance": self.time_provenance,
        }

    def core_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_format": self.source_format,
            "revision": self.revision,
            "logical_clock": self.logical_clock,
            "generation": self.generation,
            "absolute_updated_ms": self.absolute_updated_ms,
            "raw_time": self.raw_time,
            "time_provenance": self.time_provenance,
            "layers": self.layer_chain(),
            "preprocess": self.preprocess,
            "postprocess": self.postprocess,
            "feature_tables": list(self.feature_tables),
            "upstream_models": list(self.upstream_models),
            "tombstone": self.tombstone,
            "canonical_id": self.canonical_id,
            "patch_digest": self.patch_digest,
            "shard_json_sha256": self.shard_json_sha256,
            "applied_by": self.applied_by,
        }

    def view_dict(self) -> dict[str, Any]:
        payload = self.core_dict()
        payload.pop("time_provenance", None)
        payload.update(
            {
                "shard_chain": self.shard_chain(),
                "directory_digest": self.directory_digest(),
                "time_interop": self.time_interop(),
            }
        )
        return payload

    def storage_dict(self) -> dict[str, Any]:
        payload = self.core_dict()
        payload["source_path"] = self.source_path
        payload["source_dir"] = self.source_dir
        return payload

    @classmethod
    def from_storage_dict(cls, payload: dict[str, Any]) -> "Manifest":
        layers = tuple(
            Layer(
                name=layer["name"],
                parameters=layer["parameters"],
                shards=tuple(
                    Shard(
                        layer=layer["name"],
                        shard_id=shard["shard_id"],
                        index=shard["index"],
                        shard_count=shard["shard_count"],
                        sha256=shard["sha256"],
                        size=shard.get("size"),
                        path=shard.get("path"),
                        byte_start=shard.get("byte_start"),
                        byte_end=shard.get("byte_end"),
                    )
                    for shard in layer["shards"]
                ),
            )
            for layer in payload["layers"]
        )
        return cls(
            version=payload["version"],
            source_format=payload["source_format"],
            revision=payload["revision"],
            logical_clock=payload["logical_clock"],
            generation=payload["generation"],
            absolute_updated_ms=payload["absolute_updated_ms"],
            raw_time=payload.get("raw_time", {}),
            time_provenance=payload.get("time_provenance", {}),
            layers=layers,
            preprocess=payload["preprocess"],
            postprocess=payload["postprocess"],
            feature_tables=tuple(payload.get("feature_tables", [])),
            upstream_models=tuple(payload.get("upstream_models", [])),
            tombstone=bool(payload.get("tombstone", False)),
            canonical_id=payload.get("canonical_id", ""),
            patch_digest=payload.get("patch_digest", ""),
            shard_json_sha256=payload.get("shard_json_sha256", ""),
            source_path=payload.get("source_path", ""),
            source_dir=payload.get("source_dir", ""),
            applied_by=payload.get("applied_by", ""),
        )

    def with_overrides(self, **overrides: Any) -> "Manifest":
        values = self.storage_dict()
        values.update(overrides)
        return Manifest.from_storage_dict(values)


def parse_layers(raw_layers: Any, *, path: str) -> tuple[Layer, ...]:
    if not isinstance(raw_layers, list) or not raw_layers:
        fail("VALIDATION_ERROR", "a non-tombstone version must declare layers", path=path)
    layers: list[Layer] = []
    layer_names: set[str] = set()
    for layer_index, raw_layer in enumerate(raw_layers):
        layer_path = f"{path}.layers[{layer_index}]"
        if not isinstance(raw_layer, dict):
            fail("VALIDATION_ERROR", "layer must be an object", path=layer_path)
        name = _require_name(raw_layer.get("name"), "layer.name", path=layer_path)
        if name in layer_names:
            fail("VALIDATION_ERROR", "layer name is duplicated", path=layer_path, layer=name)
        layer_names.add(name)
        parameters = raw_layer.get("parameters")
        if not _is_int(parameters) or parameters <= 0:
            fail(
                "VALIDATION_ERROR",
                "layer parameters must be a positive integer",
                path=layer_path,
                layer=name,
                parameters=parameters,
            )
        raw_shards = raw_layer.get("shards")
        if not isinstance(raw_shards, list) or not raw_shards:
            fail(
                "SHARD_MISSING",
                "layer does not declare a complete shard set",
                path=layer_path,
                layer=name,
            )
        shard_count = raw_layer.get("shard_count", len(raw_shards))
        if not _is_int(shard_count) or shard_count != len(raw_shards):
            fail(
                "SHARD_MISSING",
                "declared shard_count does not match the shard set",
                path=layer_path,
                layer=name,
                expected=shard_count,
                actual=len(raw_shards),
            )
        shards: list[Shard] = []
        shard_ids: set[str] = set()
        indexes: set[int] = set()
        for shard_index, raw_shard in enumerate(raw_shards):
            shard_path = f"{layer_path}.shards[{shard_index}]"
            if not isinstance(raw_shard, dict):
                fail("VALIDATION_ERROR", "shard must be an object", path=shard_path)
            shard_id = _require_name(
                raw_shard.get("shard_id", raw_shard.get("id")),
                "shard.shard_id",
                path=shard_path,
            )
            if shard_id in shard_ids:
                fail(
                    "VALIDATION_ERROR",
                    "shard id is duplicated within a layer",
                    path=shard_path,
                    layer=name,
                    shard_id=shard_id,
                )
            shard_ids.add(shard_id)
            index = raw_shard.get("index", shard_index)
            if not _is_int(index) or index < 0:
                fail("VALIDATION_ERROR", "shard index must be a non-negative integer", path=shard_path)
            if index in indexes:
                fail("SHARD_MISSING", "shard indexes overlap", path=shard_path, index=index)
            indexes.add(index)
            declared_count = raw_shard.get("shard_count", shard_count)
            if not _is_int(declared_count) or declared_count != len(raw_shards):
                fail(
                    "SHARD_MISSING",
                    "shard count is inconsistent",
                    path=shard_path,
                    layer=name,
                    shard_id=shard_id,
                    expected=len(raw_shards),
                    actual=declared_count,
                )
            digest = raw_shard.get("sha256", raw_shard.get("digest"))
            if not isinstance(digest, str) or not SHA256_RE.match(digest):
                fail(
                    "VALIDATION_ERROR",
                    "shard sha256 must be 64 hexadecimal characters",
                    path=shard_path,
                    layer=name,
                    shard_id=shard_id,
                    sha256=digest,
                )
            size = raw_shard.get("size")
            if size is not None and (not _is_int(size) or size < 0):
                fail("VALIDATION_ERROR", "shard size must be a non-negative integer", path=shard_path)
            byte_start = raw_shard.get("byte_start")
            byte_end = raw_shard.get("byte_end")
            if (byte_start is None) != (byte_end is None):
                fail("VALIDATION_ERROR", "byte_start and byte_end must be provided together", path=shard_path)
            if byte_start is not None:
                if (
                    not _is_int(byte_start)
                    or not _is_int(byte_end)
                    or byte_start < 0
                    or byte_end <= byte_start
                ):
                    fail("VALIDATION_ERROR", "invalid half-open shard byte range", path=shard_path)
                if size is not None and byte_end > size:
                    fail(
                        "VALIDATION_ERROR",
                        "shard byte range exceeds shard size",
                        path=shard_path,
                        size=size,
                        byte_start=byte_start,
                        byte_end=byte_end,
                    )
            path_value = raw_shard.get("path")
            if path_value is not None and not isinstance(path_value, str):
                fail("VALIDATION_ERROR", "shard path must be a string", path=shard_path)
            shards.append(
                Shard(
                    layer=name,
                    shard_id=shard_id,
                    index=index,
                    shard_count=declared_count,
                    sha256=digest.lower(),
                    size=size,
                    path=path_value,
                    byte_start=byte_start,
                    byte_end=byte_end,
                )
            )
        if indexes != set(range(len(raw_shards))):
            fail(
                "SHARD_MISSING",
                "shards do not cover indexes 0..shard_count-1",
                path=layer_path,
                layer=name,
                indexes=sorted(indexes),
                expected_count=len(raw_shards),
            )
        layers.append(Layer(name=name, parameters=parameters, shards=tuple(shards)))
    return tuple(layers)


def validate_manifest_order(manifests: Iterable[Manifest], *, source: str) -> None:
    previous_revision: int | None = None
    previous_time: int | None = None
    for manifest in manifests:
        if previous_revision is not None and manifest.revision <= previous_revision:
            fail(
                "VERSION_ORDER_INVALID",
                "version revision must be strictly increasing within a source",
                source=source,
                version=manifest.version,
                previous_revision=previous_revision,
                revision=manifest.revision,
            )
        if previous_time is not None and manifest.absolute_updated_ms < previous_time:
            fail(
                "VERSION_ORDER_INVALID",
                "version update time must be monotonic within a source",
                source=source,
                version=manifest.version,
                previous_absolute_updated_ms=previous_time,
                absolute_updated_ms=manifest.absolute_updated_ms,
            )
        previous_revision = manifest.revision
        previous_time = manifest.absolute_updated_ms
