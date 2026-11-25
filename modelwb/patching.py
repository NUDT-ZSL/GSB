from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, read_json, sha256_bytes
from .domain import SHA256_RE, Layer, Manifest, Shard
from .errors import fail


@dataclass(frozen=True)
class ShardChange:
    layer: str
    shard_id: str
    sha256: str
    byte_start: int | None = None
    byte_end: int | None = None
    range_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "shard_id": self.shard_id,
            "sha256": self.sha256,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "range_sha256": self.range_sha256,
        }


@dataclass(frozen=True)
class PatchSpec:
    patch_id: str
    patch_sha256: str
    base_version: str
    target_version: str
    logical_clock: int | None
    generation: int | None
    changes: tuple[ShardChange, ...]
    preprocess: str | None
    postprocess: str | None
    feature_tables: tuple[str, ...] | None
    upstream_models: tuple[str, ...] | None
    payload: dict[str, Any]


@dataclass
class PatchComputation:
    spec: PatchSpec
    base: Manifest
    target: Manifest
    full_replacement: Manifest
    computed_directory_digest: str
    full_directory_digest: str
    changed_shards: list[dict[str, Any]]
    unchanged_shard_count: int
    applied_ranges: list[dict[str, Any]]

    def summary(self) -> dict[str, Any]:
        return {
            "patch_id": self.spec.patch_id,
            "patch_sha256": self.spec.patch_sha256,
            "base_version": self.base.version,
            "target_version": self.target.version,
            "target_directory_digest": self.computed_directory_digest,
            "full_replacement_directory_digest": self.full_directory_digest,
            "matches_full_replacement": self.computed_directory_digest == self.full_directory_digest,
            "changed_shards": self.changed_shards,
            "unchanged_shards": self.unchanged_shard_count,
            "applied_ranges": self.applied_ranges,
            "shard_chain": self.target.shard_chain(),
        }


def parse_patch(path: str | Path) -> PatchSpec:
    path = Path(path).resolve()
    try:
        raw_bytes = path.read_bytes()
        data = read_json(path)
    except (OSError, ValueError) as exc:
        fail("IO_ERROR", "cannot parse patch file", path=str(path), detail=str(exc))
    if not isinstance(data, dict):
        fail("VALIDATION_ERROR", "patch must be a JSON object", path=str(path))
    declared_digest = data.get("patch_sha256")
    digest_body = {key: value for key, value in data.items() if key != "patch_sha256"}
    computed_digest = sha256_bytes(canonical_json_bytes(digest_body))
    if declared_digest is not None and declared_digest != computed_digest:
        fail(
            "PATCH_DIGEST_MISMATCH",
            "patch digest does not match canonical patch body",
            path=str(path),
            expected=declared_digest,
            actual=computed_digest,
        )
    base_version = data.get("base_version")
    target_version = data.get("target_version", data.get("version"))
    if not isinstance(base_version, str) or not base_version:
        fail("VALIDATION_ERROR", "patch base_version is required", path=str(path))
    if not isinstance(target_version, str) or not target_version:
        fail("VALIDATION_ERROR", "patch target_version is required", path=str(path))
    changes = _parse_changes(data, path)
    logical_clock = data.get("logical_clock")
    generation = data.get("generation")
    if logical_clock is not None and (
        not isinstance(logical_clock, int) or isinstance(logical_clock, bool) or logical_clock < 0
    ):
        fail("VALIDATION_ERROR", "patch logical_clock must be a non-negative integer", path=str(path))
    if generation is not None and (
        not isinstance(generation, int) or isinstance(generation, bool) or generation < 0
    ):
        fail("VALIDATION_ERROR", "patch generation must be a non-negative integer", path=str(path))
    dependencies = data.get("dependencies")
    feature_tables: tuple[str, ...] | None = None
    upstream_models: tuple[str, ...] | None = None
    if dependencies is not None:
        if not isinstance(dependencies, dict):
            fail("VALIDATION_ERROR", "patch dependencies must be an object", path=str(path))
        from .domain import normalise_dependencies

        normalized = normalise_dependencies(dependencies, path=str(path))
        feature_tables = normalized["feature_tables"]
        upstream_models = normalized["upstream_models"]
    preprocess = data.get("preprocess")
    postprocess = data.get("postprocess")
    if preprocess is not None and not isinstance(preprocess, str):
        fail("VALIDATION_ERROR", "patch preprocess must be a string", path=str(path))
    if postprocess is not None and not isinstance(postprocess, str):
        fail("VALIDATION_ERROR", "patch postprocess must be a string", path=str(path))
    patch_id = data.get("patch_id") or f"patch-{computed_digest[:16]}"
    if not isinstance(patch_id, str):
        fail("VALIDATION_ERROR", "patch_id must be a string", path=str(path))
    return PatchSpec(
        patch_id=patch_id,
        patch_sha256=declared_digest or computed_digest,
        base_version=base_version,
        target_version=target_version,
        logical_clock=logical_clock,
        generation=generation,
        changes=tuple(changes),
        preprocess=preprocess,
        postprocess=postprocess,
        feature_tables=feature_tables,
        upstream_models=upstream_models,
        payload=data,
    )


