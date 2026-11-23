"""Incremental repair engine with targeted invalidation and cache reuse."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .cache import ArtifactCache, CacheRecord, cache_key
from .config import (
    merge_config,
    normalize_config,
    style_snapshot_payload,
    style_projection_digest,
)
from .errors import BusinessError, ErrorCode
from .graph import (
    NODE_BLEND_OUTPUT,
    NODE_CONTENT_COMPLETION,
    NODE_CONTEXT_SAMPLE,
    NODE_MASK_PARSE,
    NODE_ORDER,
    NODE_STYLE_TRANSFER,
    GraphSpec,
)
from .hashing import array_content_hash, first_difference, product_hash, sha256_json
from .mask import (
    MaskState,
    Region,
    coerce_mask_state,
    context_bbox,
    estimate_resident_pixels,
    validate_mask_state,
)
from .nodes import (
    assert_blend_consistency,
    blend_deviation_spectrum,
    blend_region,
    compose_global,
    compose_regionwise,
    content_completion,
    context_sample,
    mask_crop,
    style_transfer,
)
from .snapshots import SnapshotStore, snapshot_checksum

ALGORITHM_VERSION = "repairflow-algorithms-2026.1"


@dataclass(frozen=True)
class UpdateResult:
    image: np.ndarray
    report: dict[str, Any]
    mask_state: MaskState
    style_config: dict[str, Any]


@dataclass(frozen=True)
class FullRecomputeResult:
    image: np.ndarray
    node_records: dict[int, dict[str, CacheRecord]]
    arrays: dict[int, dict[str, np.ndarray]]
    blend_check: dict[str, Any]
    mask_state: MaskState
    style_config: dict[str, Any]


class RepairEngine:
    """A single-image stage with non-blocking incremental submissions."""

    def __init__(
        self,
        base: np.ndarray,
        cache_dir: str | Path = ".repairflow_cache",
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._base = _validate_base(base)
        self.base_hash = array_content_hash(self._base)
        self.config = normalize_config(dict(config or {}))
        self.graph = GraphSpec.from_config(self.config)
        self.cache = ArtifactCache(cache_dir)
        self.snapshots = SnapshotStore()
        self.stage_lock = threading.Lock()

        self._mask_state: MaskState | None = None
        self._style_config: dict[str, Any] | None = None
        self._node_state: dict[int, dict[str, CacheRecord]] = {}
        self._status: dict[int, dict[str, str]] = {}
        self._image: np.ndarray | None = None
        self._last_report: dict[str, Any] | None = None
        self._last_error: dict[str, Any] | None = None
        self._last_full: FullRecomputeResult | None = None

    @property
    def base(self) -> np.ndarray:
        return self._base

    @property
    def image(self) -> np.ndarray | None:
        return None if self._image is None else self._image.copy()

    @property
    def mask_state(self) -> MaskState | None:
        return self._mask_state

    @property
    def style_config(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._style_config)

    @property
    def last_report(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_report)

    @property
    def last_error(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_error)

    def submit(
        self,
        mask: Any,
        style: Mapping[str, Any] | None = None,
        *,
        base: np.ndarray | None = None,
        edit_record: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
    ) -> UpdateResult:
        if not self.stage_lock.acquire(blocking=False):
            error = BusinessError(
                ErrorCode.STAGE_BUSY,
                "another incremental submission is already active",
            )
            self._last_error = error.to_dict()
            raise error

        try:
            mask = coerce_mask_state(mask)
            candidate_base = self._base if base is None else _validate_base(base)
            candidate_base_hash = array_content_hash(candidate_base)
            raw_config = (
                merge_config(self._style_config or self.config, dict(style))
                if style is not None
                else self.config
            )
            candidate_config = normalize_config(raw_config)
            graph = GraphSpec.from_config(candidate_config)
            validate_mask_state(
                mask,
                canvas_shape=(candidate_base.shape[0], candidate_base.shape[1]),
                max_regions=candidate_config["limits"]["max_regions"],
            )
            self._check_memory_limit(mask, candidate_config, candidate_base.shape[:2])

            edits, added, removed, changed_masks, field_changes, base_changed, graph_changed = (
                self._diff_inputs(
                    old_mask=self._mask_state,
                    new_mask=mask,
                    old_style=self._style_config,
                    new_style=candidate_config,
                    old_base_hash=self.base_hash,
                    new_base_hash=candidate_base_hash,
                    old_graph=self.graph,
                    new_graph=graph,
                )
            )
            if edit_record is not None:
                if isinstance(edit_record, Mapping):
                    edits.append(copy.deepcopy(dict(edit_record)))
                else:
                    edits.extend(copy.deepcopy(dict(item)) for item in edit_record)

            invalidated = self._invalidated_nodes(
                mask=mask,
                added=added,
                changed_masks=changed_masks,
                field_changes=field_changes,
                base_changed=base_changed,
                graph_changed=graph_changed,
                had_state=self._mask_state is not None,
            )
            invalidated_regions = sorted(
                region_id for region_id, nodes in invalidated.items() if nodes
            )

            worker_count, degradation = self._execution_plan(
                invalidated_regions, candidate_config
            )
            results = self._process_regions(
                base=candidate_base,
                base_hash=candidate_base_hash,
                mask=mask,
                config=candidate_config,
                graph=graph,
                old_state=self._node_state,
                worker_count=worker_count,
            )

            node_state = {region_id: item[0] for region_id, item in results.items()}
            status = {region_id: item[1] for region_id, item in results.items()}
            blend_specs: dict[int, dict[str, Any]] = {}
            for region_id in mask.region_ids:
                record = node_state[region_id][NODE_BLEND_OUTPUT]
                array, verified = self.cache.load(record.key)
                if verified.artifact_hash != record.artifact_hash:
                    raise BusinessError(
                        ErrorCode.CACHE_CORRUPT,
                        "loaded artifact hash differs from committed record",
                        region_id=region_id,
                        node=NODE_BLEND_OUTPUT,
                    )
                blend_specs[region_id] = {
                    "region_id": region_id,
                    "array": array,
                    "bbox": record.product_metadata["bbox"],
                }

            masks = {region.region_id: region.mask for region in mask.regions}
            incremental_image = compose_regionwise(candidate_base, blend_specs)
            global_image, blend_check = compose_global(candidate_base, blend_specs)
            blend_check.update(assert_blend_consistency(incremental_image, global_image, masks))
            blend_check["spectrum"] = blend_deviation_spectrum(
                incremental_image, global_image, masks, blend_specs
            )

            style_payload = style_snapshot_payload(candidate_config)
            mask_version = self.snapshots.next_version("mask", mask)
            style_version = self.snapshots.next_version("style", style_payload)
            image_version = self.snapshots.next_version("image", incremental_image)
            report = self._build_report(
                mask=mask,
                base_hash=candidate_base_hash,
                canvas_shape=(candidate_base.shape[0], candidate_base.shape[1]),
                mask_version=mask_version,
                config=candidate_config,
                style_payload=style_payload,
                style_version=style_version,
                image=incremental_image,
                image_version=image_version,
                edits=edits,
                added=added,
                removed=removed,
                changed_masks=changed_masks,
                field_changes=field_changes,
                invalidated_regions=invalidated_regions,
                node_state=node_state,
                status=status,
                blend_check=blend_check,
                degradation=degradation,
                worker_count=worker_count,
                graph=graph,
            )
            report["self_check"] = self._self_check(
                report=report,
                mask=mask,
                node_state=node_state,
                invalidated=invalidated,
                image=incremental_image,
                config=candidate_config,
            )

            self.snapshots.commit("mask", mask_version, mask)
            self.snapshots.commit("style", style_version, style_payload)
            self.snapshots.commit("image", image_version, incremental_image)

            self._base = candidate_base
            self.base_hash = candidate_base_hash
            self.config = candidate_config
            self.graph = graph
            self._mask_state = mask
            self._style_config = candidate_config
            self._node_state = node_state
            self._status = status
            self._image = incremental_image
            self._last_report = report
            self._last_full = None
            report["snapshots"] = self.snapshots.status()
            return UpdateResult(
                image=incremental_image.copy(),
                report=copy.deepcopy(report),
                mask_state=mask,
                style_config=copy.deepcopy(candidate_config),
            )
        except BusinessError as exc:
            self._last_error = exc.to_dict()
            raise
        except Exception as exc:
            error = BusinessError(
                ErrorCode.INTERNAL_ERROR,
                "unexpected internal failure during submission",
                cause=type(exc).__name__,
                error_message=str(exc),
            )
            self._last_error = error.to_dict()
            raise error from exc
        finally:
            self.stage_lock.release()

    def full_recompute(
        self,
        mask: Any | None = None,
        style: Mapping[str, Any] | None = None,
        *,
        base: np.ndarray | None = None,
    ) -> FullRecomputeResult:
        """Recompute every region and node without reading or writing the cache."""

        used_mask = self._mask_state if mask is None else coerce_mask_state(mask)
        if used_mask is None:
            error = BusinessError(
                ErrorCode.NO_ACTIVE_STATE,
                "submit an initial incremental state before full recompute",
            )
            self._last_error = error.to_dict()
            raise error
        used_style = normalize_config(
            merge_config(self._style_config or self.config, dict(style))
            if style is not None
            else (self._style_config or self.config)
        )
        used_base = self._base if base is None else _validate_base(base)
        graph = GraphSpec.from_config(used_style)
        try:
            validate_mask_state(
                used_mask,
                canvas_shape=used_base.shape[:2],
                max_regions=used_style["limits"]["max_regions"],
            )
            self._check_memory_limit(used_mask, used_style, used_base.shape[:2])
        except BusinessError as exc:
            self._last_error = exc.to_dict()
            raise

        all_records: dict[int, dict[str, CacheRecord]] = {}
        all_arrays: dict[int, dict[str, np.ndarray]] = {}
        for region in used_mask.regions:
            records: dict[str, CacheRecord] = {}
            arrays: dict[str, np.ndarray] = {}
            for node in graph.topological_order():
                array, metadata = self._execute_node(
                    node=node,
                    region=region,
                    config=used_style,
                    records=records,
                    arrays=arrays,
                    base=used_base,
                )
                records[node] = _full_record(node, region.region_id, array, metadata)
                arrays[node] = array
            all_records[region.region_id] = records
            all_arrays[region.region_id] = arrays

        blends = {
            region_id: {
                "region_id": region_id,
                "array": all_arrays[region_id][NODE_BLEND_OUTPUT],
                "bbox": all_records[region_id][NODE_BLEND_OUTPUT].product_metadata["bbox"],
            }
            for region_id in used_mask.region_ids
        }
        masks = {region.region_id: region.mask for region in used_mask.regions}
        regionwise = compose_regionwise(used_base, blends)
        global_image, blend_check = compose_global(used_base, blends)
        blend_check.update(assert_blend_consistency(regionwise, global_image, masks))

        result = FullRecomputeResult(
            image=regionwise,
            node_records=all_records,
            arrays=all_arrays,
            blend_check=blend_check,
            mask_state=used_mask,
            style_config=used_style,
        )
        self._last_full = result
        return result

    def compare_with_full(
        self,
        *,
        mask: MaskState | None = None,
        style: Mapping[str, Any] | None = None,
        base: np.ndarray | None = None,
        raise_on_mismatch: bool = False,
    ) -> dict[str, Any]:
        if self._image is None or self._mask_state is None:
            error = BusinessError(
                ErrorCode.NO_ACTIVE_STATE,
                "no active incremental result to compare",
            )
            self._last_error = error.to_dict()
            raise error

        full = self.full_recompute(mask=mask, style=style, base=base)
        incremental = self._image
        difference = first_difference(incremental, full.image)
        byte_identical = difference is None and incremental.tobytes() == full.image.tobytes()
        hash_equal = array_content_hash(incremental) == array_content_hash(full.image)

        comparisons: dict[str, Any] = {}
        hashes_equal = True
        for region_id in self._mask_state.region_ids:
            comparisons[str(region_id)] = {}
            for node in NODE_ORDER:
                incremental_hash = self._node_state[region_id][node].artifact_hash
                full_hash = full.node_records[region_id][node].artifact_hash
                equal = incremental_hash == full_hash
                hashes_equal = hashes_equal and equal
                comparisons[str(region_id)][node] = {
                    "equal": equal,
                    "incremental": incremental_hash,
                    "full": full_hash,
                }

        blend_specs = self._current_blend_specs()
        spectrum = blend_deviation_spectrum(
            incremental,
            full.image,
            {region.region_id: region.mask for region in self._mask_state.regions},
            blend_specs,
        )
        result = {
            "byte_identical": byte_identical,
            "hash_identical": hash_equal,
            "artifact_hashes_equal": hashes_equal,
            "incremental_hash": array_content_hash(incremental),
            "full_hash": array_content_hash(full.image),
            "first_difference": _difference_payload(difference, self._mask_state),
            "blend_consistency": full.blend_check,
            "blend_deviation_spectrum": spectrum,
            "node_comparison": comparisons,
        }
        if raise_on_mismatch and (not byte_identical or not hashes_equal):
            raise BusinessError(
                ErrorCode.INCONSISTENT,
                "incremental result does not match full recompute",
                **{
                    "byte_identical": byte_identical,
                    "artifact_hashes_equal": hashes_equal,
                    "first_difference": result["first_difference"],
                },
            )
        return result

    def inspect_region(self, region_id: int) -> dict[str, Any]:
        if self._mask_state is None or region_id not in self._node_state:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "unknown or inactive region",
                region_id=region_id,
                field="region_id",
            )
        region = self._mask_state.by_id()[region_id]
        nodes: dict[str, Any] = {}
        for node in NODE_ORDER:
            record = self._node_state[region_id][node]
            nodes[node] = {
                **record.to_dict(),
                "last_status": self._status.get(region_id, {}).get(node),
                "artifact_exists": (self.cache.objects_dir / record.artifact_file).exists(),
            }
        return {
            "region": region.to_dict(),
            "nodes": nodes,
            "mask_digest": region.digest,
        }

    def self_check(self, report: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._mask_state is None or self._image is None:
            raise BusinessError(
                ErrorCode.NO_ACTIVE_STATE,
                "no active result to self-check",
            )
        used_report = dict(report) if report is not None else dict(self._last_report or {})
        return self._self_check(
            report=used_report,
            mask=self._mask_state,
            node_state=self._node_state,
            invalidated={},
            image=self._image,
            config=self._style_config or self.config,
            enforce_invalidated=False,
        )

    def snapshot_status(self) -> dict[str, Any]:
        return self.snapshots.status()

    def verify_snapshots(self) -> dict[str, Any]:
        for kind in ("mask", "style", "image"):
            self.snapshots.verify(kind)
        return {"ok": True, "snapshots": self.snapshots.status()}

    def _check_memory_limit(
        self,
        mask: MaskState,
        config: dict[str, Any],
        canvas_shape: tuple[int, int],
    ) -> dict[str, Any]:
        estimated = estimate_resident_pixels(
            mask,
            config["context_radius"],
            canvas_shape,
            config["limits"]["resident_node_copies"],
        )
        limit = config["limits"]["max_resident_pixels"]
        if estimated > limit:
            raise BusinessError(
                ErrorCode.RESOURCE_LIMIT,
                "estimated resident pixel count exceeds configured maximum",
                limit="limits.max_resident_pixels",
                estimated=estimated,
                max_resident_pixels=limit,
            )
        return {
            "estimated_resident_pixels": estimated,
            "max_resident_pixels": limit,
            "resident_node_copies": config["limits"]["resident_node_copies"],
        }

    def _execution_plan(
        self,
        invalidated_regions: list[int],
        config: dict[str, Any],
    ) -> tuple[int, dict[str, Any] | None]:
        limit = config["limits"]["max_parallel_regions"]
        requested = len(invalidated_regions)
        if requested > limit:
            return 1, {
                "degraded": True,
                "categories": ["parallel_region_limit_serial"],
                "limit": "limits.max_parallel_regions",
                "max_parallel_regions": limit,
                "requested_regions": requested,
                "workers_used": 1,
                "omitted_regions": [],
            }
        workers = 1 if requested <= 1 else requested
        return workers, None

    def _process_regions(
        self,
        *,
        base: np.ndarray,
        base_hash: str,
        mask: MaskState,
        config: dict[str, Any],
        graph: GraphSpec,
        old_state: dict[int, dict[str, CacheRecord]],
        worker_count: int,
    ) -> dict[int, tuple[dict[str, CacheRecord], dict[str, str]]]:
        def process(region: Region) -> tuple[int, tuple[dict[str, CacheRecord], dict[str, str]]]:
            result = self._process_region(
                base=base,
                base_hash=base_hash,
                region=region,
                config=config,
                graph=graph,
                old_records=old_state.get(region.region_id, {}),
            )
            return region.region_id, result

        if worker_count <= 1 or len(mask.regions) <= 1:
            items = [process(region) for region in mask.regions]
        else:
            with ThreadPoolExecutor(max_workers=min(worker_count, len(mask.regions))) as executor:
                items = list(executor.map(process, mask.regions))
        items.sort(key=lambda item: item[0])
        return dict(items)

    def _process_region(
        self,
        *,
        base: np.ndarray,
        base_hash: str,
        region: Region,
        config: dict[str, Any],
        graph: GraphSpec,
        old_records: dict[str, CacheRecord],
    ) -> tuple[dict[str, CacheRecord], dict[str, str]]:
        records: dict[str, CacheRecord] = {}
        arrays: dict[str, np.ndarray] = {}
        status: dict[str, str] = {}

        for node in graph.topological_order():
            components = self._cache_key_components(
                node=node,
                region=region,
                base_hash=base_hash,
                config=config,
                graph=graph,
                records=records,
            )
            key = cache_key(components)
            old_record = old_records.get(node)
            if old_record is not None and old_record.key == key:
                records[node] = old_record
                status[node] = "cache_hit"
                continue

            existing = self.cache.lookup(key)
            if existing is not None:
                try:
                    array, loaded = self.cache.load(key)
                except BusinessError as exc:
                    raise self._attribute_error(exc, region.region_id, node) from exc
                records[node] = loaded
                arrays[node] = array
                status[node] = "cache_hit"
                continue

            try:
                array, metadata = self._execute_node(
                    node=node,
                    region=region,
                    config=config,
                    records=records,
                    arrays=arrays,
                    base=base,
                )
                record = self.cache.store(
                    key=key,
                    array=array,
                    node=node,
                    region_id=region.region_id,
                    components=components,
                    product_metadata=metadata,
                )
            except BusinessError as exc:
                raise self._attribute_error(exc, region.region_id, node) from exc
            except Exception as exc:
                raise BusinessError(
                    ErrorCode.REGION_NODE_FAILURE,
                    "region node computation failed",
                    region_id=region.region_id,
                    node=node,
                    cause=type(exc).__name__,
                    error_message=str(exc),
                ) from exc
            records[node] = record
            arrays[node] = array
            status[node] = "recomputed"

        return records, status

    def _cache_key_components(
        self,
        *,
        node: str,
        region: Region,
        base_hash: str,
        config: dict[str, Any],
        graph: GraphSpec,
        records: dict[str, CacheRecord],
    ) -> dict[str, Any]:
        ancestors = graph.ancestors(node)
        upstream = [
            {"node": ancestor, "artifact_hash": records[ancestor].artifact_hash}
            for ancestor in ancestors
        ]
        return {
            "schema": "RF-REGION-KEY-V1",
            "algorithm_version": ALGORITHM_VERSION,
            "graph_signature": graph.signature(),
            "node": node,
            "region_id": region.region_id,
            "base_hash": base_hash,
            "region_mask_digest": region.digest,
            "context_radius": config["context_radius"],
            "style_param_version": style_projection_digest(config, node),
            "upstream_artifact_hashes": upstream,
            "node_params": _node_params(node, config),
        }

    def _execute_node(
        self,
        *,
        node: str,
        region: Region,
        config: dict[str, Any],
        records: dict[str, CacheRecord],
        arrays: dict[str, np.ndarray],
        base: np.ndarray,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        region_bbox = region.bbox()
        if node == NODE_MASK_PARSE:
            array = mask_crop(region.mask, region_bbox)
            return array, {
                "kind": "mask_crop",
                "bbox": list(region_bbox),
                "region_pixels": region.pixel_count,
            }

        if node == NODE_CONTEXT_SAMPLE:
            ctx_bbox = context_bbox(region_bbox, config["context_radius"], base.shape[:2])
            array = context_sample(base, ctx_bbox)
            return array, {
                "kind": "boundary_context",
                "bbox": list(ctx_bbox),
                "region_bbox": list(region_bbox),
                "radius": config["context_radius"],
            }

        if node == NODE_CONTENT_COMPLETION:
            context = self._upstream_array(NODE_CONTEXT_SAMPLE, records, arrays)
            ctx_bbox = _bbox_from_record(records[NODE_CONTEXT_SAMPLE])
            mask_ctx = region.mask[ctx_bbox[0] : ctx_bbox[2], ctx_bbox[1] : ctx_bbox[3]]
            array = content_completion(
                context,
                mask_ctx,
                iterations=config["style"]["iterations"],
                precision=config["style"]["precision"],
            )
            return array, {
                "kind": "completed_region",
                "bbox": list(ctx_bbox),
                "iterations": config["style"]["iterations"],
            }

        if node == NODE_STYLE_TRANSFER:
            completed = self._upstream_array(NODE_CONTENT_COMPLETION, records, arrays)
            ctx_bbox = _bbox_from_record(records[NODE_CONTENT_COMPLETION])
            mask_ctx = region.mask[ctx_bbox[0] : ctx_bbox[2], ctx_bbox[1] : ctx_bbox[3]]
            array = style_transfer(
                completed,
                mask_ctx,
                config["style"],
                config["style"]["precision"],
            )
            return array, {
                "kind": "styled_region",
                "bbox": list(ctx_bbox),
                "style_strength": config["style"]["style_strength"],
            }

        if node == NODE_BLEND_OUTPUT:
            styled = self._upstream_array(NODE_STYLE_TRANSFER, records, arrays)
            ctx_bbox = _bbox_from_record(records[NODE_STYLE_TRANSFER])
            mask_ctx = region.mask[ctx_bbox[0] : ctx_bbox[2], ctx_bbox[1] : ctx_bbox[3]]
            array = blend_region(
                styled,
                mask_ctx,
                region_bbox,
                ctx_bbox,
                config["style"]["blend"]["feather_px"],
                config["style"]["precision"],
            )
            return array, {
                "kind": "blend_rgba",
                "bbox": list(region_bbox),
                "feather_px": config["style"]["blend"]["feather_px"],
                "weight_rule": "region_weight + (1 - region_weight) = 1",
            }

        raise BusinessError(
            ErrorCode.INVALID_GRAPH,
            "unknown graph node",
            node=node,
            region_id=region.region_id,
        )

    def _upstream_array(
        self,
        node: str,
        records: dict[str, CacheRecord],
        arrays: dict[str, np.ndarray],
    ) -> np.ndarray:
        if node in arrays:
            return arrays[node]
        if node not in records:
            raise BusinessError(
                ErrorCode.INCONSISTENT,
                "upstream node artifact is unavailable",
                node=node,
                region_id=None,
            )
        array, _ = self.cache.load(records[node].key)
        arrays[node] = array
        return array

    def _current_blend_specs(self) -> dict[int, dict[str, Any]]:
        specs: dict[int, dict[str, Any]] = {}
        if self._mask_state is None:
            return specs
        for region_id in self._mask_state.region_ids:
            record = self._node_state[region_id][NODE_BLEND_OUTPUT]
            array, _ = self.cache.load(record.key)
            specs[region_id] = {
                "region_id": region_id,
                "array": array,
                "bbox": record.product_metadata["bbox"],
            }
        return specs

    def _diff_inputs(
        self,
        *,
        old_mask: MaskState | None,
        new_mask: MaskState,
        old_style: dict[str, Any] | None,
        new_style: dict[str, Any],
        old_base_hash: str,
        new_base_hash: str,
        old_graph: GraphSpec,
        new_graph: GraphSpec,
    ) -> tuple[
        list[dict[str, Any]],
        list[int],
        list[int],
        list[int],
        list[str],
        bool,
        bool,
    ]:
        edits: list[dict[str, Any]] = []
        added: list[int] = []
        removed: list[int] = []
        changed_masks: list[int] = []
        fields: list[str] = []
        base_changed = old_base_hash != new_base_hash
        graph_changed = old_graph.signature() != new_graph.signature()

        if base_changed:
            edits.append(
                {
                    "type": "base_change",
                    "before_hash": old_base_hash,
                    "after_hash": new_base_hash,
                }
            )
        if graph_changed:
            edits.append(
                {
                    "type": "graph_change",
                    "before_signature": old_graph.signature(),
                    "after_signature": new_graph.signature(),
                }
            )

        old_regions = {} if old_mask is None else old_mask.by_id()
        new_regions = new_mask.by_id()
        for region_id in sorted(new_regions):
            if region_id not in old_regions:
                added.append(region_id)
                edits.append(
                    {
                        "type": "mask_add",
                        "region_id": region_id,
                        "pixels_added": new_regions[region_id].pixel_count,
                    }
                )
                continue
            old_region = old_regions[region_id]
            new_region = new_regions[region_id]
            if old_region.digest != new_region.digest:
                changed_masks.append(region_id)
                added_pixels = np.logical_and(new_region.mask, ~old_region.mask)
                removed_pixels = np.logical_and(old_region.mask, ~new_region.mask)
                added_count = int(np.count_nonzero(added_pixels))
                removed_count = int(np.count_nonzero(removed_pixels))
                if added_count and not removed_count:
                    action = "grow"
                elif removed_count and not added_count:
                    action = "shrink"
                else:
                    action = "edit"
                edits.append(
                    {
                        "type": "mask_change",
                        "region_id": region_id,
                        "action": action,
                        "pixels_added": added_count,
                        "pixels_removed": removed_count,
                        "before_digest": old_region.digest,
                        "after_digest": new_region.digest,
                        "before_bbox": list(old_region.bbox()),
                        "after_bbox": list(new_region.bbox()),
                    }
                )
        for region_id in sorted(set(old_regions) - set(new_regions)):
            removed.append(region_id)
            edits.append(
                {
                    "type": "mask_remove",
                    "region_id": region_id,
                    "pixels_removed": old_regions[region_id].pixel_count,
                }
            )

        if old_style is not None:
            old_flat = _flatten_config_for_diff(old_style)
            new_flat = _flatten_config_for_diff(new_style)
            for field in sorted(set(old_flat) | set(new_flat)):
                if old_flat.get(field) != new_flat.get(field):
                    fields.append(field)
                    edits.append(
                        {
                            "type": "style_change" if field.startswith("style.") else "config_change",
                            "field": field,
                            "before": old_flat.get(field),
                            "after": new_flat.get(field),
                        }
                    )
            old_limits = old_style["limits"]
            new_limits = new_style["limits"]
            for key in sorted(set(old_limits) | set(new_limits)):
                if old_limits.get(key) != new_limits.get(key):
                    edits.append(
                        {
                            "type": "limits_change",
                            "field": f"limits.{key}",
                            "before": old_limits.get(key),
                            "after": new_limits.get(key),
                        }
                    )
        elif old_mask is not None:
            fields.append("*")

        return (
            edits,
            added,
            removed,
            changed_masks,
            fields,
            base_changed,
            graph_changed,
        )

    def _invalidated_nodes(
        self,
        *,
        mask: MaskState,
        added: list[int],
        changed_masks: list[int],
        field_changes: list[str],
        base_changed: bool,
        graph_changed: bool,
        had_state: bool,
    ) -> dict[int, set[str]]:
        invalidated = {region_id: set() for region_id in mask.region_ids}
        if not had_state or base_changed or graph_changed or "*" in field_changes:
            for region_id in invalidated:
                invalidated[region_id].update(NODE_ORDER)
            return invalidated

        for region_id in added + changed_masks:
            invalidated[region_id].update(NODE_ORDER)

        for field in field_changes:
            nodes = _invalidated_nodes_for_field(field)
            for region_id in invalidated:
                invalidated[region_id].update(nodes)
        return invalidated

    def _build_report(
        self,
        *,
        mask: MaskState,
        base_hash: str,
        canvas_shape: tuple[int, int],
        mask_version: int,
        config: dict[str, Any],
        style_payload: dict[str, Any],
        style_version: int,
        image: np.ndarray,
        image_version: int,
        edits: list[dict[str, Any]],
        added: list[int],
        removed: list[int],
        changed_masks: list[int],
        field_changes: list[str],
        invalidated_regions: list[int],
        node_state: dict[int, dict[str, CacheRecord]],
        status: dict[int, dict[str, str]],
        blend_check: dict[str, Any],
        degradation: dict[str, Any] | None,
        worker_count: int,
        graph: GraphSpec,
    ) -> dict[str, Any]:
        active = list(mask.region_ids)
        recomputed_regions: list[int] = []
        reused_regions: list[int] = []
        recomputed_nodes: dict[str, list[str]] = {}
        reused_nodes: dict[str, list[str]] = {}
        node_status: dict[str, dict[str, str]] = {}
        cache_hits: list[dict[str, Any]] = []
        cache_misses: list[dict[str, Any]] = []

        for region_id in active:
            region_status = status.get(region_id, {})
            node_status[str(region_id)] = region_status
            recomputed = [node for node in NODE_ORDER if region_status.get(node) == "recomputed"]
            reused = [node for node in NODE_ORDER if region_status.get(node) != "recomputed"]
            recomputed_nodes[str(region_id)] = recomputed
            reused_nodes[str(region_id)] = reused
            if recomputed:
                recomputed_regions.append(region_id)
            else:
                reused_regions.append(region_id)
            for node in NODE_ORDER:
                record = node_state[region_id][node]
                item = {
                    "region_id": region_id,
                    "node": node,
                    "key": record.key,
                    "artifact_hash": record.artifact_hash,
                    "status": region_status.get(node),
                }
                if region_status.get(node) == "cache_hit":
                    cache_hits.append(item)
                elif region_status.get(node) == "recomputed":
                    cache_misses.append(item)

        report = {
            "base_hash": base_hash,
            "graph": graph.to_dict(),
            "edits": edits,
            "added_regions": added,
            "removed_regions": removed,
            "changed_mask_regions": changed_masks,
            "changed_fields": field_changes,
            "invalidated_regions": invalidated_regions,
            "affected_regions": active,
            "recomputed_regions": recomputed_regions,
            "reused_regions": reused_regions,
            "node_status": node_status,
            "recomputed_nodes": recomputed_nodes,
            "reused_nodes": reused_nodes,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_hit_count": len(cache_hits),
            "recomputed_node_count": len(cache_misses),
            "output_hash": array_content_hash(image),
            "output_checksum": sha256_json(
                {
                    "kind": "incremental-output",
                    "hash": array_content_hash(image),
                }
            ),
            "blend_check": blend_check,
            "degradation": degradation,
            "degraded": bool(degradation and degradation.get("degraded")),
            "execution": {
                "workers_used": worker_count,
                "mode": "parallel" if worker_count > 1 else "serial",
            },
            "resource_estimate": {
                "estimated_resident_pixels": estimate_resident_pixels(
                    mask,
                    config["context_radius"],
                    canvas_shape,
                    config["limits"]["resident_node_copies"],
                ),
                "max_resident_pixels": config["limits"]["max_resident_pixels"],
            },
            "snapshots": {
                "mask": {
                    "version": mask_version,
                    "checksum": snapshot_checksum("mask", mask),
                },
                "style": {
                    "version": style_version,
                    "checksum": snapshot_checksum("style", style_payload),
                },
                "image": {
                    "version": image_version,
                    "checksum": snapshot_checksum("image", image),
                },
            },
            "recent_error": copy.deepcopy(self._last_error),
        }
        return report

    def _self_check(
        self,
        *,
        report: Mapping[str, Any],
        mask: MaskState,
        node_state: dict[int, dict[str, CacheRecord]],
        invalidated: dict[int, set[str]],
        image: np.ndarray,
        config: dict[str, Any],
        enforce_invalidated: bool = True,
    ) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        active = set(mask.region_ids)
        recomputed = set(report.get("recomputed_regions", []))
        reused = set(report.get("reused_regions", []))
        affected = set(report.get("affected_regions", []))

        if recomputed & reused:
            failures.append(
                {
                    "code": "RECOMPUTED_REUSED_OVERLAP",
                    "regions": sorted(recomputed & reused),
                }
            )
        if recomputed | reused != affected:
            failures.append(
                {
                    "code": "REGION_SETS_DO_NOT_COVER_AFFECTED",
                    "missing": sorted(affected - (recomputed | reused)),
                    "extra": sorted((recomputed | reused) - affected),
                }
            )
        if affected != active:
            failures.append(
                {
                    "code": "AFFECTED_SET_DOES_NOT_MATCH_ACTIVE_REGIONS",
                    "affected": sorted(affected),
                    "active": sorted(active),
                }
            )

        for region_id in mask.region_ids:
            recomputed_nodes = set(report.get("recomputed_nodes", {}).get(str(region_id), []))
            reused_nodes = set(report.get("reused_nodes", {}).get(str(region_id), []))
            if recomputed_nodes & reused_nodes:
                failures.append(
                    {
                        "code": "NODE_RECOMPUTED_REUSED_OVERLAP",
                        "region_id": region_id,
                        "nodes": sorted(recomputed_nodes & reused_nodes),
                    }
                )
            if recomputed_nodes | reused_nodes != set(NODE_ORDER):
                failures.append(
                    {
                        "code": "NODE_SETS_INCOMPLETE",
                        "region_id": region_id,
                    }
                )
            if enforce_invalidated and invalidated:
                allowed = invalidated.get(region_id, set())
                if not recomputed_nodes <= allowed:
                    failures.append(
                        {
                            "code": "RECOMPUTED_OUTSIDE_INVALIDATION",
                            "region_id": region_id,
                            "nodes": sorted(recomputed_nodes - allowed),
                        }
                    )

        expected_output_hash = array_content_hash(image)
        if report.get("output_hash") != expected_output_hash:
            failures.append(
                {
                    "code": "OUTPUT_HASH_MISMATCH",
                    "expected": expected_output_hash,
                    "actual": report.get("output_hash"),
                }
            )
        for kind, snapshot in report.get("snapshots", {}).items():
            if kind == "mask":
                payload = mask
            elif kind == "style":
                payload = style_snapshot_payload(config)
            else:
                payload = image
            if snapshot_checksum(kind, payload) != snapshot.get("checksum"):
                failures.append(
                    {
                        "code": "SNAPSHOT_CHECKSUM_MISMATCH",
                        "kind": kind,
                    }
                )

        for region_id, nodes in node_state.items():
            for node, record in nodes.items():
                path = self.cache.objects_dir / record.artifact_file
                if not path.exists():
                    failures.append(
                        {
                            "code": "CACHE_ARTIFACT_MISSING",
                            "region_id": region_id,
                            "node": node,
                            "artifact_hash": record.artifact_hash,
                        }
                    )
                    continue
                try:
                    _, loaded = self.cache.load(record.key)
                except BusinessError as exc:
                    failures.append(
                        {
                            "code": "CACHE_ARTIFACT_CORRUPT",
                            "region_id": region_id,
                            "node": node,
                            "artifact_hash": record.artifact_hash,
                            "error_code": exc.code,
                        }
                    )
                    continue
                if loaded.artifact_hash != record.artifact_hash:
                    failures.append(
                        {
                            "code": "CACHE_ARTIFACT_HASH_MISMATCH",
                            "region_id": region_id,
                            "node": node,
                            "expected": record.artifact_hash,
                            "actual": loaded.artifact_hash,
                        }
                    )

        if failures:
            raise BusinessError(
                ErrorCode.INCONSISTENT,
                "cross-module consistency self-check failed",
                failures=failures,
                recomputed_regions=sorted(recomputed),
                reused_regions=sorted(reused),
                affected_regions=sorted(affected),
            )
        return {
            "ok": True,
            "recomputed_and_reused_disjoint": True,
            "sets_cover_affected": True,
            "node_sets_complete": True,
            "cache_artifacts_present": True,
            "recomputed_regions": sorted(recomputed),
            "reused_regions": sorted(reused),
            "affected_regions": sorted(affected),
        }

    @staticmethod
    def _attribute_error(exc: BusinessError, region_id: int, node: str) -> BusinessError:
        if exc.details.get("region_id") is None:
            exc.details["region_id"] = region_id
        exc.details.setdefault("node", node)
        return exc


def _validate_base(base: np.ndarray) -> np.ndarray:
    array = np.asarray(base)
    if array.ndim != 3 or array.shape[2] != 3:
        raise BusinessError(
            ErrorCode.INVALID_BASE,
            "base image must be an HxWx3 RGB array",
            field="base.shape",
            shape=list(array.shape),
        )
    if array.dtype != np.uint8:
        raise BusinessError(
            ErrorCode.INVALID_BASE,
            "base image must use uint8 RGB pixels",
            field="base.dtype",
            actual=str(array.dtype),
            expected="uint8",
        )
    if array.shape[0] <= 0 or array.shape[1] <= 0:
        raise BusinessError(
            ErrorCode.INVALID_BASE,
            "base image must not be empty",
            field="base.shape",
            shape=list(array.shape),
        )
    normalized = np.array(array, dtype=np.uint8, order="C", copy=True)
    normalized.setflags(write=False)
    return normalized


def _node_params(node: str, config: dict[str, Any]) -> dict[str, Any]:
    style = config["style"]
    if node == NODE_MASK_PARSE:
        return {"operation": "binary-mask-crop-v1"}
    if node == NODE_CONTEXT_SAMPLE:
        return {
            "operation": "boundary-base-crop-v1",
            "context_radius": config["context_radius"],
        }
    if node == NODE_CONTENT_COMPLETION:
        return {
            "operation": "nearest-boundary-diffusion-v1",
            "iterations": style["iterations"],
            "precision": style["precision"],
        }
    if node == NODE_STYLE_TRANSFER:
        return {
            "operation": "matrix-edge-diffusion-v1",
            "style_strength": style["style_strength"],
            "color_matrix": style["color_matrix"],
            "edge_preserve": style["edge_preserve"],
            "iterations": style["iterations"],
            "precision": style["precision"],
        }
    if node == NODE_BLEND_OUTPUT:
        return {
            "operation": "inward-feather-blend-v1",
            "feather_px": style["blend"]["feather_px"],
            "precision": style["precision"],
            "weight_rule": "region_weight + (1 - region_weight) = 1",
        }
    raise BusinessError(
        ErrorCode.INVALID_GRAPH,
        "unknown node for cache parameters",
        node=node,
    )


def _bbox_from_record(record: CacheRecord) -> tuple[int, int, int, int]:
    return tuple(int(value) for value in record.product_metadata["bbox"])  # type: ignore[return-value]


def _full_record(
    node: str,
    region_id: int,
    array: np.ndarray,
    metadata: dict[str, Any],
) -> CacheRecord:
    metadata = dict(metadata)
    metadata.setdefault("node", node)
    metadata.setdefault("region_id", region_id)
    metadata.setdefault("array_hash", array_content_hash(array))
    artifact_hash = product_hash(array, metadata)
    components = {"mode": "full_recompute", "node": node, "region_id": region_id}
    return CacheRecord(
        key=cache_key(components),
        node=node,
        region_id=region_id,
        artifact_hash=artifact_hash,
        components=components,
        product_metadata=metadata,
        artifact_file=f"{artifact_hash}.npy",
    )


def _flatten_config_for_diff(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"context_radius": config["context_radius"]}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                walk(f"{prefix}.{key}", value[key])
        else:
            result[prefix] = value

    walk("style", config["style"])
    result["graph.signature"] = GraphSpec.from_config(config).signature()
    return result


def _invalidated_nodes_for_field(field: str) -> set[str]:
    if field == "context_radius":
        return set(NODE_ORDER)
    if field == "graph.signature":
        return set(NODE_ORDER)
    if field.startswith("style.blend."):
        return {NODE_BLEND_OUTPUT}
    if field in {
        "style.style_strength",
        "style.color_matrix",
        "style.edge_preserve",
    }:
        return {NODE_STYLE_TRANSFER, NODE_BLEND_OUTPUT}
    if field in {"style.iterations", "style.precision"}:
        return {NODE_CONTENT_COMPLETION, NODE_STYLE_TRANSFER, NODE_BLEND_OUTPUT}
    if field.startswith("style."):
        return set(NODE_ORDER)
    return set(NODE_ORDER)


def _difference_payload(
    difference: tuple[int, int, int, Any, Any] | None,
    mask: MaskState,
) -> dict[str, Any] | None:
    if difference is None:
        return None
    row, col, channel, left, right = difference
    region = None
    for item in mask.regions:
        if 0 <= row < item.mask.shape[0] and 0 <= col < item.mask.shape[1] and item.mask[row, col]:
            region = item.region_id
            break
    return {
        "row": row,
        "column": col,
        "x": col,
        "y": row,
        "channel": channel,
        "incremental_value": left,
        "full_value": right,
        "region_id": region,
    }
