from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import fail
from .hashing import atomic_write_json, digest_obj
from .params import MaterialParams
from .plan import BlockPlan


def _params_snapshot(params: MaterialParams, version: int) -> dict[str, Any]:
    value = params.to_dict()
    return {"version": version, "checksum": digest_obj(value), "value": value}


def _plan_snapshot(plan: BlockPlan, version: int) -> dict[str, Any]:
    value = plan.to_dict()
    return {"version": version, "checksum": digest_obj(value), "value": value}


def _ready_snapshot(items: list[dict[str, Any]], version: int) -> dict[str, Any]:
    ordered = sorted(items, key=lambda item: item["block"])
    return {"version": version, "checksum": digest_obj(ordered), "items": ordered}


@dataclass
class Snapshot:
    params: dict[str, Any]
    plan: dict[str, Any]
    ready: dict[str, Any]
    checksum: str
    schema: str = "matforge.snapshot.v1"

    @classmethod
    def build(
        cls,
        params: MaterialParams,
        plan: BlockPlan,
        plan_version: int,
        ready_items: list[dict[str, Any]],
        params_version: int,
        ready_version: int,
    ) -> Snapshot:
        params_part = _params_snapshot(params, params_version)
        plan_part = _plan_snapshot(plan, plan_version)
        ready_part = _ready_snapshot(ready_items, ready_version)
        checksum = digest_obj({"params": params_part, "plan": plan_part, "ready": ready_part})
        return cls(params=params_part, plan=plan_part, ready=ready_part, checksum=checksum)

    def validate(self) -> None:
        if self.schema != "matforge.snapshot.v1":
            fail("E_SNAPSHOT_CORRUPT", "快照 schema 不匹配", schema=self.schema)
        if self.checksum != digest_obj({"params": self.params, "plan": self.plan, "ready": self.ready}):
            fail("E_SNAPSHOT_CORRUPT", "快照总校验和不符")
        if self.params.get("checksum") != digest_obj(self.params.get("value")):
            fail("E_SNAPSHOT_CORRUPT", "参数快照校验和不符")
        if self.plan.get("checksum") != digest_obj(self.plan.get("value")):
            fail("E_SNAPSHOT_CORRUPT", "分块计划快照校验和不符")
        if self.ready.get("checksum") != digest_obj(self.ready.get("items")):
            fail("E_SNAPSHOT_CORRUPT", "切片清单快照校验和不符")
        for name, part in (("params", self.params), ("plan", self.plan), ("ready", self.ready)):
            if not isinstance(part.get("version"), int) or part["version"] < 1:
                fail("E_SNAPSHOT_CORRUPT", "快照版本号无效", component=name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "params": self.params,
            "plan": self.plan,
            "ready": self.ready,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Snapshot:
        try:
            snapshot = cls(
                schema=value["schema"],
                params=value["params"],
                plan=value["plan"],
                ready=value["ready"],
                checksum=value["checksum"],
            )
        except (KeyError, TypeError) as error:
            fail("E_SNAPSHOT_CORRUPT", "快照结构不完整", reason=str(error))
        snapshot.validate()
        return snapshot

    @property
    def versions(self) -> dict[str, int]:
        return {
            "params": self.params["version"],
            "plan": self.plan["version"],
            "ready": self.ready["version"],
        }


class SnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._current: Snapshot | None = None

    def save(self, snapshot: Snapshot, *, reject_rollback: bool = True) -> dict[str, Any]:
        snapshot.validate()
        with self._lock:
            if reject_rollback and self._current is not None:
                old = self._current.versions
                new = snapshot.versions
                for component in ("params", "plan", "ready"):
                    if new[component] < old[component]:
                        fail(
                            "E_SNAPSHOT_ROLLBACK",
                            "快照回退被拒绝",
                            component=component,
                            current_version=old[component],
                            attempted_version=new[component],
                        )
                    if new[component] == old[component] and digest_obj(self._current.to_dict()[component]) != digest_obj(
                        snapshot.to_dict()[component]
                    ):
                        fail(
                            "E_SNAPSHOT_CORRUPT",
                            "同版本快照内容发生变化",
                            component=component,
                            version=new[component],
                        )
            atomic_write_json(self.path, snapshot.to_dict())
            self._current = snapshot
        return snapshot.versions

    def load(self, *, replace: bool = True) -> Snapshot:
        with self._lock:
            if not self.path.exists():
                fail("E_SNAPSHOT_MISSING", "快照文件不存在", path=str(self.path))
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                fail("E_SNAPSHOT_CORRUPT", "快照文件无法解析", path=str(self.path), reason=str(error))
            snapshot = Snapshot.from_dict(document)
            if replace and self._current is not None:
                old = self._current.versions
                new = snapshot.versions
                for component in ("params", "plan", "ready"):
                    if new[component] < old[component]:
                        fail(
                            "E_SNAPSHOT_ROLLBACK",
                            "加载快照会回退版本",
                            component=component,
                            current_version=old[component],
                            attempted_version=new[component],
                        )
                    if new[component] == old[component] and digest_obj(self._current.to_dict()[component]) != digest_obj(
                        snapshot.to_dict()[component]
                    ):
                        fail(
                            "E_SNAPSHOT_CORRUPT",
                            "加载的同版本快照内容不一致",
                            component=component,
                            version=new[component],
                        )
            if replace:
                self._current = snapshot
            return snapshot

    @property
    def current(self) -> Snapshot | None:
        return self._current

    def inspect_file(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"exists": False, "path": str(self.path)}
        snapshot = self.load(replace=False)
        return {
            "exists": True,
            "path": str(self.path),
            "versions": snapshot.versions,
            "checksum": snapshot.checksum,
            "ready_count": len(snapshot.ready["items"]),
        }