def _parse_changes(data: dict[str, Any], path: Path) -> list[ShardChange]:
    raw_changes = data.get("changes")
    entries: list[Any] = []
    if raw_changes is not None:
        if isinstance(raw_changes, dict):
            for layer, values in raw_changes.items():
                if not isinstance(values, list):
                    fail("VALIDATION_ERROR", "patch change layer map values must be lists", path=str(path))
                for value in values:
                    item = dict(value) if isinstance(value, dict) else {}
                    item.setdefault("layer", layer)
                    entries.append(item)
        elif isinstance(raw_changes, list):
            entries = list(raw_changes)
        else:
            fail("VALIDATION_ERROR", "patch changes must be a list or object", path=str(path))
    elif isinstance(data.get("layers"), dict):
        for layer, values in data["layers"].items():
            if not isinstance(values, list):
                fail("VALIDATION_ERROR", "patch layers map values must be lists", path=str(path))
            for value in values:
                item = dict(value) if isinstance(value, dict) else {}
                item.setdefault("layer", layer)
                entries.append(item)
    if not entries:
        fail("VALIDATION_ERROR", "patch must declare at least one changed shard", path=str(path))
    changes: list[ShardChange] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            fail("VALIDATION_ERROR", "patch change must be an object", index=index)
        layer = entry.get("layer")
        shard_id = entry.get("shard_id", entry.get("id"))
        digest = entry.get("sha256", entry.get("new_sha256"))
        if not isinstance(layer, str) or not layer:
            fail("VALIDATION_ERROR", "patch change layer is required", index=index)
        if not isinstance(shard_id, str) or not shard_id:
            fail("VALIDATION_ERROR", "patch change shard_id is required", index=index)
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            fail(
                "VALIDATION_ERROR",
                "patch change sha256 must be 64 hexadecimal characters",
                layer=layer,
                shard_id=shard_id,
            )
        if (layer, shard_id) in seen:
            fail(
                "VALIDATION_ERROR",
                "patch declares the same shard more than once",
                layer=layer,
                shard_id=shard_id,
            )
        seen.add((layer, shard_id))
        byte_start = entry.get("byte_start")
        byte_end = entry.get("byte_end")
        if (byte_start is None) != (byte_end is None):
            fail(
                "VALIDATION_ERROR",
                "patch byte_start and byte_end must be provided together",
                layer=layer,
                shard_id=shard_id,
            )
        if byte_start is not None and (
            not isinstance(byte_start, int)
            or isinstance(byte_start, bool)
            or not isinstance(byte_end, int)
            or isinstance(byte_end, bool)
            or byte_start < 0
            or byte_end <= byte_start
        ):
            fail(
                "VALIDATION_ERROR",
                "patch byte range must be a valid half-open interval",
                layer=layer,
                shard_id=shard_id,
            )
        range_digest = entry.get("range_sha256")
        if range_digest is not None and (
            not isinstance(range_digest, str) or not SHA256_RE.match(range_digest)
        ):
            fail("VALIDATION_ERROR", "range_sha256 must be 64 hexadecimal characters")
        changes.append(
            ShardChange(
                layer=layer,
                shard_id=shard_id,
                sha256=digest.lower(),
                byte_start=byte_start,
                byte_end=byte_end,
                range_sha256=range_digest.lower() if isinstance(range_digest, str) else None,
            )
        )
    return changes


