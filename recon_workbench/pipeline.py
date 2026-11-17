"""End-to-end deterministic reconstruction pipeline and recovery orchestration."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .caching import (
    ArtifactStore,
    cloud_cache_key,
    depth_cache_key,
    feature_cache_key,
    pair_cache_key,
)
from .canonical import (
    atomic_write_json,
    sha256_json,
)
from .config import (
    config_fingerprint,
    semantic_config_fingerprint,
    validate_config,
)
from .depth import generate_depth_map
from .errors import (
    BusinessRuleError,
    E_CONFIG_INVALID,
    E_CONFIG_MISMATCH,
    E_MATCH_INSUFFICIENT,
    E_NONFINITE_FEATURE,
    E_STAGE_EXCEPTION,
    E_VIEW_INVALID,
    Failure,
    sort_failures,
)
from .features import (
    extract_features,
    inject_nonfinite_descriptor,
    payload_has_nonfinite_features,
)
from .geometry import match_and_triangulate_pair, pair_id_for
from .injection import (
    InjectionItem,
    InjectionSet,
    apply_config_injections,
    injection_ids_for_config_issue,
    load_injections,
)
from .io_views import ViewInput, ViewManifest, load_view_manifest
from .pointcloud import merge_global_cloud
from .snapshots import (
    SnapshotManager,
    SnapshotMismatch,
    empty_progress,
)
from .tasks import Task, TaskBatchResult, execute_tasks


@dataclass
class RunOutcome:
    status: str
    exit_code: int
    report: dict[str, Any]
    cloud: dict[str, Any] | None = None


class ReconstructionWorkbench:
    def __init__(
        self,
        *,
        input_dir: Path,
        config_path: Path,
        injection_path: Path | None = None,
        injection_profile: str | None = None,
        state_root: Path,
        cache_root: Path | None = None,
        report_path: Path | None = None,
        cloud_out_path: Path | None = None,
        depth_out_path: Path | None = None,
        features_out_path: Path | None = None,
        matches_out_path: Path | None = None,
        resume: bool = True,
        cache_enabled: bool = True,
        test_delay_ms: int = 0,
    ):
        self.input_dir = Path(input_dir)
        self.config_path = Path(config_path)
        self.injection_path = Path(injection_path) if injection_path is not None else None
        self.injection_profile = injection_profile
        self.state_root = Path(state_root)
        self.cache_root = Path(cache_root) if cache_root is not None else self.state_root / "cache"
        self.report_path = (
            Path(report_path) if report_path is not None else self.state_root / "report.json"
        )
        self.cloud_out_path = (
            Path(cloud_out_path)
            if cloud_out_path is not None
            else self.state_root / "cloud.json"
        )
        self.depth_out_path = (
            Path(depth_out_path)
            if depth_out_path is not None
            else self.state_root / "depth_layers.json"
        )
        self.features_out_path = (
            Path(features_out_path)
            if features_out_path is not None
            else self.state_root / "features.json"
        )
        self.matches_out_path = (
            Path(matches_out_path)
            if matches_out_path is not None
            else self.state_root / "matches.json"
        )
        self.resume = resume
        self.cache_enabled = cache_enabled
        self.test_delay_ms = max(0, int(test_delay_ms))
        self.stop_event = threading.Event()

    def request_stop(self) -> None:
        self.stop_event.set()

    def run(self) -> RunOutcome:
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            injections = load_injections(self.injection_path, self.injection_profile)
        except Exception as error:
            return self._config_invalid_outcome(
                None,
                None,
                injections=None,
                message="injection configuration is invalid",
                detail={"error": type(error).__name__ + ": " + str(error)},
            )

        try:
            raw_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as error:
            return self._config_invalid_outcome(
                None,
                None,
                injections=injections,
                message="reconstruction configuration cannot be read",
                detail={"error": type(error).__name__ + ": " + str(error)},
            )

        effective_raw = apply_config_injections(raw_config, injections)
        config, issues = validate_config(effective_raw)
        if issues:
            return self._config_invalid_outcome(
                effective_raw,
                None,
                injections=injections,
                message="configuration validation failed",
                detail={
                    "issues": [
                        {"field": issue.field, "message": issue.message} for issue in issues
                    ]
                },
                issues_fields=[issue.field for issue in issues],
            )
        assert config is not None
        config_hash = config_fingerprint(config)
        semantic_config_hash = semantic_config_fingerprint(config)
        snapshot_version = int(config["snapshot"]["version"])

        snapshot_manager = SnapshotManager(self.state_root)
        saved_snapshot: dict[str, Any] | None = None
        if self.resume:
            try:
                saved_snapshot = snapshot_manager.load_validated(
                    snapshot_version=snapshot_version,
                    config_hash=config_hash,
                )
            except SnapshotMismatch as mismatch:
                return self._config_mismatch_outcome(
                    config=config,
                    injections=injections,
                    mismatch=mismatch,
                )
            except Exception as error:
                return self._config_mismatch_outcome(
                    config=config,
                    injections=injections,
                    mismatch=SnapshotMismatch(
                        reason="latest snapshot is unreadable: " + str(error),
                        latest_sequence=snapshot_manager.latest_sequence(),
                        expected_version=snapshot_version,
                        found_version=None,
                        expected_config_hash=config_hash,
                        found_config_hash=None,
                    ),
                )

        recovery_point = {
            "mode": "resume" if saved_snapshot is not None else "fresh",
            "snapshot_sequence": (
                int(saved_snapshot.get("sequence", 0)) if saved_snapshot is not None else 0
            ),
            "stage": saved_snapshot.get("stage") if saved_snapshot is not None else None,
            "stage_complete": (
                bool(saved_snapshot.get("stage_complete", False))
                if saved_snapshot is not None
                else False
            ),
            "completed_views": (
                list(saved_snapshot.get("completed_views", []))
                if saved_snapshot is not None
                else []
            ),
        }

        try:
            manifest = load_view_manifest(self.input_dir, injections)
        except Exception as error:
            manifest = ViewManifest(
                views=tuple(),
                failures=(
                    Failure(
                        code=E_VIEW_INVALID,
                        stage="features",
                        message="view manifest loading failed",
                        detail={"error": type(error).__name__ + ": " + str(error)},
                    ),
                ),
                discovered_indices=tuple(),
                camera_hash=sha256_json({"error": "manifest-load-failed"}),
                input_hash=sha256_json({"error": "manifest-load-failed"}),
                resolution=None,
                image_hashes={},
                camera_hashes={},
            )
        if saved_snapshot is not None:
            recovery_point["camera_hash_match"] = (
                saved_snapshot.get("camera_hash") == manifest.camera_hash
            )
            recovery_point["input_hash_match"] = (
                saved_snapshot.get("input_hash") == manifest.input_hash
            )
            recovery_point["injection_hash_match"] = (
                saved_snapshot.get("injection_hash") == injections.hash
            )

        failures: list[Failure] = list(manifest.failures)
        if not manifest.discovered_indices:
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="no view images or camera parameter files were discovered",
                )
            )

        store = ArtifactStore(self.cache_root, enabled=self.cache_enabled)
        progress = empty_progress()
        snapshot_writes: list[dict[str, Any]] = []
        stage_payloads: dict[str, dict[str, tuple[Task, Any]]] = {
            name: {} for name in ("features", "matching", "depth", "merge")
        }
        latest_partial_summary: dict[str, Any] = {
            "reconstructed_point_count": 0,
            "source_pair_count": 0,
            "digest": sha256_json([]),
            "finalized": False,
        }
        interval = int(config["snapshot"]["commit_interval_views"])
        snapshot_view_checkpoints: dict[str, int] = {
            name: 0 for name in ("features", "matching", "depth", "merge")
        }
        stage_completed_keys: dict[str, list[str]] = {
            name: [] for name in ("features", "matching", "depth", "merge")
        }
        stage_completed_views: dict[str, list[int]] = {
            name: [] for name in ("features", "matching", "depth", "merge")
        }

        def union_completed_views() -> list[int]:
            values: set[int] = set()
            for name in stage_completed_views:
                values.update(stage_completed_views[name])
            return sorted(values)

        def write_snapshot(stage: str, stage_complete: bool) -> None:
            for name in stage_completed_keys:
                progress[name] = {
                    "completed_keys": sorted(set(stage_completed_keys[name])),
                    "completed_views": sorted(set(stage_completed_views[name])),
                }
            snapshot = snapshot_manager.write(
                snapshot_version=snapshot_version,
                config_hash=config_hash,
                camera_hash=manifest.camera_hash,
                input_hash=manifest.input_hash,
                injection_hash=injections.hash,
                stage=stage,
                stage_complete=stage_complete,
                progress=progress,
                completed_views=union_completed_views(),
                partial_pointcloud_summary=latest_partial_summary,
            )
            snapshot_writes.append(snapshot)

        def calculate_partial_summary(stage: str) -> dict[str, Any]:
            pair_payloads = [payload for _, payload in stage_payloads["matching"].values()]
            points = [
                point
                for payload in pair_payloads
                if not payload.get("business_code")
                for point in payload.get("points", [])
            ]
            point_tokens = [
                [point["point_id"], point["xyz"], point["source_views"]] for point in points
            ]
            depth_keys = sorted(stage_payloads["depth"].keys())
            summary = {
                "reconstructed_point_count": len(points),
                "source_pair_count": sum(
                    1 for payload in pair_payloads if not payload.get("business_code")
                ),
                "pair_payload_count": len(pair_payloads),
                "depth_payload_count": len(stage_payloads["depth"]),
                "digest": sha256_json(
                    {
                        "points": point_tokens,
                        "depth_keys": depth_keys,
                        "stage": stage,
                    }
                ),
                "finalized": False,
            }
            if stage == "merge":
                cloud_payloads = list(stage_payloads["merge"].values())
                if cloud_payloads:
                    cloud = cloud_payloads[0][1]
                    summary.update(
                        {
                            "voxel_count": int(cloud.get("voxel_count", 0)),
                            "cloud_digest": cloud.get("cloud_digest"),
                            "finalized": True,
                        }
                    )
            return summary

        def on_complete(task: Task, payload: Any, completion_count: int, total: int) -> None:
            nonlocal latest_partial_summary
            del completion_count, total
            stage_payloads[task.stage][task.key] = (task, payload)
            stage_completed_keys[task.stage] = sorted(
                set(stage_completed_keys[task.stage]) | {task.key}
            )
            views = set(stage_completed_views[task.stage])
            if task.view_index is not None:
                views.add(task.view_index)
            views.update(int(value) for value in task.metadata.get("completed_views", []))
            stage_completed_views[task.stage] = sorted(views)
            completed_view_count = len(stage_completed_views[task.stage])
            if completed_view_count - snapshot_view_checkpoints[task.stage] >= interval:
                snapshot_view_checkpoints[task.stage] = completed_view_count
                latest_partial_summary = calculate_partial_summary(task.stage)
                write_snapshot(task.stage, False)

        def run_stage(stage: str, tasks: list[Task], max_workers: int) -> TaskBatchResult:
            stage_payloads[stage] = {}
            stage_completed_keys[stage] = []
            stage_completed_views[stage] = []
            snapshot_view_checkpoints[stage] = 0
            return execute_tasks(
                tasks,
                max_workers=max_workers,
                stop_event=self.stop_event,
                on_complete=on_complete,
                test_delay_ms=self.test_delay_ms,
            )

        def finish_stage(stage: str, batch: TaskBatchResult) -> None:
            nonlocal latest_partial_summary
            stage_completed_keys[stage] = sorted(set(batch.completed_keys))
            stage_completed_views[stage] = sorted(set(batch.completed_views))
            latest_partial_summary = calculate_partial_summary(stage)
            failures.extend(batch.failures)
            write_snapshot(stage, not batch.interrupted)

        def stage_exception_for_view(view_index: int, stage: str) -> InjectionItem | None:
            for item in injections.items:
                if (
                    item.type == "stage_exception"
                    and item.stage == stage
                    and item.view == view_index
                ):
                    return item
            return None

        def stage_exception_for_pair(pair: tuple[int, int], stage: str) -> InjectionItem | None:
            canonical = (min(pair), max(pair))
            for item in injections.items:
                if item.type != "stage_exception" or item.stage != stage:
                    continue
                if item.pair == canonical or (item.view is not None and item.view in canonical):
                    return item
            return None

        # Stage 1: feature extraction.
        feature_tasks: list[Task] = []
        feature_cache_keys: dict[int, str] = {}
        for view in sorted(manifest.views, key=lambda item: item.view_index):
            key = feature_cache_key(
                image_hash=view.image_hash,
                camera_hash=view.camera.hash,
                feature_params=config["feature_detection"],
                seed=int(config["seed"]),
                view_index=view.view_index,
                injection_hash=injections.hash,
            )
            feature_cache_keys[view.view_index] = key
            path = store.path_for("features", key, f"view_{view.view_index:04d}")
            exception_item = stage_exception_for_view(view.view_index, "features")
            nonfinite_item = injections.first_view(view.view_index, "nonfinite_descriptor")
            injection_ids = tuple(
                item.id for item in (exception_item, nonfinite_item) if item is not None
            )

            def make_feature_loader(cache_path: Path = path, cache_key: str = key) -> Callable[[], Any]:
                return lambda: store.load("features", cache_key, cache_path)

            def make_feature_calculator(
                current_view: ViewInput = view,
                cache_path: Path = path,
                cache_key: str = key,
                exception: InjectionItem | None = exception_item,
                nonfinite: InjectionItem | None = nonfinite_item,
            ) -> Callable[[], Any]:
                def calculate() -> Any:
                    if exception is not None:
                        raise BusinessRuleError(
                            Failure(
                                code=E_STAGE_EXCEPTION,
                                stage="features",
                                message="injected feature task exception",
                                view_index=current_view.view_index,
                                injection_ids=(exception.id,),
                                detail={"task": "feature_extraction"},
                            )
                        )
                    payload = extract_features(
                        current_view,
                        config["feature_detection"],
                        int(config["seed"]),
                    )
                    injected_nonfinite = False
                    if nonfinite is not None:
                        try:
                            inject_nonfinite_descriptor(payload)
                            injected_nonfinite = True
                        except ValueError:
                            payload["features"] = [
                                {
                                    "feature_id": f"F-V{current_view.view_index:04d}-000001",
                                    "view_index": current_view.view_index,
                                    "pixel": [0, 0],
                                    "response": float("nan"),
                                    "descriptor": [float("nan")],
                                }
                            ]
                            payload["feature_count"] = 1
                            injected_nonfinite = True
                    if injected_nonfinite or payload_has_nonfinite_features(payload):
                        ids = (nonfinite.id,) if nonfinite is not None else ()
                        raise BusinessRuleError(
                            Failure(
                                code=E_NONFINITE_FEATURE,
                                stage="features",
                                message="feature descriptor contains a non-finite value",
                                view_index=current_view.view_index,
                                injection_ids=ids,
                                detail={"task": "feature_extraction"},
                            )
                        )
                    payload["cache_key"] = cache_key
                    store.save("features", cache_key, cache_path, payload)
                    return payload

                return calculate

            feature_tasks.append(
                Task(
                    key=key,
                    sort_key=(view.view_index, "features"),
                    stage="features",
                    label=f"features-view-{view.view_index}",
                    loader=make_feature_loader(),
                    calculator=make_feature_calculator(),
                    view_index=view.view_index,
                    injection_ids=injection_ids,
                )
            )

        feature_batch = run_stage("features", feature_tasks, int(config["executor"]["max_workers"]))
        finish_stage("features", feature_batch)
        feature_counts = {
            int(payload["view_index"]): int(payload.get("feature_count", 0))
            for payload in feature_batch.payloads
        }
        if feature_batch.payloads:
            atomic_write_json(
                self.features_out_path,
                {
                    "schema_version": 1,
                    "config_hash": semantic_config_hash,
                    "views": sorted(
                        feature_batch.payloads, key=lambda payload: int(payload["view_index"])
                    ),
                },
            )
        if feature_batch.interrupted:
            return self._finish_run(
                status="interrupted",
                exit_code=0,
                config=config,
                config_hash=config_hash,
                injections=injections,
                manifest=manifest,
                pair_payloads=[],
                depth_payloads=[],
                cloud_payload=None,
                failures=failures,
                feature_counts=feature_counts,
                snapshot_manager=snapshot_manager,
                recovery_point=recovery_point,
                store=store,
                resumed=saved_snapshot is not None,
                snapshot_writes=snapshot_writes,
            )

        feature_by_view = {
            int(payload["view_index"]): payload for payload in feature_batch.payloads
        }
        ordered_feature_views = sorted(feature_by_view)
        pair_specs: dict[tuple[int, int], str] = {}
        for left, right in zip(ordered_feature_views, ordered_feature_views[1:]):
            canonical = (min(left, right), max(left, right))
            pair_specs[canonical] = "adjacent"
        for pair in config["view_matching"]["loop_pairs"]:
            canonical = (min(pair), max(pair))
            if canonical[0] in feature_by_view and canonical[1] in feature_by_view:
                if canonical in pair_specs:
                    pair_specs[canonical] = "adjacent+loop"
                else:
                    pair_specs[canonical] = "loop"

        # Stage 2: matching and triangulation.
        pair_tasks: list[Task] = []
        for pair, pair_kind in sorted(pair_specs.items()):
            first, second = pair
            first_feature = feature_by_view[first]
            second_feature = feature_by_view[second]
            pair_id = pair_id_for(first, second)
            key = pair_cache_key(
                pair_id=pair_id,
                feature_keys=[
                    feature_cache_keys[first],
                    feature_cache_keys[second],
                ],
                camera_hashes=[
                    next(view.camera.hash for view in manifest.views if view.view_index == first),
                    next(view.camera.hash for view in manifest.views if view.view_index == second),
                ],
                matching_params=config["view_matching"],
                triangulation_params=config["triangulation"],
                injection_hash=injections.hash,
            )
            path = store.path_for("pairs", key, pair_id)
            exception_item = stage_exception_for_pair(pair, "matching")
            low_parallax_item = injections.first_pair(pair, "low_parallax")
            injection_ids = tuple(
                item.id
                for item in (exception_item, low_parallax_item)
                if item is not None
            )

            def make_pair_loader(cache_path: Path = path, cache_key: str = key) -> Callable[[], Any]:
                return lambda: store.load("pairs", cache_key, cache_path)

            def make_pair_calculator(
                pair_value: tuple[int, int] = pair,
                pair_kind_value: str = pair_kind,
                pair_cache_key_value: str = key,
                cache_path: Path = path,
                first_feature_value: dict[str, Any] = first_feature,
                second_feature_value: dict[str, Any] = second_feature,
                exception: InjectionItem | None = exception_item,
                low_parallax: InjectionItem | None = low_parallax_item,
            ) -> Callable[[], Any]:
                def calculate() -> Any:
                    if exception is not None:
                        raise BusinessRuleError(
                            Failure(
                                code=E_STAGE_EXCEPTION,
                                stage="matching",
                                message="injected matching task exception",
                                pair=pair_value,
                                injection_ids=(exception.id,),
                                detail={"task": "matching_triangulation"},
                            )
                        )
                    first_camera = next(
                        view.camera for view in manifest.views if view.view_index == pair_value[0]
                    )
                    second_camera = next(
                        view.camera for view in manifest.views if view.view_index == pair_value[1]
                    )
                    payload = match_and_triangulate_pair(
                        feature_a=first_feature_value,
                        feature_b=second_feature_value,
                        camera_a=first_camera,
                        camera_b=second_camera,
                        matching_params=config["view_matching"],
                        triangulation_params=config["triangulation"],
                        feature_key_a=feature_cache_keys[pair_value[0]],
                        feature_key_b=feature_cache_keys[pair_value[1]],
                    )
                    payload["pair_kind"] = pair_kind_value
                    payload["cache_key"] = pair_cache_key_value
                    if low_parallax is not None:
                        payload["status"] = "insufficient"
                        payload["business_code"] = E_MATCH_INSUFFICIENT
                        payload["points"] = []
                        payload["injection_ids"] = [low_parallax.id]
                        payload["failure_detail"] = {
                            "reason": "injected_parallax_below_minimum",
                            "injection_id": low_parallax.id,
                            "descriptor_match_count": payload["descriptor_match_count"],
                            "inlier_count": payload["inlier_count"],
                            "minimum_inliers": payload["minimum_inliers"],
                        }
                    store.save("pairs", pair_cache_key_value, cache_path, payload)
                    return payload

                return calculate

            pair_tasks.append(
                Task(
                    key=key,
                    sort_key=(pair[0], pair[1], "matching"),
                    stage="matching",
                    label=f"pair-{pair[0]}-{pair[1]}",
                    loader=make_pair_loader(),
                    calculator=make_pair_calculator(),
                    pair=pair,
                    injection_ids=injection_ids,
                    metadata={"completed_views": [pair[0], pair[1]]},
                )
            )

        pair_batch = run_stage("matching", pair_tasks, int(config["executor"]["max_workers"]))
        finish_stage("matching", pair_batch)
        pair_payloads = sorted(
            pair_batch.payloads, key=lambda payload: tuple(payload["view_indices"])
        )
        if pair_payloads:
            atomic_write_json(
                self.matches_out_path,
                {
                    "schema_version": 1,
                    "config_hash": semantic_config_hash,
                    "pairs": pair_payloads,
                },
            )
        for payload in pair_payloads:
            if payload.get("business_code") == E_MATCH_INSUFFICIENT:
                failures.append(
                    Failure(
                        code=E_MATCH_INSUFFICIENT,
                        stage="matching",
                        message="matching pair has fewer inliers than required",
                        pair=tuple(payload["view_indices"]),
                        injection_ids=tuple(payload.get("injection_ids", [])),
                        detail=dict(payload.get("failure_detail", {})),
                    )
                )
        if pair_batch.interrupted:
            return self._finish_run(
                status="interrupted",
                exit_code=0,
                config=config,
                config_hash=config_hash,
                injections=injections,
                manifest=manifest,
                pair_payloads=pair_payloads,
                depth_payloads=[],
                cloud_payload=None,
                failures=failures,
                feature_counts=feature_counts,
                snapshot_manager=snapshot_manager,
                recovery_point=recovery_point,
                store=store,
                resumed=saved_snapshot is not None,
                snapshot_writes=snapshot_writes,
            )

        # Stage 3: depth layers.
        depth_tasks: list[Task] = []
        view_lookup = {view.view_index: view for view in manifest.views}
        for view_index in ordered_feature_views:
            view = view_lookup[view_index]
            contributing_pairs = [
                payload
                for payload in pair_payloads
                if view_index in payload["view_indices"] and not payload.get("business_code")
            ]
            pair_keys_for_view = sorted(str(payload["cache_key"]) for payload in contributing_pairs)
            source_points = [
                point for payload in contributing_pairs for point in payload.get("points", [])
            ]
            key = depth_cache_key(
                view_index=view_index,
                camera_hash=view.camera.hash,
                contributing_pair_keys=pair_keys_for_view,
                depth_params=config["depth_map"],
                triangulation_params=config["triangulation"],
                image_size=(view.color.shape[1], view.color.shape[0]),
                injection_hash=injections.hash,
            )
            path = store.path_for("depth", key, f"view_{view_index:04d}")
            exception_item = stage_exception_for_view(view_index, "depth")
            injection_ids = (exception_item.id,) if exception_item is not None else ()

            def make_depth_loader(cache_path: Path = path, cache_key: str = key) -> Callable[[], Any]:
                return lambda: store.load("depth", cache_key, cache_path)

            def make_depth_calculator(
                current_view: ViewInput = view,
                contributing: list[dict[str, Any]] = source_points,
                pair_keys: list[str] = pair_keys_for_view,
                cache_path: Path = path,
                cache_key: str = key,
                exception: InjectionItem | None = exception_item,
            ) -> Callable[[], Any]:
                def calculate() -> Any:
                    if exception is not None:
                        raise BusinessRuleError(
                            Failure(
                                code=E_STAGE_EXCEPTION,
                                stage="depth",
                                message="injected depth-map task exception",
                                view_index=current_view.view_index,
                                injection_ids=(exception.id,),
                                detail={"task": "depth_map"},
                            )
                        )
                    payload = generate_depth_map(
                        view_index=current_view.view_index,
                        camera=current_view.camera,
                        source_points=contributing,
                        depth_params=config["depth_map"],
                        triangulation_params=config["triangulation"],
                        image_size=(
                            current_view.color.shape[1],
                            current_view.color.shape[0],
                        ),
                    )
                    payload["source_pair_keys"] = pair_keys
                    payload["cache_key"] = cache_key
                    store.save("depth", cache_key, cache_path, payload)
                    return payload

                return calculate

            depth_tasks.append(
                Task(
                    key=key,
                    sort_key=(view_index, "depth"),
                    stage="depth",
                    label=f"depth-view-{view_index}",
                    loader=make_depth_loader(),
                    calculator=make_depth_calculator(),
                    view_index=view_index,
                    injection_ids=injection_ids,
                )
            )

        depth_batch = run_stage("depth", depth_tasks, int(config["executor"]["max_workers"]))
        finish_stage("depth", depth_batch)
        depth_payloads = sorted(depth_batch.payloads, key=lambda payload: int(payload["view_index"]))
        if depth_payloads:
            atomic_write_json(
                self.depth_out_path,
                {
                    "schema_version": 1,
                    "config_hash": semantic_config_hash,
                    "layers": depth_payloads,
                },
            )
        if depth_batch.interrupted:
            return self._finish_run(
                status="interrupted",
                exit_code=0,
                config=config,
                config_hash=config_hash,
                injections=injections,
                manifest=manifest,
                pair_payloads=pair_payloads,
                depth_payloads=depth_payloads,
                cloud_payload=None,
                failures=failures,
                feature_counts=feature_counts,
                snapshot_manager=snapshot_manager,
                recovery_point=recovery_point,
                store=store,
                resumed=saved_snapshot is not None,
                snapshot_writes=snapshot_writes,
            )

        # Stage 4: voxel-level merge.
        cloud_key = cloud_cache_key(
            pair_keys=sorted(str(payload["cache_key"]) for payload in pair_payloads),
            depth_keys=sorted(str(payload["cache_key"]) for payload in depth_payloads),
            pointcloud_params=config["pointcloud"],
            injection_hash=injections.hash,
        )
        cloud_path = store.path_for("cloud", cloud_key, "global")
        merge_exception = next(
            (
                item
                for item in injections.items
                if item.type == "stage_exception" and item.stage == "merge"
            ),
            None,
        )

        def merge_loader() -> Any:
            return store.load("cloud", cloud_key, cloud_path)

        def merge_calculator() -> Any:
            if merge_exception is not None:
                raise BusinessRuleError(
                    Failure(
                        code=E_STAGE_EXCEPTION,
                        stage="merge",
                        message="injected point-cloud merge exception",
                        injection_ids=(merge_exception.id,),
                        detail={"task": "voxel_merge"},
                    )
                )
            payload = merge_global_cloud(
                pair_payloads,
                float(config["pointcloud"]["voxel_size"]),
            )
            payload["source_pair_keys"] = sorted(
                str(pair_payload["cache_key"]) for pair_payload in pair_payloads
            )
            payload["source_depth_keys"] = sorted(
                str(depth_payload["cache_key"]) for depth_payload in depth_payloads
            )
            payload["config_hash"] = semantic_config_hash
            store.save("cloud", cloud_key, cloud_path, payload)
            return payload

        merge_task = Task(
            key=cloud_key,
            sort_key=(0, "merge"),
            stage="merge",
            label="global-voxel-merge",
            loader=merge_loader,
            calculator=merge_calculator,
            injection_ids=(merge_exception.id,) if merge_exception is not None else (),
        )
        merge_batch = run_stage("merge", [merge_task], int(config["executor"]["max_workers"]))
        finish_stage("merge", merge_batch)
        cloud_payload = merge_batch.payloads[0] if merge_batch.payloads else None
        merge_interrupted = merge_batch.interrupted and cloud_payload is None
        if cloud_payload is not None:
            atomic_write_json(self.cloud_out_path, cloud_payload)
        if merge_interrupted:
            return self._finish_run(
                status="interrupted",
                exit_code=0,
                config=config,
                config_hash=config_hash,
                injections=injections,
                manifest=manifest,
                pair_payloads=pair_payloads,
                depth_payloads=depth_payloads,
                cloud_payload=None,
                failures=failures,
                feature_counts=feature_counts,
                snapshot_manager=snapshot_manager,
                recovery_point=recovery_point,
                store=store,
                resumed=saved_snapshot is not None,
                snapshot_writes=snapshot_writes,
            )

        if cloud_payload is None:
            failures.append(
                Failure(
                    code=E_STAGE_EXCEPTION,
                    stage="merge",
                    message="global point-cloud merge did not produce an artifact",
                )
            )
        return self._finish_run(
            status="completed",
            exit_code=0,
            config=config,
            config_hash=config_hash,
            injections=injections,
            manifest=manifest,
            pair_payloads=pair_payloads,
            depth_payloads=depth_payloads,
            cloud_payload=cloud_payload,
            failures=failures,
            feature_counts=feature_counts,
            snapshot_manager=snapshot_manager,
            recovery_point=recovery_point,
            store=store,
            resumed=saved_snapshot is not None,
            snapshot_writes=snapshot_writes,
        )

    def _finish_run(
        self,
        *,
        status: str,
        exit_code: int,
        config: dict[str, Any],
        config_hash: str,
        injections: InjectionSet,
        manifest: ViewManifest,
        pair_payloads: list[dict[str, Any]],
        depth_payloads: list[dict[str, Any]],
        cloud_payload: dict[str, Any] | None,
        failures: list[Failure],
        feature_counts: dict[int, int],
        snapshot_manager: SnapshotManager,
        recovery_point: dict[str, Any],
        store: ArtifactStore,
        resumed: bool,
        snapshot_writes: list[dict[str, Any]],
    ) -> RunOutcome:
        semantic_config_hash = semantic_config_fingerprint(config)
        sorted_failure_records = sort_failures(failures)
        latest_snapshot = snapshot_writes[-1] if snapshot_writes else None
        if latest_snapshot is None:
            latest_snapshot = snapshot_manager.load_latest()
        result = self._build_result(
            config=config,
            config_hash=config_hash,
            manifest=manifest,
            pair_payloads=pair_payloads,
            depth_payloads=depth_payloads,
            cloud_payload=cloud_payload,
            failures=sorted_failure_records,
            feature_counts=feature_counts,
            injections=injections,
        )
        injection_hits = self._injection_hits(injections, sorted_failure_records)
        report = {
            "schema_version": 1,
            "status": status,
            "exit_code": exit_code,
            "snapshot_version": result["snapshot_version"],
            "business_codes": result["business_codes"],
            "failures": result["failures"],
            "views": result["views"],
            "match_pairs": result["match_pairs"],
            "match_pair_count": result["match_pair_count"],
            "reconstructed_point_count": result["reconstructed_point_count"],
            "voxel_count": result["voxel_count"],
            "cloud_digest": result["cloud_digest"],
            "result_digest": result["result_digest"],
            "hashes": {
                "config": config_hash,
                "semantic_config": semantic_config_hash,
                "camera": manifest.camera_hash,
                "input": manifest.input_hash,
                "injection": injections.hash,
            },
            "run": {
                "resumed": resumed,
                "recovery_point": recovery_point,
                "cache": store.report(),
                "snapshot_writes": [int(snapshot["sequence"]) for snapshot in snapshot_writes],
                "latest_snapshot_sequence": (
                    int(latest_snapshot.get("sequence", 0)) if latest_snapshot else 0
                ),
                "latest_snapshot_stage": latest_snapshot.get("stage") if latest_snapshot else None,
                "interrupted": status == "interrupted",
            },
            "injection": {
                "profile": injections.profile,
                "selected_items": [
                    {
                        "id": item.id,
                        "type": item.type,
                        "view": item.view,
                        "pair": list(item.pair) if item.pair else None,
                        "stage": item.stage,
                    }
                    for item in injections.items
                ],
                "hits": injection_hits,
            },
            "result": result,
            "artifacts": {
                "report": str(self.report_path),
                "cloud": str(self.cloud_out_path) if cloud_payload is not None else None,
                "depth_layers": str(self.depth_out_path) if depth_payloads else None,
                "features": (
                    str(self.features_out_path)
                    if any(count > 0 for count in feature_counts.values())
                    else None
                ),
                "matches": str(self.matches_out_path) if pair_payloads else None,
            },
        }
        atomic_write_json(self.report_path, report)
        return RunOutcome(
            status=status,
            exit_code=exit_code,
            report=report,
            cloud=cloud_payload,
        )

    def _build_result(
        self,
        *,
        config: dict[str, Any],
        config_hash: str,
        manifest: ViewManifest,
        pair_payloads: list[dict[str, Any]],
        depth_payloads: list[dict[str, Any]],
        cloud_payload: dict[str, Any] | None,
        failures: list[Failure],
        feature_counts: dict[int, int],
        injections: InjectionSet,
    ) -> dict[str, Any]:
        depth_by_view = {
            int(payload["view_index"]): payload for payload in depth_payloads
        }
        pair_by_view: dict[int, list[dict[str, Any]]] = {}
        for payload in pair_payloads:
            for view_index in payload["view_indices"]:
                pair_by_view.setdefault(int(view_index), []).append(payload)
        invalid_views = {
            failure.view_index
            for failure in failures
            if failure.code == E_VIEW_INVALID and failure.view_index is not None
        }
        view_records: list[dict[str, Any]] = []
        for view_index in sorted(manifest.discovered_indices):
            view_pairs = pair_by_view.get(view_index, [])
            successful_pairs = [
                payload for payload in view_pairs if not payload.get("business_code")
            ]
            reconstructed = sum(
                len(payload.get("points", [])) for payload in successful_pairs
            )
            depth_payload = depth_by_view.get(view_index)
            if view_index in invalid_views:
                view_status = "invalid"
            elif view_index in feature_counts:
                view_status = "ok"
            else:
                view_status = "failed"
            view_records.append(
                {
                    "view_index": view_index,
                    "status": view_status,
                    "feature_count": int(feature_counts.get(view_index, 0)),
                    "match_attempt_count": len(view_pairs),
                    "match_pair_count": len(successful_pairs),
                    "reconstructed_point_count": reconstructed,
                    "depth_nonzero_cells": int(
                        depth_payload.get("nonzero_cells", 0) if depth_payload else 0
                    ),
                    "business_codes": sorted(
                        {
                            failure.code
                            for failure in failures
                            if failure.view_index == view_index
                            or (failure.pair is not None and view_index in failure.pair)
                        }
                    ),
                }
            )
        pair_summaries = [
            {
                "pair_id": payload["pair_id"],
                "view_indices": list(payload["view_indices"]),
                "pair_kind": payload.get("pair_kind"),
                "status": payload.get("status"),
                "descriptor_match_count": int(payload.get("descriptor_match_count", 0)),
                "inlier_count": int(payload.get("inlier_count", 0)),
                "reconstructed_point_count": len(payload.get("points", [])),
                "business_code": payload.get("business_code"),
            }
            for payload in pair_payloads
        ]
        depth_summaries = [
            {
                "view_index": int(payload["view_index"]),
                "resolution": list(payload["resolution"]),
                "projected_point_count": int(payload.get("projected_point_count", 0)),
                "unique_depth_cells": int(payload.get("unique_depth_cells", 0)),
                "nonzero_cells": int(payload.get("nonzero_cells", 0)),
            }
            for payload in depth_payloads
        ]
        depth_digest = sha256_json(
            [
                {
                    "view_index": payload["view_index"],
                    "depth_map": payload["depth_map"],
                }
                for payload in depth_payloads
            ]
        )
        business_codes = sorted({failure.code for failure in failures})
        result = {
            "complete": cloud_payload is not None,
            "snapshot_version": int(config["snapshot"]["version"]),
            "config_hash": semantic_config_fingerprint(config),
            "camera_hash": manifest.camera_hash,
            "input_hash": manifest.input_hash,
            "definitions": {
                "per_view_reconstructed_point_count": (
                    "sum of points in successful pairs that contain that view; "
                    "a point is counted once for each of its two source views"
                ),
                "per_view_depth_nonzero_cells": (
                    "non-background cells in the final quantized, smoothed depth layer"
                ),
            },
            "views": view_records,
            "match_pairs": pair_summaries,
            "depth_layers": depth_summaries,
            "match_pair_count": len(pair_payloads),
            "successful_match_pair_count": sum(
                1 for payload in pair_payloads if not payload.get("business_code")
            ),
            "reconstructed_point_count": sum(
                len(payload.get("points", []))
                for payload in pair_payloads
                if not payload.get("business_code")
            ),
            "voxel_count": int(cloud_payload.get("voxel_count", 0)) if cloud_payload else 0,
            "cloud_digest": cloud_payload.get("cloud_digest") if cloud_payload else None,
            "depth_layers_digest": depth_digest,
            "failures": [failure.to_dict() for failure in failures],
            "business_codes": business_codes,
            "injection_hits": self._injection_hits(injections, failures),
        }
        result["result_digest"] = sha256_json(result)
        return result

    @staticmethod
    def _injection_hits(
        injections: InjectionSet,
        failures: list[Failure],
    ) -> list[dict[str, Any]]:
        by_id: dict[str, list[dict[str, Any]]] = {}
        for failure in failures:
            for injection_id in failure.injection_ids:
                by_id.setdefault(injection_id, []).append(failure.to_dict())
        item_by_id = {item.id: item for item in injections.items}
        hits: list[dict[str, Any]] = []
        for item in injections.items:
            occurrences = by_id.get(item.id, [])
            if not occurrences:
                continue
            hits.append(
                {
                    "id": item.id,
                    "type": item.type,
                    "hit": True,
                    "occurrences": sorted(
                        occurrences,
                        key=lambda record: (
                            record.get("stage", ""),
                            record.get("view_index", -1),
                            tuple(record.get("pair", [])),
                            record.get("code", ""),
                        ),
                    ),
                }
            )
        return hits

    def _config_invalid_outcome(
        self,
        raw_config: dict[str, Any] | None,
        config: dict[str, Any] | None,
        *,
        injections: InjectionSet | None,
        message: str,
        detail: dict[str, Any],
        issues_fields: list[str] | None = None,
    ) -> RunOutcome:
        injection_ids: list[str] = []
        if injections is not None:
            for field in issues_fields or []:
                injection_ids.extend(injection_ids_for_config_issue(field, injections))
            if not issues_fields:
                injection_ids.extend(
                    item.id
                    for item in injections.items
                    if item.type in {"zero_voxel_size", "invalid_budget"}
                )
        failure = Failure(
            code=E_CONFIG_INVALID,
            stage="config",
            message=message,
            injection_ids=tuple(sorted(set(injection_ids))),
            detail=detail,
        )
        raw_hash = sha256_json(
            raw_config if raw_config is not None else {"path": str(self.config_path)}
        )
        snapshot_version_value: int | None = None
        if isinstance(raw_config, dict):
            snapshot_section = raw_config.get("snapshot")
            if isinstance(snapshot_section, dict) and isinstance(snapshot_section.get("version"), int):
                snapshot_version_value = int(snapshot_section["version"])
        result = {
            "complete": False,
            "snapshot_version": snapshot_version_value,
            "config_hash": raw_hash,
            "camera_hash": None,
            "input_hash": None,
            "definitions": {},
            "views": [],
            "match_pairs": [],
            "depth_layers": [],
            "match_pair_count": 0,
            "successful_match_pair_count": 0,
            "reconstructed_point_count": 0,
            "voxel_count": 0,
            "cloud_digest": None,
            "depth_layers_digest": None,
            "failures": [failure.to_dict()],
            "business_codes": [E_CONFIG_INVALID],
            "injection_hits": (
                self._injection_hits(injections, [failure]) if injections is not None else []
            ),
        }
        result["result_digest"] = sha256_json(result)
        report = {
            "schema_version": 1,
            "status": "config_invalid",
            "exit_code": 2,
            "snapshot_version": result["snapshot_version"],
            "business_codes": result["business_codes"],
            "failures": result["failures"],
            "views": result["views"],
            "match_pairs": result["match_pairs"],
            "match_pair_count": result["match_pair_count"],
            "reconstructed_point_count": result["reconstructed_point_count"],
            "voxel_count": result["voxel_count"],
            "cloud_digest": result["cloud_digest"],
            "result_digest": result["result_digest"],
            "hashes": {
                "config": raw_hash,
                "camera": None,
                "input": None,
                "injection": injections.hash if injections is not None else None,
            },
            "run": {
                "resumed": False,
                "recovery_point": None,
                "cache": None,
                "snapshot_writes": [],
                "latest_snapshot_sequence": 0,
                "latest_snapshot_stage": None,
                "interrupted": False,
            },
            "injection": {
                "profile": injections.profile if injections is not None else None,
                "selected_items": (
                    [
                        {
                            "id": item.id,
                            "type": item.type,
                            "view": item.view,
                            "pair": list(item.pair) if item.pair else None,
                            "stage": item.stage,
                        }
                        for item in injections.items
                    ]
                    if injections is not None
                    else []
                ),
                "hits": (
                    self._injection_hits(injections, [failure])
                    if injections is not None
                    else []
                ),
            },
            "result": result,
            "artifacts": {
                "report": str(self.report_path),
                "cloud": None,
                "depth_layers": None,
                "features": None,
                "matches": None,
            },
        }
        atomic_write_json(self.report_path, report)
        return RunOutcome(status="config_invalid", exit_code=2, report=report)

    def _config_mismatch_outcome(
        self,
        *,
        config: dict[str, Any],
        injections: InjectionSet,
        mismatch: SnapshotMismatch,
    ) -> RunOutcome:
        failure = Failure(
            code=E_CONFIG_MISMATCH,
            stage="snapshot",
            message="latest snapshot cannot be resumed with the current configuration",
            detail={
                "reason": mismatch.reason,
                "latest_sequence": mismatch.latest_sequence,
                "expected_snapshot_version": mismatch.expected_version,
                "found_snapshot_version": mismatch.found_version,
                "expected_config_hash": mismatch.expected_config_hash,
                "found_config_hash": mismatch.found_config_hash,
            },
        )
        result = {
            "complete": False,
            "snapshot_version": int(config["snapshot"]["version"]),
            "config_hash": semantic_config_fingerprint(config),
            "camera_hash": None,
            "input_hash": None,
            "definitions": {},
            "views": [],
            "match_pairs": [],
            "depth_layers": [],
            "match_pair_count": 0,
            "successful_match_pair_count": 0,
            "reconstructed_point_count": 0,
            "voxel_count": 0,
            "cloud_digest": None,
            "depth_layers_digest": None,
            "failures": [failure.to_dict()],
            "business_codes": [E_CONFIG_MISMATCH],
            "injection_hits": [],
        }
        result["result_digest"] = sha256_json(result)
        report = {
            "schema_version": 1,
            "status": "rejected",
            "exit_code": 3,
            "snapshot_version": result["snapshot_version"],
            "business_codes": result["business_codes"],
            "failures": result["failures"],
            "views": result["views"],
            "match_pairs": result["match_pairs"],
            "match_pair_count": result["match_pair_count"],
            "reconstructed_point_count": result["reconstructed_point_count"],
            "voxel_count": result["voxel_count"],
            "cloud_digest": result["cloud_digest"],
            "result_digest": result["result_digest"],
            "hashes": {
                "config": config_fingerprint(config),
                "camera": None,
                "input": None,
                "injection": injections.hash,
            },
            "run": {
                "resumed": False,
                "recovery_point": {
                    "mode": "rejected",
                    "snapshot_sequence": mismatch.latest_sequence,
                    "stage": None,
                    "stage_complete": False,
                    "completed_views": [],
                },
                "cache": None,
                "snapshot_writes": [],
                "latest_snapshot_sequence": mismatch.latest_sequence,
                "latest_snapshot_stage": None,
                "interrupted": False,
            },
            "injection": {
                "profile": injections.profile,
                "selected_items": [],
                "hits": [],
            },
            "result": result,
            "artifacts": {
                "report": str(self.report_path),
                "cloud": None,
                "depth_layers": None,
                "features": None,
                "matches": None,
            },
        }
        atomic_write_json(self.report_path, report)
        return RunOutcome(status="rejected", exit_code=3, report=report)
