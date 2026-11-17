"""Independent fault-injection configuration selection and bookkeeping."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .canonical import read_json, sha256_json


SUPPORTED_INJECTION_TYPES = {
    "corrupt_png",
    "missing_camera",
    "low_parallax",
    "nonfinite_descriptor",
    "stage_exception",
    "zero_voxel_size",
    "invalid_budget",
}


@dataclass(frozen=True)
class InjectionItem:
    id: str
    type: str
    view: int | None = None
    pair: tuple[int, int] | None = None
    stage: str | None = None
    profiles: tuple[str, ...] = ()
    enabled: bool = True
    detail: dict[str, Any] | None = None

    def matches_view(self, view_index: int, *, types: Iterable[str] | None = None) -> bool:
        if self.view != view_index:
            return False
        return types is None or self.type in set(types)

    def matches_pair(self, pair: tuple[int, int], *, types: Iterable[str] | None = None) -> bool:
        if self.pair is None:
            return False
        canonical_pair = (min(pair), max(pair))
        if self.pair != canonical_pair:
            return False
        return types is None or self.type in set(types)


@dataclass(frozen=True)
class InjectionSet:
    path: str | None
    profile: str | None
    items: tuple[InjectionItem, ...]
    all_items: tuple[InjectionItem, ...]
    hash: str

    def first_view(self, view_index: int, injection_type: str) -> InjectionItem | None:
        for item in self.items:
            if item.type == injection_type and item.view == view_index:
                return item
        return None

    def first_pair(self, pair: tuple[int, int], injection_type: str) -> InjectionItem | None:
        canonical = (min(pair), max(pair))
        for item in self.items:
            if item.type == injection_type and item.pair == canonical:
                return item
        return None

    def of_type(self, injection_type: str) -> tuple[InjectionItem, ...]:
        return tuple(item for item in self.items if item.type == injection_type)


EMPTY_INJECTIONS = InjectionSet(
    path=None,
    profile=None,
    items=tuple(),
    all_items=tuple(),
    hash=sha256_json({"items": []}),
)


def _parse_item(raw: dict[str, Any], position: int) -> InjectionItem:
    item_id = str(raw.get("id", f"INJ-{position:03d}"))
    injection_type = str(raw.get("type", ""))
    if injection_type not in SUPPORTED_INJECTION_TYPES:
        raise ValueError(f"injection {item_id}: unsupported type {injection_type!r}")
    profiles_raw = raw.get("profiles", raw.get("profile", []))
    if isinstance(profiles_raw, str):
        profiles = (profiles_raw,)
    elif isinstance(profiles_raw, list):
        profiles = tuple(str(value) for value in profiles_raw)
    elif profiles_raw is None:
        profiles = tuple()
    else:
        raise ValueError(f"injection {item_id}: profiles must be a string or array")

    view = raw.get("view")
    if view is not None and (not isinstance(view, int) or isinstance(view, bool) or view < 1):
        raise ValueError(f"injection {item_id}: view must be a positive integer")

    pair_raw = raw.get("pair")
    pair = None
    if pair_raw is not None:
        if (
            not isinstance(pair_raw, list)
            or len(pair_raw) != 2
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in pair_raw)
            or pair_raw[0] == pair_raw[1]
        ):
            raise ValueError(f"injection {item_id}: pair must contain two distinct integers")
        pair = (min(pair_raw), max(pair_raw))

    stage = raw.get("stage")
    if stage is not None:
        stage = str(stage)
    if injection_type == "stage_exception" and not stage:
        raise ValueError(f"injection {item_id}: stage_exception requires stage")
    if injection_type in {"corrupt_png", "missing_camera", "nonfinite_descriptor"} and view is None:
        raise ValueError(f"injection {item_id}: requires view")
    if injection_type == "low_parallax" and pair is None:
        raise ValueError(f"injection {item_id}: requires pair")

    return InjectionItem(
        id=item_id,
        type=injection_type,
        view=view,
        pair=pair,
        stage=stage,
        profiles=profiles,
        enabled=bool(raw.get("enabled", True)),
        detail=dict(raw.get("detail", {})),
    )


def load_injections(
    path: Path | None,
    profile: str | None = None,
) -> InjectionSet:
    if path is None:
        return EMPTY_INJECTIONS
    raw = read_json(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ValueError("injection configuration must contain an items array")
    all_items = tuple(_parse_item(item, position) for position, item in enumerate(raw["items"], 1))
    identifiers = [item.id for item in all_items]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("injection item ids must be unique")

    selected: list[InjectionItem] = []
    for item in all_items:
        if not item.enabled:
            continue
        if profile is not None and item.profiles and profile not in item.profiles:
            continue
        selected.append(item)

    selected_tuple = tuple(selected)
    payload = {
        "profile": profile,
        "items": [
            {
                "id": item.id,
                "type": item.type,
                "view": item.view,
                "pair": list(item.pair) if item.pair else None,
                "stage": item.stage,
            }
            for item in selected_tuple
        ],
    }
    return InjectionSet(
        path=str(path),
        profile=profile,
        items=selected_tuple,
        all_items=all_items,
        hash=sha256_json(payload),
    )


def apply_config_injections(raw_config: dict[str, Any], injections: InjectionSet) -> dict[str, Any]:
    config = copy.deepcopy(raw_config)
    for item in injections.items:
        if item.type == "zero_voxel_size":
            config.setdefault("pointcloud", {})["voxel_size"] = 0.0
        elif item.type == "invalid_budget":
            config.setdefault("executor", {})["max_workers"] = 0
    return config


def injection_ids_for_config_issue(
    issue_field: str,
    injections: InjectionSet,
) -> tuple[str, ...]:
    ids: list[str] = []
    if issue_field.startswith("pointcloud.voxel_size"):
        ids.extend(item.id for item in injections.of_type("zero_voxel_size"))
    if issue_field.startswith("executor.max_workers"):
        ids.extend(item.id for item in injections.of_type("invalid_budget"))
    return tuple(ids)