def apply_patch(
    base: Manifest,
    full_replacement: Manifest,
    spec: PatchSpec,
    *,
    target_artifact_manifest: Manifest | None = None,
) -> PatchComputation:
    if base.version != spec.base_version:
        fail(
            "VALIDATION_ERROR",
            "patch base_version does not match the selected baseline",
            expected=spec.base_version,
            actual=base.version,
        )
    if full_replacement.version != spec.target_version:
        fail(
            "VALIDATION_ERROR",
            "full replacement version does not match patch target_version",
            expected=spec.target_version,
            actual=full_replacement.version,
        )
    _validate_patch_metadata(spec, full_replacement)
    base_layers = base.layer_map()
    full_layers = full_replacement.layer_map()
    if set(base_layers) != set(full_layers):
        first = _first_layer_difference(base, full_replacement)
        fail(
            "LAYER_REFERENCE_MISSING",
            "base and full replacement declare different layer sets",
            first_divergence=first,
        )
    change_by_key = {(change.layer, change.shard_id): change for change in spec.changes}
    for change in spec.changes:
        if change.layer not in base_layers:
            fail(
                "LAYER_REFERENCE_MISSING",
                "patch references a layer missing from the baseline",
                layer=change.layer,
                shard_id=change.shard_id,
            )
        if change.sha256 != _full_shard_digest(full_layers[change.layer], change.shard_id):
            actual = _full_shard_digest(full_layers[change.layer], change.shard_id)
            fail(
                "SHARD_DIGEST_MISMATCH",
                "patch digest does not match the full replacement shard",
                first_divergence={
                    "layer": change.layer,
                    "shard_id": change.shard_id,
                    "field": "sha256",
                    "computed": change.sha256,
                    "full_replacement": actual,
                },
                layer=change.layer,
                shard_id=change.shard_id,
                patch_sha256=change.sha256,
                full_replacement_sha256=actual,
            )
    for layer_name in sorted(base_layers):
        base_layer = base_layers[layer_name]
        full_layer = full_layers[layer_name]
        base_shards = {shard.shard_id: shard for shard in base_layer.shards}
        full_shards = {shard.shard_id: shard for shard in full_layer.shards}
        if set(base_shards) != set(full_shards):
            divergence = _first_shard_difference(base_layer, full_layer)
            fail(
                "PATCH_FULL_REPLACEMENT_MISMATCH",
                "shard set changed; incremental patches cannot add or remove shards",
                first_divergence=divergence,
            )
        for shard_id in sorted(base_shards):
            base_shard = base_shards[shard_id]
            full_shard = full_shards[shard_id]
            if (
                base_shard.sha256 != full_shard.sha256
                or base_shard.size != full_shard.size
                or base_shard.index != full_shard.index
                or base_shard.shard_count != full_shard.shard_count
            ) and (layer_name, shard_id) not in change_by_key:
                fail(
                    "PATCH_UNDECLARED_SHARD_CHANGE",
                    "full replacement changes a shard not declared by the patch",
                    first_divergence={
                        "layer": layer_name,
                        "shard_id": shard_id,
                        "field": "sha256" if base_shard.sha256 != full_shard.sha256 else "metadata",
                        "baseline": base_shard.identity_dict(),
                        "full_replacement": full_shard.identity_dict(),
                    },
                    layer=layer_name,
                    shard_id=shard_id,
                )
    computed_layers: list[Layer] = []
    changed_details: list[dict[str, Any]] = []
    unchanged_count = 0
    for layer in base.layers:
        shards: list[Shard] = []
        for shard in layer.shards:
            change = change_by_key.get((layer.name, shard.shard_id))
            if change is None:
                shards.append(shard)
                unchanged_count += 1
            else:
                shards.append(
                    Shard(
                        layer=shard.layer,
                        shard_id=shard.shard_id,
                        index=shard.index,
                        shard_count=shard.shard_count,
                        sha256=change.sha256,
                        size=shard.size,
                        path=shard.path,
                        byte_start=shard.byte_start,
                        byte_end=shard.byte_end,
                    )
                )
                full_shard = full_layers[layer.name].shards[shard.index]
                changed_details.append(
                    {
                        "layer": layer.name,
                        "shard_id": shard.shard_id,
                        "old_sha256": shard.sha256,
                        "new_sha256": change.sha256,
                        "byte_start": change.byte_start,
                        "byte_end": change.byte_end,
                        "full_size": full_shard.size,
                    }
                )
        computed_layers.append(Layer(name=layer.name, parameters=layer.parameters, shards=tuple(shards)))
    computed = Manifest(
        version=full_replacement.version,
        source_format="patch",
        revision=full_replacement.revision,
        logical_clock=spec.logical_clock if spec.logical_clock is not None else full_replacement.logical_clock,
        generation=spec.generation if spec.generation is not None else full_replacement.generation,
        absolute_updated_ms=full_replacement.absolute_updated_ms,
        raw_time=full_replacement.raw_time,
        time_provenance=full_replacement.time_provenance,
        layers=tuple(computed_layers),
        preprocess=spec.preprocess or full_replacement.preprocess,
        postprocess=spec.postprocess or full_replacement.postprocess,
        feature_tables=spec.feature_tables
        if spec.feature_tables is not None
        else full_replacement.feature_tables,
        upstream_models=spec.upstream_models
        if spec.upstream_models is not None
        else full_replacement.upstream_models,
        canonical_id=full_replacement.canonical_id,
        patch_digest=spec.patch_sha256,
        source_path=base.source_path,
        source_dir=base.source_dir,
        applied_by=spec.patch_id,
    )
    divergence = first_directory_divergence(computed, full_replacement)
    if divergence is not None:
        fail(
            "PATCH_FULL_REPLACEMENT_MISMATCH",
            "incremental patch result differs from full replacement",
            first_divergence=divergence,
            computed_directory_digest=computed.directory_digest(),
            full_replacement_directory_digest=full_replacement.directory_digest(),
        )
    applied_ranges = _validate_ranges(spec, target_artifact_manifest or full_replacement)
    return PatchComputation(
        spec=spec,
        base=base,
        target=computed,
        full_replacement=full_replacement,
        computed_directory_digest=computed.directory_digest(),
        full_directory_digest=full_replacement.directory_digest(),
        changed_shards=changed_details,
        unchanged_shard_count=unchanged_count,
        applied_ranges=applied_ranges,
    )


