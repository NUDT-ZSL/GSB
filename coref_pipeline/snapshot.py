"""Versioned whole-snapshot replacement and non-blocking stage locking."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import atomic_write_text, content_hash, pretty_json, read_json
from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    PipelineError,
)


SNAPSHOT_KINDS = (
    "evidence_weights",
    "time_equivalence",
    "trigger_similarity",
    "chain_state",
)


class SnapshotStore:
    """A monotonic snapshot index with atomic whole-file replacement.

    The stage lock is created with ``O_EXCL`` and is explicitly non-blocking.
    A second concurrent commit therefore receives ``E_STAGE_BUSY`` immediately.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / ".stage.lock"
        self.lock_owner: str | None = None

    def _empty_index(self) -> dict[str, Any]:
        return {"schema_version": "snapshot-index/v1", "latest": {}}

    def load_index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return self._empty_index()
        try:
            index = read_json(self.index_path)
        except (OSError, ValueError) as exc:
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field="index.json",
                message=f"快照索引损坏: {exc}",
            ) from exc
        if (
            not isinstance(index, dict)
            or index.get("schema_version") != "snapshot-index/v1"
            or not isinstance(index.get("latest"), dict)
        ):
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field="index.json",
                message="快照索引结构无效",
            )
        return index

    def _snapshot_path(self, kind: str, version: int, checksum: str) -> Path:
        return self.root / kind / f"v{version}-{checksum[:16]}.json"

    @contextmanager
    def stage_lock(self, owner: str = "incremental-commit") -> Iterator[None]:
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise PipelineError(
                code=E_STAGE_BUSY,
                stage="commit",
                field="stage_lock",
                message="已有增量提交正在生效，后到提交立即返回 E_STAGE_BUSY",
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"owner": owner, "pid": os.getpid()}))
                handle.flush()
                os.fsync(handle.fileno())
            self.lock_owner = owner
            yield
        finally:
            self.lock_owner = None
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def load_latest(self, kind: str) -> dict[str, Any] | None:
        if kind not in SNAPSHOT_KINDS:
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field="kind",
                message=f"未知快照类别: {kind}",
            )
        index = self.load_index()
        entry = index["latest"].get(kind)
        if entry is None:
            return None
        try:
            version = int(entry["version"])
            checksum = str(entry["checksum"])
            file_name = str(entry["file"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field=f"index.latest.{kind}",
                message="快照索引条目损坏",
            ) from exc
        path = self.root / file_name
        try:
            wrapper = read_json(path)
        except (OSError, ValueError) as exc:
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field=kind,
                message=f"快照文件缺失或无法读取: {exc}",
            ) from exc
        if not isinstance(wrapper, dict):
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field=kind,
                message="快照文件根节点必须为对象",
            )
        try:
            expected = content_hash(
                {
                    "kind": wrapper.get("kind"),
                    "version": wrapper.get("version"),
                    "payload": wrapper.get("payload"),
                }
            )
            wrapper_version = int(wrapper.get("version", -1))
        except (TypeError, ValueError) as exc:
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field=kind,
                message="快照版本字段损坏",
            ) from exc
        if (
            wrapper.get("kind") != kind
            or wrapper_version != version
            or wrapper.get("checksum") != checksum
            or checksum != expected
        ):
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field=kind,
                message="快照版本、校验和或内容不一致",
                details={
                    "expected_checksum": checksum,
                    "computed_checksum": expected,
                    "version": version,
                },
            )
        return {
            "kind": kind,
            "version": version,
            "checksum": checksum,
            "payload": wrapper["payload"],
            "file": file_name,
        }

    def _preflight(
        self,
        kind: str,
        version: int,
        checksum: str,
        *,
        allow_equal: bool = True,
    ) -> dict[str, Any] | None:
        current = self.load_latest(kind)
        if current is None:
            return None
        if version < int(current["version"]):
            raise PipelineError(
                code=E_SNAPSHOT_ROLLBACK,
                stage="snapshot",
                field=kind,
                message="快照版本回退被拒绝",
                details={
                    "attempted_version": version,
                    "current_version": current["version"],
                },
            )
        if version == int(current["version"]):
            if allow_equal and checksum == current["checksum"]:
                return current
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field=kind,
                message="同一版本快照的校验和发生变化",
                details={
                    "version": version,
                    "current_checksum": current["checksum"],
                    "attempted_checksum": checksum,
                },
            )
        return current

    def _write_snapshot(
        self,
        *,
        kind: str,
        payload: Any,
        version: int,
        checksum: str,
    ) -> str:
        path = self._snapshot_path(kind, version, checksum)
        wrapper = {
            "kind": kind,
            "version": version,
            "checksum": checksum,
            "payload": payload,
        }
        atomic_write_text(path, pretty_json(wrapper))
        return str(path.relative_to(self.root)).replace(os.sep, "/")

    def _commit_locked(
        self,
        *,
        kind: str,
        payload: Any,
        version: int,
        checksum: str | None = None,
    ) -> dict[str, Any]:
        computed_checksum = content_hash(
            {"kind": kind, "version": version, "payload": payload}
        )
        if checksum is not None and checksum != computed_checksum:
            raise PipelineError(
                code=E_SNAPSHOT_CORRUPT,
                stage="snapshot",
                field=kind,
                message="提交前快照校验和不符",
                details={
                    "provided_checksum": checksum,
                    "computed_checksum": computed_checksum,
                },
            )
        current = self._preflight(kind, version, computed_checksum)
        if current is not None and int(current["version"]) == version:
            return current
        file_name = self._write_snapshot(
            kind=kind,
            payload=payload,
            version=version,
            checksum=computed_checksum,
        )
        index = self.load_index()
        index["latest"][kind] = {
            "version": version,
            "checksum": computed_checksum,
            "file": file_name,
        }
        atomic_write_text(self.index_path, pretty_json(index))
        return {
            "kind": kind,
            "version": version,
            "checksum": computed_checksum,
            "payload": payload,
            "file": file_name,
        }

    def commit(
        self,
        *,
        kind: str,
        payload: Any,
        version: int,
        checksum: str | None = None,
    ) -> dict[str, Any]:
        with self.stage_lock(f"commit:{kind}:v{version}"):
            return self._commit_locked(
                kind=kind,
                payload=payload,
                version=version,
                checksum=checksum,
            )

    def commit_run(
        self,
        *,
        config_components: dict[str, dict[str, Any]],
        chain_payload: dict[str, Any],
        chain_version: int | None = None,
    ) -> dict[str, Any]:
        """Atomically commit all config components and one chain snapshot."""

        with self.stage_lock("incremental-run"):
            prepared: list[tuple[str, Any, int, str]] = []
            for kind, component in sorted(config_components.items()):
                version = int(component["version"])
                payload = component["payload"]
                checksum = content_hash(
                    {"kind": kind, "version": version, "payload": payload}
                )
                current = self._preflight(kind, version, checksum)
                prepared.append((kind, payload, version, checksum))
                if current is not None and int(current["version"]) == version:
                    continue
            chain_payload_hash = content_hash(
                {"kind": "chain_state", "payload": chain_payload}
            )
            current_chain = self.load_latest("chain_state")
            if chain_version is None:
                if (
                    current_chain is not None
                    and content_hash(
                        {
                            "kind": "chain_state",
                            "version": current_chain["version"],
                            "payload": chain_payload,
                        }
                    )
                    == content_hash(
                        {
                            "kind": "chain_state",
                            "version": current_chain["version"],
                            "payload": current_chain["payload"],
                        }
                    )
                ):
                    chain_version = int(current_chain["version"])
                else:
                    chain_version = (
                        int(current_chain["version"]) + 1
                        if current_chain is not None
                        else 1
                    )
            chain_checksum = content_hash(
                {
                    "kind": "chain_state",
                    "version": chain_version,
                    "payload": chain_payload,
                }
            )
            chain_current = self._preflight(
                "chain_state", chain_version, chain_checksum
            )

            committed: dict[str, Any] = {}
            for kind, payload, version, checksum in prepared:
                current = self._preflight(kind, version, checksum)
                if current is not None and int(current["version"]) == version:
                    committed[kind] = current
                else:
                    committed[kind] = self._commit_locked(
                        kind=kind,
                        payload=payload,
                        version=version,
                        checksum=checksum,
                    )
            if chain_current is not None and int(chain_current["version"]) == chain_version:
                committed["chain_state"] = chain_current
            else:
                committed["chain_state"] = self._commit_locked(
                    kind="chain_state",
                    payload=chain_payload,
                    version=chain_version,
                    checksum=chain_checksum,
                )
            return {
                "chain_payload_hash": chain_payload_hash,
                "committed": committed,
            }

    def summary(self) -> dict[str, Any]:
        index = self.load_index()
        latest = index.get("latest", {})
        return {
            "root": str(self.root),
            "latest": {
                kind: {
                    "version": int(entry["version"]),
                    "checksum": str(entry["checksum"]),
                    "file": str(entry["file"]),
                }
                for kind, entry in sorted(latest.items())
            },
        }

