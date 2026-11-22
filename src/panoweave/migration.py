"""Cross-version migration, drop reports, drift, and round-trip checks."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

from .config import ParseDefaults
from .errors import BusinessError, ErrorCode
from .formats import parse_text, serialize_scene
from .geometry import derive_homography
from .model import (
    DistortionStage,
    ExposureCurve,
    MigrationItem,
    MigrationResult,
    ParsedScene,
    SceneParameters,
    ViewParameters,
    qfloat,
)


def _canonical_radial_chain(view: ViewParameters) -> tuple[DistortionStage, ...]:
    return (DistortionStage.radial(view.distortion_k1, view.distortion_k2),)


def _flatten_distortion_chain(
    view: ViewParameters, index: int
) -> tuple[tuple[DistortionStage, ...], list[MigrationItem], tuple[str, ...]]:
    items: list[MigrationItem] = []
    dropped: list[str] = []
    chain = view.distortion_chain
    radial_index = next(
        (stage_index for stage_index, stage in enumerate(chain) if stage.model == "radial"),
        None,
    )
    if radial_index is None:
        k1 = 0.0
        k2 = 0.0
        items.append(
            MigrationItem(
                "flattened",
                f"views[{index}].distortion_chain",
                "v3",
                "no radial stage exists; v2/v1 radial coefficients become zero",
                {"k1": 0.0, "k2": 0.0},
            )
        )
    else:
        stage = chain[radial_index]
        k1 = stage.param("k1")
        k2 = stage.param("k2")
        if len(chain) > 1:
            items.append(
                MigrationItem(
                    "flattened",
                    f"views[{index}].distortion_chain",
                    "v3",
                    "first radial stage retained; all other stages dropped",
                    {"k1": k1, "k2": k2},
                )
            )
    for stage_index, stage in enumerate(chain):
        if stage_index == radial_index:
            continue
        path = f"views[{index}].distortion_chain[{stage_index}]"
        dropped.append(path)
        items.append(
            MigrationItem(
                "dropped",
                path,
                "v3",
                "v1/v2 cannot express a distortion model chain",
                stage.to_dict(),
            )
        )
    return (DistortionStage.radial(k1, k2),), items, tuple(dropped)


def _flatten_exposure_curve(
    view: ViewParameters, index: int
) -> tuple[ExposureCurve, list[MigrationItem], tuple[str, ...]]:
    items: list[MigrationItem] = []
    dropped: list[str] = []
    curve = view.exposure_curve
    if curve.mode == "unit":
        if curve.gain != 1.0:
            path = f"views[{index}].exposure_curve.gain"
            dropped.append(path)
            items.append(
                MigrationItem(
                    "flattened",
                    path,
                    "v3",
                    "unit exposure mode ignores gain; flattened gain is one",
                    curve.gain,
                )
            )
        return ExposureCurve("unit", 1.0, 1.0), items, tuple(dropped)
    if curve.gamma != 1.0:
        path = f"views[{index}].exposure_curve.gamma"
        dropped.append(path)
        items.append(
            MigrationItem(
                "dropped",
                path,
                "v3",
                "v1/v2 expose only scalar gain and cannot express gamma",
                curve.gamma,
            )
        )
    if curve.params:
        path = f"views[{index}].exposure_curve.params"
        dropped.append(path)
        items.append(
            MigrationItem(
                "dropped",
                path,
                "v3",
                "v1/v2 cannot express additional exposure-curve parameters",
                dict(curve.params),
            )
        )
    if curve.mode not in {"linear_gain", "unit"}:
        path = f"views[{index}].exposure_curve.mode"
        dropped.append(path)
        items.append(
            MigrationItem(
                "flattened",
                path,
                "v3",
                "unsupported curve mode is flattened to linear scalar gain",
                curve.mode,
            )
        )
    gain = curve.gain
    flattened = ExposureCurve("linear_gain", qfloat(gain), 1.0)
    return flattened, items, tuple(dropped)


def _target_v3(
    source: ParsedScene,
) -> tuple[SceneParameters, list[MigrationItem], tuple[str, ...]]:
    scene = source.scene
    items = list(source.completions)
    return scene, items, ()


def _target_v2(
    source: ParsedScene,
) -> tuple[SceneParameters, list[MigrationItem], tuple[str, ...]]:
    items: list[MigrationItem] = []
    dropped: list[str] = []
    if source.format_version == "v1":
        allowed = (
            ".homography",
            ".overlap_weight",
            ".exposure_mode",
            ".color_gain",
        )
        items.extend(
            item
            for item in source.completions
            if item.field_path in {"feather_radius", "color_space"}
            or any(item.field_path.endswith(suffix) for suffix in allowed)
        )
    views: list[ViewParameters] = []
    for index, view in enumerate(source.scene.sorted_views):
        chain = _canonical_radial_chain(view)
        curve = (
            view.exposure_curve
            if source.format_version == "v2"
            else ExposureCurve("linear_gain", view.exposure_gain, 1.0)
        )
        if source.format_version == "v3":
            chain, chain_items, chain_dropped = _flatten_distortion_chain(view, index)
            curve, curve_items, curve_dropped = _flatten_exposure_curve(view, index)
            items.extend(chain_items)
            items.extend(curve_items)
            dropped.extend(chain_dropped)
            dropped.extend(curve_dropped)
        views.append(
            replace(
                view,
                distortion_chain=chain,
                exposure_curve=curve,
                exposure_gain=curve.gain,
                exposure_mode="unit" if curve.mode == "unit" else "linear",
            )
        )
    scene = replace(source.scene, views=tuple(views))
    return scene, items, tuple(dropped)


def _target_v1(
    source: ParsedScene, defaults: ParseDefaults
) -> tuple[SceneParameters, list[MigrationItem], tuple[str, ...]]:
    items: list[MigrationItem] = []
    dropped: list[str] = []
    if source.format_version == "v1":
        return source.scene, items, ()
    if source.scene.canvas_width != defaults.canvas_width:
        items.append(
            MigrationItem(
                "dropped",
                "canvas_width",
                source.format_version,
                "v1 has no canvas declaration; parse context is authoritative",
                source.scene.canvas_width,
            )
        )
        dropped.append("canvas_width")
    if source.scene.canvas_height != defaults.canvas_height:
        items.append(
            MigrationItem(
                "dropped",
                "canvas_height",
                source.format_version,
                "v1 has no canvas declaration; parse context is authoritative",
                source.scene.canvas_height,
            )
        )
        dropped.append("canvas_height")
    if source.scene.feather_radius != qfloat(defaults.feather_radius):
        items.append(
            MigrationItem(
                "dropped",
                "feather_radius",
                source.format_version,
                "v1 has no fusion feather radius",
                source.scene.feather_radius,
            )
        )
        dropped.append("feather_radius")
    if source.scene.color_space != defaults.color_space:
        items.append(
            MigrationItem(
                "dropped",
                "color_space",
                source.format_version,
                "v1 has no color-space marker",
                source.scene.color_space,
            )
        )
        dropped.append("color_space")
    views: list[ViewParameters] = []
    for index, view in enumerate(source.scene.sorted_views):
        flattened_chain, chain_items, chain_dropped = _flatten_distortion_chain(view, index)
        flattened_curve, curve_items, curve_dropped = _flatten_exposure_curve(view, index)
        if source.format_version == "v3":
            items.extend(chain_items)
            items.extend(curve_items)
            dropped.extend(chain_dropped)
            dropped.extend(curve_dropped)
        derived_h = derive_homography(
            view.focal,
            view.principal_x,
            view.principal_y,
            view.extrinsic,
            defaults.canvas_width,
            defaults.canvas_height,
        )
        if view.homography != derived_h:
            items.append(
                MigrationItem(
                    "dropped",
                    f"views[{index}].homography",
                    source.format_version,
                    "v1 recomputes homography from camera parameters; explicit value is lost",
                    list(view.homography),
                )
            )
            dropped.append(f"views[{index}].homography")
        if view.overlap_weight != qfloat(defaults.overlap_weight):
            items.append(
                MigrationItem(
                    "dropped",
                    f"views[{index}].overlap_weight",
                    source.format_version,
                    "v1 has no overlap declaration",
                    view.overlap_weight,
                )
            )
            dropped.append(f"views[{index}].overlap_weight")
        if tuple(view.color_gain) != (1.0, 1.0, 1.0):
            items.append(
                MigrationItem(
                    "dropped",
                    f"views[{index}].color_gain",
                    source.format_version,
                    "v1 has only a scalar exposure gain",
                    list(view.color_gain),
                )
            )
            dropped.append(f"views[{index}].color_gain")
        if view.exposure_mode != "linear":
            items.append(
                MigrationItem(
                    "dropped",
                    f"views[{index}].exposure_mode",
                    source.format_version,
                    "v1 exposes only scalar gain",
                    view.exposure_mode,
                )
            )
            dropped.append(f"views[{index}].exposure_mode")
        views.append(
            replace(
                view,
                homography=derived_h,
                overlap_weight=qfloat(defaults.overlap_weight),
                color_gain=(1.0, 1.0, 1.0),
                distortion_chain=flattened_chain,
                exposure_curve=flattened_curve,
                exposure_gain=flattened_curve.gain,
                exposure_mode="linear",
            )
        )
    scene = SceneParameters(
        canvas_width=defaults.canvas_width,
        canvas_height=defaults.canvas_height,
        feather_radius=qfloat(defaults.feather_radius),
        color_space=defaults.color_space,
        views=tuple(views),
        interpolation=defaults.interpolation,
        weight_tolerance=defaults.weight_tolerance,
    )
    return scene, items, tuple(dropped)


def migrate_scene(
    source: ParsedScene,
    target_version: str,
    defaults: ParseDefaults | None = None,
) -> MigrationResult:
    defaults = defaults or ParseDefaults()
    if target_version not in {"v1", "v2", "v3"}:
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            f"unsupported target version {target_version!r}",
            stage="migration",
            field_path="target_version",
        )
    if target_version == "v1":
        scene, items, dropped = _target_v1(source, defaults)
    elif target_version == "v2":
        scene, items, dropped = _target_v2(source)
    else:
        scene, items, dropped = _target_v3(source)
    return MigrationResult(
        scene=scene,
        source_version=source.format_version,
        target_version=target_version,
        items=tuple(items),
        dropped_fields=tuple(dict.fromkeys(dropped)),
    )


def first_difference(left: Any, right: Any, path: str = "$") -> dict[str, Any] | None:
    if type(left) is not type(right):
        return {"path": path, "left": left, "right": right}
    if isinstance(left, dict):
        keys = sorted(set(left) | set(right))
        for key in keys:
            if key not in left:
                return {"path": f"{path}.{key}", "left": "<missing>", "right": right[key]}
            if key not in right:
                return {"path": f"{path}.{key}", "left": left[key], "right": "<missing>"}
            difference = first_difference(left[key], right[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return {"path": f"{path}.length", "left": len(left), "right": len(right)}
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            difference = first_difference(left_item, right_item, f"{path}[{index}]")
            if difference:
                return difference
        return None
    if isinstance(left, float):
        if left != right:
            return {"path": path, "left": left, "right": right}
        return None
    if left != right:
        return {"path": path, "left": left, "right": right}
    return None


def all_differences(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    if type(left) is not type(right):
        return [{"path": path, "left": left, "right": right}]
    if isinstance(left, dict):
        differences: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            if key not in left:
                differences.append(
                    {"path": f"{path}.{key}", "left": "<missing>", "right": right[key]}
                )
            elif key not in right:
                differences.append(
                    {"path": f"{path}.{key}", "left": left[key], "right": "<missing>"}
                )
            else:
                differences.extend(
                    all_differences(left[key], right[key], f"{path}.{key}")
                )
        return differences
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return [{"path": f"{path}.length", "left": len(left), "right": len(right)}]
        differences = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(
                all_differences(left_item, right_item, f"{path}[{index}]")
            )
        return differences
    return [] if left == right else [{"path": path, "left": left, "right": right}]


def representable_dict(
    scene: SceneParameters, version: str
) -> dict[str, Any]:
    """Fields that survive a write/read cycle in a given version."""

    base: dict[str, Any] = {"views": []}
    if version in {"v2", "v3"}:
        base.update(
            {
                "canvas_width": scene.canvas_width,
                "canvas_height": scene.canvas_height,
                "feather_radius": scene.feather_radius,
                "color_space": scene.color_space,
            }
        )
    for view in scene.sorted_views:
        item: dict[str, Any] = {
            "camera_id": view.camera_id,
            "focal": view.focal,
            "principal_x": view.principal_x,
            "principal_y": view.principal_y,
            "extrinsic": list(view.extrinsic),
            "distortion_k1": view.distortion_k1,
            "distortion_k2": view.distortion_k2,
            "exposure_gain": view.exposure_gain,
        }
        if version in {"v2", "v3"}:
            item.update(
                {
                    "homography": list(view.homography),
                    "overlap_weight": view.overlap_weight,
                    "exposure_mode": view.exposure_mode,
                    "color_gain": list(view.color_gain),
                }
            )
        if version == "v3":
            item["distortion_chain"] = [
                stage.to_dict() for stage in view.distortion_chain
            ]
            item["exposure_curve"] = view.exposure_curve.to_dict()
        base["views"].append(item)
    return base


def check_roundtrip(
    source: ParsedScene,
    via_version: str,
    defaults: ParseDefaults | None = None,
    *,
    raise_on_mismatch: bool = True,
) -> dict[str, Any]:
    defaults = defaults or ParseDefaults()
    outbound = migrate_scene(source, via_version, defaults)
    serialized = serialize_scene(outbound.scene, via_version)
    reparsed = parse_text(serialized, defaults, source_name=f"<roundtrip-{via_version}>")
    inbound = migrate_scene(reparsed, source.format_version, defaults)
    before = representable_dict(source.scene, via_version)
    after = representable_dict(inbound.scene, via_version)
    difference = first_difference(before, after)
    full_difference = first_difference(
        source.scene.semantic_dict(), inbound.scene.semantic_dict()
    )
    report = {
        "source_version": source.format_version,
        "via_version": via_version,
        "representable_equal": difference is None,
        "full_semantic_equal": full_difference is None,
        "first_difference": difference,
        "full_first_difference": full_difference,
        "intermediate_semantic_hash": outbound.scene.semantic_hash,
        "final_semantic_hash": inbound.scene.semantic_hash,
        "dropped_fields": list(outbound.dropped_fields),
    }
    if difference is not None and raise_on_mismatch:
        raise BusinessError(
            ErrorCode.ROUNDTRIP_MISMATCH,
            f"round-trip via {via_version} changed the first representable field at {difference['path']}",
            stage="migration",
            field_path=difference["path"],
            details={
                "source_version": source.format_version,
                "via_version": via_version,
                "left": difference["left"],
                "right": difference["right"],
                "report": report,
            },
        )
    return report


def assert_representable_equality(
    before: SceneParameters,
    after: SceneParameters,
    via_version: str,
    *,
    source_version: str = "unknown",
) -> dict[str, Any]:
    """Raise the stable round-trip error for a known field discrepancy."""

    left = representable_dict(before, via_version)
    right = representable_dict(after, via_version)
    difference = first_difference(left, right)
    if difference is not None:
        raise BusinessError(
            ErrorCode.ROUNDTRIP_MISMATCH,
            f"round-trip via {via_version} changed the first representable field at {difference['path']}",
            stage="migration",
            field_path=difference["path"],
            details={
                "source_version": source_version,
                "via_version": via_version,
                "left": difference["left"],
                "right": difference["right"],
            },
        )
    return {
        "representable_equal": True,
        "via_version": via_version,
        "source_version": source_version,
    }


def v1_inexpressible_fields(
    source: ParsedScene, defaults: ParseDefaults | None = None
) -> list[dict[str, Any]]:
    defaults = defaults or ParseDefaults()
    target = migrate_scene(source, "v1", defaults)
    return all_differences(
        source.scene.semantic_dict(), target.scene.semantic_dict()
    )


def assert_semantic_equivalence(
    sources: Sequence[ParsedScene],
    defaults: ParseDefaults | None = None,
) -> dict[str, Any]:
    defaults = defaults or ParseDefaults()
    if not sources:
        return {"equivalent": True, "semantic_hashes": {}}
    hashes = {source.format_version: source.scene.semantic_hash for source in sources}
    reference = sources[0].scene.semantic_dict()
    differences: list[dict[str, Any]] = []
    for source in sources[1:]:
        differences.extend(
            all_differences(reference, source.scene.semantic_dict(), "$")
        )
    if differences:
        v1_sources = [source for source in sources if source.format_version != "v1"]
        if any(source.format_version == "v1" for source in sources):
            drift_fields: list[str] = []
            for source in v1_sources:
                drift_fields.extend(
                    difference["path"]
                    for difference in v1_inexpressible_fields(source, defaults)
                )
            fields = sorted(set(drift_fields))
            if not fields:
                fields = sorted({difference["path"] for difference in differences})
            raise BusinessError(
                ErrorCode.SEMANTIC_DRIFT,
                "v1 cannot express all semantics in the supplied comparison",
                stage="migration",
                field_path=fields[0] if fields else None,
                details={
                    "unrecoverable_fields": fields,
                    "semantic_hashes": hashes,
                    "differences": differences[:50],
                },
            )
        raise BusinessError(
            ErrorCode.SEMANTIC_DRIFT,
            "versions do not resolve to the same internal parameters",
            stage="migration",
            field_path=differences[0]["path"],
            details={
                "semantic_hashes": hashes,
                "differences": differences[:50],
            },
        )
    return {
        "equivalent": True,
        "semantic_hash": sources[0].scene.semantic_hash,
        "semantic_hashes": hashes,
        "differences": [],
    }