def _full_shard_digest(layer: Layer, shard_id: str) -> str | None:
    for shard in layer.shards:
        if shard.shard_id == shard_id:
            return shard.sha256
    fail(
        "SHARD_MISSING",
        "patch references a shard missing from the full replacement",
        layer=layer.name,
        shard_id=shard_id,
    )


def _validate_patch_metadata(spec: PatchSpec, target: Manifest) -> None:
    checks = (
        ("logical_clock", spec.logical_clock, target.logical_clock),
        ("generation", spec.generation, target.generation),
        ("preprocess", spec.preprocess, target.preprocess),
        ("postprocess", spec.postprocess, target.postprocess),
        (
            "feature_tables",
            tuple(spec.feature_tables) if spec.feature_tables is not None else None,
            target.feature_tables,
        ),
        (
            "upstream_models",
            tuple(spec.upstream_models) if spec.upstream_models is not None else None,
            target.upstream_models,
        ),
    )
    for field, patched, expected in checks:
        if patched is not None and patched != expected:
            fail(
                "PATCH_FULL_REPLACEMENT_MISMATCH",
                "patch metadata differs from the full replacement",
                first_divergence={
                    "field": f"metadata.{field}",
                    "patch": patched,
                    "full_replacement": expected,
                },
            )


def _validate_ranges(spec: PatchSpec, artifact_manifest: Manifest) -> list[dict[str, Any]]:
    layer_map = artifact_manifest.layer_map()
    result: list[dict[str, Any]] = []
    for change in spec.changes:
        if change.byte_start is None:
            continue
        layer = layer_map.get(change.layer)
        if layer is None:
            fail("LAYER_REFERENCE_MISSING", "patch range references a missing layer", layer=change.layer)
        shard = next((item for item in layer.shards if item.shard_id == change.shard_id), None)
        if shard is None:
            fail(
                "SHARD_MISSING",
                "patch range references a missing shard",
                layer=change.layer,
                shard_id=change.shard_id,
            )
        if shard.size is not None and change.byte_end > shard.size:
            fail(
                "SHARD_DIGEST_MISMATCH",
                "patch byte range exceeds the full replacement shard size",
                layer=change.layer,
                shard_id=change.shard_id,
                size=shard.size,
                byte_end=change.byte_end,
            )
        detail = {
            "layer": change.layer,
            "shard_id": change.shard_id,
            "byte_start": change.byte_start,
            "byte_end": change.byte_end,
            "range_sha256": change.range_sha256,
        }
        if change.range_sha256 is not None:
            path = shard.path
            if path:
                shard_path = Path(artifact_manifest.source_dir) / path
                if not shard_path.exists():
                    fail(
                        "SHARD_MISSING",
                        "cannot verify patch byte range because the target shard file is missing",
                        layer=change.layer,
                        shard_id=change.shard_id,
                        path=str(shard_path),
                    )
                with shard_path.open("rb") as handle:
                    handle.seek(change.byte_start)
                    payload = handle.read(change.byte_end - change.byte_start)
                actual = hashlib.sha256(payload).hexdigest()
                if actual != change.range_sha256:
                    fail(
                        "SHARD_DIGEST_MISMATCH",
                        "patch byte range digest does not match the full replacement bytes",
                        layer=change.layer,
                        shard_id=change.shard_id,
                        expected=change.range_sha256,
                        actual=actual,
                        byte_start=change.byte_start,
                        byte_end=change.byte_end,
                    )
                detail["verified"] = True
        result.append(detail)
    return result


