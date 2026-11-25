"""Manifest validation and two-path restore consistency checks."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .derivation import (
    CheckpointContent,
    LayerContent,
    decode_shard,
    derive_checkpoint,
    extract_layer_payloads,
    parameter_digest_for,
)
from .errors import (
    LayerShapeMismatchError,
    ManifestDigestMismatchError,
    ManifestValidationError,
    ParameterDigestMismatchError,
    RebuildMismatchError,
    ShardDigestMismatchError,
    ShardFormatError,
    ShardMissingError,
    StitchOrderMismatchError,
)
from .jsonio import digest_bytes
from .model import DTYPE_ITEMSIZE, Plan, digest_is_hex, parse_plan

MANIFEST_SCHEMA = "ckpt-manifest/v1"


@dataclass(frozen=True)
class VerifiedRestore:
    checkpoint: str
    version: int
    parameter_digest: str
    stitched_digest: str
    independent_digest: str
    layer_order: Tuple[str, ...]
    layers: Tuple[LayerContent, ...]
    shard_digests: Tuple[Tuple[str, str], ...]

    def summary(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "checkpoint": self.checkpoint,
            "version": self.version,
            "parameter_digest": self.parameter_digest,
            "stitched_digest": self.stitched_digest,
            "independent_digest": self.independent_digest,
            "stitched_equals_independent": (
                self.stitched_digest == self.independent_digest
            ),
            "layer_order": list(self.layer_order),
            "layers": [
                {
                    "name": layer.name,
                    "shape": list(layer.shape),
                    "dtype": layer.dtype,
                    "revision": layer.revision,
                    "nbytes": layer.nbytes,
                    "param_digest": layer.param_digest,
                    "payload_digest": digest_bytes(layer.payload),
                }
                for layer in self.layers
            ],
            "shards": [
                {"shard_id": shard_id, "digest": digest}
                for shard_id, digest in self.shard_digests
            ],
        }


def _manifest_error(message: str, **details: Any) -> ManifestValidationError:
    return ManifestValidationError(message, **details)


def validate_manifest_structure(manifest: Any) -> Dict[str, Any]:
    if not isinstance(manifest, dict):
        raise _manifest_error("checkpoint manifest must be an object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise _manifest_error(
            "unsupported checkpoint manifest schema", schema=manifest.get("schema")
        )
    checkpoint = manifest.get("checkpoint")
    if not isinstance(checkpoint, str) or not checkpoint:
        raise _manifest_error("checkpoint manifest requires a checkpoint name")
    version = manifest.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise _manifest_error("checkpoint version must be a positive integer")
    if not isinstance(manifest.get("global_seed"), int) or isinstance(
        manifest.get("global_seed"), bool
    ):
        raise _manifest_error("checkpoint manifest global_seed must be an integer")
    if not digest_is_hex(manifest.get("plan_digest")):
        raise _manifest_error("plan_digest must be a lowercase SHA-256 digest")
    if not digest_is_hex(manifest.get("parameter_digest")):
        raise _manifest_error("parameter_digest must be a lowercase SHA-256 digest")
    if not isinstance(manifest.get("plan"), dict):
        raise _manifest_error("checkpoint manifest must embed its normalized plan")

    try:
        plan = parse_plan(manifest["plan"])
    except ManifestValidationError:
        raise
    except Exception as exc:
        raise _manifest_error(
            "embedded training plan is invalid", reason=str(exc)
        ) from exc
    if plan.checkpoint != checkpoint:
        raise _manifest_error(
            "manifest checkpoint does not match embedded plan",
            manifest_checkpoint=checkpoint,
            plan_checkpoint=plan.checkpoint,
        )
    if plan.global_seed != manifest["global_seed"]:
        raise _manifest_error("manifest global seed does not match embedded plan")

    layers = manifest.get("layers")
    layer_order = manifest.get("layer_order")
    if not isinstance(layers, list) or not layers:
        raise _manifest_error("manifest layers must be a non-empty list")
    if not isinstance(layer_order, list) or len(layer_order) != len(layers):
        raise _manifest_error("manifest layer_order must cover every layer")
    if len(set(layer_order)) != len(layer_order):
        raise _manifest_error("manifest layer_order contains duplicates")
    seen_layers = set()
    shard_for_layer: Dict[str, str] = {}
    for index, entry in enumerate(layers):
        if not isinstance(entry, dict):
            raise _manifest_error("manifest layer entries must be objects")
        name = entry.get("name")
        if not isinstance(name, str) or not name or name != layer_order[index]:
            raise StitchOrderMismatchError(
                "manifest layer order is inconsistent",
                index=index,
                expected=layer_order[index],
                actual=name,
            )
        if name in seen_layers:
            raise _manifest_error("manifest contains a duplicate layer", layer=name)
        seen_layers.add(name)
        shape = entry.get("shape")
        if not isinstance(shape, list) or not shape:
            raise _manifest_error("manifest layer shape is invalid", layer=name)
        total = 1
        for dimension in shape:
            if (
                not isinstance(dimension, int)
                or isinstance(dimension, bool)
                or dimension <= 0
            ):
                raise _manifest_error(
                    "manifest shape dimensions must be positive integers", layer=name
                )
            total *= dimension
        dtype = entry.get("dtype")
        if dtype not in DTYPE_ITEMSIZE:
            raise _manifest_error("manifest layer dtype is invalid", layer=name)
        revision = entry.get("revision")
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise _manifest_error("manifest layer revision is invalid", layer=name)
        nbytes = entry.get("nbytes")
        if nbytes != total * DTYPE_ITEMSIZE[dtype]:
            raise _manifest_error(
                "manifest layer byte size does not match its shape and dtype",
                layer=name,
                expected=total * DTYPE_ITEMSIZE[dtype],
                actual=nbytes,
            )
        if not digest_is_hex(entry.get("param_digest")):
            raise _manifest_error("manifest layer digest is invalid", layer=name)
        shard_id = entry.get("shard_id")
        shard_index = entry.get("shard_index")
        if not isinstance(shard_id, str) or not isinstance(shard_index, int):
            raise _manifest_error("manifest layer shard assignment is invalid", layer=name)
        if name in shard_for_layer:
            raise _manifest_error("manifest layer assigned more than once", layer=name)
        shard_for_layer[name] = shard_id

    shards = manifest.get("shards")
    shard_order = manifest.get("shard_order")
    if not isinstance(shards, list) or not shards:
        raise _manifest_error("manifest shards must be a non-empty list")
    if not isinstance(shard_order, list) or len(shard_order) != len(shards):
        raise _manifest_error("manifest shard_order must cover every shard")
    if len(set(shard_order)) != len(shard_order):
        raise _manifest_error("manifest shard_order contains duplicates")
    seen_shards = set()
    for shard_index, entry in enumerate(shards):
        if not isinstance(entry, dict):
            raise _manifest_error("manifest shard entries must be objects")
        shard_id = entry.get("shard_id")
        if not isinstance(shard_id, str) or not shard_id:
            raise _manifest_error("manifest shard_id is invalid")
        if shard_id in seen_shards:
            raise _manifest_error("manifest contains a duplicate shard", shard_id=shard_id)
        seen_shards.add(shard_id)
        if shard_order[shard_index] != shard_id:
            raise StitchOrderMismatchError(
                "manifest shard order does not match the shard table",
                index=shard_index,
                expected=shard_order[shard_index],
                actual=shard_id,
            )
        if entry.get("shard_index") != shard_index:
            raise StitchOrderMismatchError(
                "manifest shard index is not contiguous",
                shard_id=shard_id,
                expected=shard_index,
                actual=entry.get("shard_index"),
            )
        if not digest_is_hex(entry.get("digest")):
            raise _manifest_error("manifest shard digest is invalid", shard_id=shard_id)
        size_bytes = entry.get("size_bytes")
        if not isinstance(size_bytes, int) or size_bytes <= 0:
            raise _manifest_error("manifest shard size is invalid", shard_id=shard_id)
        names = entry.get("layers", entry.get("layer_names"))
        if not isinstance(names, list) or not names:
            raise _manifest_error(
                "manifest shard must contain at least one layer", shard_id=shard_id
            )
        for name in names:
            if shard_for_layer.get(name) != shard_id:
                raise StitchOrderMismatchError(
                    "manifest shard/layer assignment is inconsistent",
                    shard_id=shard_id,
                    layer=name,
                )
        reuse = entry.get("reuse")
        if reuse is not None and not isinstance(reuse, dict):
            raise _manifest_error("manifest reuse record must be an object", shard_id=shard_id)

    if set(shard_for_layer) != seen_layers:
        raise _manifest_error("not every manifest layer is assigned to a shard")
    if seen_shards != set(shard_for_layer.values()):
        raise _manifest_error("manifest has an empty or unreferenced shard")
    return manifest


def _expected_manifest_and_content(
    manifest: Mapping[str, Any],
) -> Tuple[Plan, CheckpointContent]:
    plan = parse_plan(manifest["plan"])
    if plan.digest() != manifest["plan_digest"]:
        raise ManifestDigestMismatchError(
            "embedded plan digest does not match plan_digest",
            expected=plan.digest(),
            actual=manifest["plan_digest"],
        )
    content = derive_checkpoint(plan, int(manifest["version"]))
    return plan, content


def _validated_expected(
    manifest: Mapping[str, Any],
) -> Tuple[Plan, CheckpointContent]:
    plan, expected = _expected_manifest_and_content(manifest)
    layer_order = [str(name) for name in manifest["layer_order"]]
    expected_order = list(expected.layer_order)
    if layer_order != expected_order:
        raise StitchOrderMismatchError(
            "manifest layer order differs from the embedded plan",
            expected=expected_order,
            actual=layer_order,
        )
    if manifest["parameter_digest"] != expected.parameter_digest:
        raise ParameterDigestMismatchError(
            "manifest parameter digest differs from independent reconstruction",
            expected=expected.parameter_digest,
            actual=manifest["parameter_digest"],
        )
    manifest_shards = manifest["shards"]
    if [item["shard_id"] for item in manifest_shards] != [
        item.shard_id for item in expected.shards
    ]:
        raise StitchOrderMismatchError(
            "manifest shard order differs from deterministic shard derivation",
            expected=[item.shard_id for item in expected.shards],
            actual=[item["shard_id"] for item in manifest_shards],
        )
    return plan, expected


def validate_manifest_semantics(manifest: Dict[str, Any]) -> Dict[str, Any]:
    validate_manifest_structure(manifest)
    _validated_expected(manifest)
    return manifest


def verify_checkpoint_manifest(
    root: Path,
    manifest: Dict[str, Any],
    shard_rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> VerifiedRestore:
    validate_manifest_structure(manifest)
    plan, expected = _validated_expected(manifest)
    checkpoint = str(manifest["checkpoint"])
    version = int(manifest["version"])
    expected_order = list(expected.layer_order)
    manifest_shards = manifest["shards"]

    row_by_shard: Dict[str, Mapping[str, Any]] = {}
    if shard_rows is not None:
        for row in shard_rows:
            row_by_shard[str(row["shard_id"])] = row
        if set(row_by_shard) != {item.shard_id for item in expected.shards}:
            raise StitchOrderMismatchError(
                "ledger shard set differs from manifest",
                expected=sorted(item.shard_id for item in expected.shards),
                actual=sorted(row_by_shard),
            )

    actual_layers: Dict[str, bytes] = {}
    shard_digests: List[Tuple[str, str]] = []
    for expected_shard, manifest_shard in zip(expected.shards, manifest_shards):
        shard_id = expected_shard.shard_id
        if list(manifest_shard.get("layers", manifest_shard.get("layer_names", []))) != list(
            expected_shard.layer_names
        ):
            raise StitchOrderMismatchError(
                "manifest shard layer order is inconsistent",
                shard_id=shard_id,
                expected=list(expected_shard.layer_names),
                actual=manifest_shard.get("layers", manifest_shard.get("layer_names")),
            )
        if manifest_shard["digest"] != expected_shard.digest:
            raise ManifestDigestMismatchError(
                "manifest shard digest differs from deterministic derivation",
                shard_id=shard_id,
                expected=expected_shard.digest,
                actual=manifest_shard["digest"],
            )
        if row_by_shard:
            row = row_by_shard[shard_id]
            if int(row["shard_index"]) != expected_shard.shard_index:
                raise StitchOrderMismatchError(
                    "ledger shard index is inconsistent",
                    shard_id=shard_id,
                    expected=expected_shard.shard_index,
                    actual=row["shard_index"],
                )
            if row["digest"] != manifest_shard["digest"]:
                raise ShardDigestMismatchError(
                    "ledger shard digest differs from manifest",
                    shard_id=shard_id,
                    expected=manifest_shard["digest"],
                    actual=row["digest"],
                )
            relpath = row.get("object_relpath") or "objects/{0}/{1}.shard".format(
                expected_shard.digest[:2], expected_shard.digest
            )
        else:
            relpath = "objects/{0}/{1}.shard".format(
                expected_shard.digest[:2], expected_shard.digest
            )
        shard_path = root / relpath
        if not shard_path.exists():
            raise ShardMissingError(
                "shard object file is missing",
                checkpoint=checkpoint,
                version=version,
                shard_id=shard_id,
                path=str(shard_path),
            )
        file_bytes = shard_path.read_bytes()
        actual_file_digest = digest_bytes(file_bytes)
        if actual_file_digest != expected_shard.digest:
            raise ShardDigestMismatchError(
                "shard object content digest does not match the manifest",
                checkpoint=checkpoint,
                version=version,
                shard_id=shard_id,
                path=str(shard_path),
                expected=expected_shard.digest,
                actual=actual_file_digest,
            )
        if file_bytes != expected_shard.file_bytes:
            raise RebuildMismatchError(
                "shard bytes differ from independent full regeneration",
                shard_id=shard_id,
            )
        try:
            header, payload = decode_shard(file_bytes)
        except ShardFormatError:
            raise
        header_names = [record.get("name") for record in header.get("layers", [])]
        if header_names != list(expected_shard.layer_names):
            raise StitchOrderMismatchError(
                "shard header layer order is inconsistent",
                shard_id=shard_id,
                expected=list(expected_shard.layer_names),
                actual=header_names,
            )
        extracted = extract_layer_payloads(header, payload, shard_id)
        for layer in expected.layers:
            if layer.name not in expected_shard.layer_names:
                continue
            record = next(
                item for item in header["layers"] if item.get("name") == layer.name
            )
            if record.get("shape") != list(layer.shape) or record.get("dtype") != layer.dtype:
                raise LayerShapeMismatchError(
                    "shard layer shape or dtype does not match the manifest",
                    shard_id=shard_id,
                    layer=layer.name,
                    expected_shape=list(layer.shape),
                    actual_shape=record.get("shape"),
                    expected_dtype=layer.dtype,
                    actual_dtype=record.get("dtype"),
                )
            actual_payload = extracted[layer.name]
            if len(actual_payload) != layer.nbytes:
                raise LayerShapeMismatchError(
                    "shard layer payload size does not match its shape",
                    shard_id=shard_id,
                    layer=layer.name,
                    expected=layer.nbytes,
                    actual=len(actual_payload),
                )
            actual_digest = _payload_param_digest(
                plan.global_seed, layer, actual_payload
            )
            if actual_digest != layer.param_digest:
                raise ParameterDigestMismatchError(
                    "stitched layer payload digest does not match the parameter digest",
                    shard_id=shard_id,
                    layer=layer.name,
                    expected=layer.param_digest,
                    actual=actual_digest,
                )
            actual_layers[layer.name] = actual_payload
        shard_digests.append((shard_id, actual_file_digest))

    missing_layers = [name for name in expected_order if name not in actual_layers]
    if missing_layers:
        raise RebuildMismatchError(
            "stitched result is missing layers", layers=missing_layers
        )
    stitched = b"".join(actual_layers[name] for name in expected_order)
    independent = b"".join(layer.payload for layer in expected.layers)
    stitched_digest = digest_bytes(stitched)
    independent_digest = digest_bytes(independent)
    if stitched != independent:
        raise RebuildMismatchError(
            "stitched shard result differs from per-layer independent reconstruction",
            stitched_digest=stitched_digest,
            independent_digest=independent_digest,
        )

    actual_metadata_layers = tuple(
        LayerContent(
            name=layer.name,
            shape=layer.shape,
            dtype=layer.dtype,
            revision=layer.revision,
            nbytes=layer.nbytes,
            param_digest=_payload_param_digest(
                plan.global_seed, layer, actual_layers[layer.name]
            ),
            payload=actual_layers[layer.name],
        )
        for layer in expected.layers
    )
    actual_parameter_digest = parameter_digest_for(
        plan.global_seed, version, actual_metadata_layers
    )
    if actual_parameter_digest != expected.parameter_digest:
        raise ParameterDigestMismatchError(
            "stitched checkpoint parameter digest differs from independent reconstruction",
            expected=expected.parameter_digest,
            actual=actual_parameter_digest,
        )

    return VerifiedRestore(
        checkpoint=checkpoint,
        version=version,
        parameter_digest=actual_parameter_digest,
        stitched_digest=stitched_digest,
        independent_digest=independent_digest,
        layer_order=tuple(expected_order),
        layers=actual_metadata_layers,
        shard_digests=tuple(shard_digests),
    )


def _payload_param_digest(global_seed: int, layer: LayerContent, payload: bytes) -> str:
    from .jsonio import canonical_bytes

    metadata = {
        "domain": "ckpt-parameter/v1",
        "global_seed": global_seed,
        "name": layer.name,
        "shape": list(layer.shape),
        "dtype": layer.dtype,
        "revision": layer.revision,
    }
    digest_input = hashlib.sha256()
    digest_input.update(b"ckpt-parameter-digest/v1\0")
    digest_input.update(canonical_bytes(metadata))
    digest_input.update(b"\0")
    digest_input.update(payload)
    return digest_input.hexdigest()