def _first_layer_difference(left: Manifest, right: Manifest) -> dict[str, Any]:
    left_names = {layer.name for layer in left.layers}
    right_names = {layer.name for layer in right.layers}
    if left_names != right_names:
        missing_right = sorted(left_names - right_names)
        missing_left = sorted(right_names - left_names)
        return {
            "layer": (missing_right or missing_left)[0],
            "field": "layer_set",
            "incremental_only": missing_right,
            "full_replacement_only": missing_left,
        }
    return {}


def _first_shard_difference(left: Layer, right: Layer) -> dict[str, Any]:
    left_ids = {shard.shard_id for shard in left.shards}
    right_ids = {shard.shard_id for shard in right.shards}
    missing_right = sorted(left_ids - right_ids)
    missing_left = sorted(right_ids - left_ids)
    return {
        "layer": left.name,
        "shard_id": (missing_right or missing_left or [None])[0],
        "field": "shard_set",
        "incremental_only": missing_right,
        "full_replacement_only": missing_left,
    }


def first_directory_divergence(left: Manifest, right: Manifest) -> dict[str, Any] | None:
    left_layers = left.layer_map()
    right_layers = right.layer_map()
    ordered_names = list(left_layers) + sorted(set(right_layers) - set(left_layers))
    for layer_name in ordered_names:
        left_layer = left_layers.get(layer_name)
        right_layer = right_layers.get(layer_name)
        if left_layer is None or right_layer is None:
            return {
                "layer": layer_name,
                "field": "layer_presence",
                "incremental": left_layer is not None,
                "full_replacement": right_layer is not None,
            }
        if left_layer.parameters != right_layer.parameters:
            return {
                "layer": layer_name,
                "field": "parameters",
                "incremental": left_layer.parameters,
                "full_replacement": right_layer.parameters,
            }
        left_shards = {shard.shard_id: shard for shard in left_layer.shards}
        right_shards = {shard.shard_id: shard for shard in right_layer.shards}
        ordered_shards = sorted(
            left_shards,
            key=lambda shard_id: (left_shards[shard_id].index, shard_id),
        ) + sorted(set(right_shards) - set(left_shards))
        for shard_id in ordered_shards:
            left_shard = left_shards.get(shard_id)
            right_shard = right_shards.get(shard_id)
            if left_shard is None or right_shard is None:
                return {
                    "layer": layer_name,
                    "shard_id": shard_id,
                    "field": "shard_presence",
                    "incremental": left_shard is not None,
                    "full_replacement": right_shard is not None,
                }
            if left_shard.sha256 != right_shard.sha256:
                return {
                    "layer": layer_name,
                    "shard_id": shard_id,
                    "field": "sha256",
                    "incremental": left_shard.sha256,
                    "full_replacement": right_shard.sha256,
                }
            for field in ("index", "shard_count", "size", "byte_start", "byte_end"):
                if getattr(left_shard, field) != getattr(right_shard, field):
                    return {
                        "layer": layer_name,
                        "shard_id": shard_id,
                        "field": field,
                        "incremental": getattr(left_shard, field),
                        "full_replacement": getattr(right_shard, field),
                    }
    return None


def check_manifest_artifacts(manifest: Manifest) -> dict[str, Any]:
    checked = 0
    for layer in manifest.layers:
        for shard in layer.shards:
            if not shard.path:
                continue
            shard_path = Path(manifest.source_dir) / shard.path
            if not shard_path.exists():
                fail(
                    "SHARD_MISSING",
                    "declared shard artifact is missing",
                    version=manifest.version,
                    layer=layer.name,
                    shard_id=shard.shard_id,
                    path=str(shard_path),
                )
            digest = hashlib.sha256()
            size = 0
            with shard_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != shard.sha256:
                fail(
                    "SHARD_DIGEST_MISMATCH",
                    "declared shard artifact digest does not match",
                    version=manifest.version,
                    layer=layer.name,
                    shard_id=shard.shard_id,
                    path=str(shard_path),
                    expected=shard.sha256,
                    actual=actual,
                )
            if shard.size is not None and size != shard.size:
                fail(
                    "SHARD_DIGEST_MISMATCH",
                    "declared shard artifact size does not match",
                    version=manifest.version,
                    layer=layer.name,
                    shard_id=shard.shard_id,
                    path=str(shard_path),
                    expected=shard.size,
                    actual=size,
                )
            checked += 1
    return {
        "version": manifest.version,
        "directory_digest": manifest.directory_digest(),
        "checked_shards": checked,
        "healthy": True,
    }
