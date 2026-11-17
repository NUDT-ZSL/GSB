"""Local workspace storage, cross-platform locking, and atomic JSON writes."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .errors import E_ARGUMENT, E_INPUT, E_IO, E_LOCK, TraceError
from .engine import RULESET_VERSION, REPORT_SCHEMA_VERSION


SOURCE_SCHEMA_VERSION = "1.0"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _validate_json_text(text: str, validator: Callable[[Any], None] | None) -> Any:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TraceError(
            E_IO,
            "temporary JSON output is not parseable before replacement",
            {"error": str(exc)},
        ) from exc
    if validator is not None:
        validator(parsed)
    return parsed


def atomic_write_json(
    path: str | Path,
    value: Any,
    validator: Callable[[Any], None] | None = None,
) -> str:
    """Write, fsync, parse-validate, then atomically replace the destination."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with temporary_path.open("r", encoding="utf-8") as handle:
            _validate_json_text(handle.read(), validator)
        os.replace(temporary_path, destination)
        temporary_path = None
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory_fd = os.open(destination.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except TraceError:
        raise
    except OSError as exc:
        raise TraceError(
            E_IO,
            "failed to atomically write JSON output",
            {"path": str(destination), "error": str(exc)},
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


class WorkspaceLock:
    """Non-blocking, re-entrant in-process workspace lock."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / ".flowtrace.lock"
        self._handle: Any = None
        self._depth = 0

    def acquire(self) -> "WorkspaceLock":
        if self._depth > 0:
            self._depth += 1
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+b")
        except OSError as exc:
            raise TraceError(
                E_IO,
                "cannot open workspace lock file",
                {"path": str(self.path), "error": str(exc)},
            ) from exc
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise TraceError(
                        E_LOCK,
                        "workspace checkpoint/report lock is already held",
                        {"path": str(self.path), "error": str(exc)},
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise TraceError(
                            E_LOCK,
                            "workspace checkpoint/report lock is already held",
                            {"path": str(self.path)},
                        ) from exc
                    raise
        except Exception:
            handle.close()
            raise
        self._handle = handle
        self._depth = 1
        return self

    def release(self) -> None:
        if self._depth > 1:
            self._depth -= 1
            return
        if self._depth == 0:
            return
        handle = self._handle
        self._handle = None
        self._depth = 0
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @property
    def held(self) -> bool:
        return self._depth > 0

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.sources_dir = self.root / "sources"
        self.checkpoints_dir = self.root / "checkpoints"
        self.reports_dir = self.root / "reports"
        self.snapshot_source = self.sources_dir / "snapshots.json"
        self.edit_source = self.sources_dir / "edits.json"
        self.checkpoint_file = self.checkpoints_dir / "checkpoint.json"
        self.lock = WorkspaceLock(self.root)

    def ensure(self) -> "Workspace":
        for directory in (
            self.root,
            self.sources_dir,
            self.checkpoints_dir,
            self.reports_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TraceError(
                    E_IO,
                    "cannot create workspace directory",
                    {"path": str(directory), "error": str(exc)},
                ) from exc
        return self

    def check_capabilities(self) -> dict[str, Any]:
        """Probe lock, fsync, temporary file, and atomic rename operations."""

        self.ensure()
        probe = self.root / ".flowtrace-capability-probe.json"

        def validate_probe(value: Any) -> None:
            if not isinstance(value, dict) or value.get("ok") is not True:
                raise TraceError(E_IO, "capability probe validation failed")

        try:
            with WorkspaceLock(self.root):
                atomic_write_json(
                    probe,
                    {"ok": True, "rule_version": RULESET_VERSION},
                    validator=validate_probe,
                )
                with probe.open("r", encoding="utf-8") as handle:
                    json.load(handle)
            probe.unlink(missing_ok=True)
        except TraceError:
            raise
        except OSError as exc:
            raise TraceError(
                E_IO,
                "workspace does not support locking or atomic replacement",
                {"root": str(self.root), "error": str(exc)},
            ) from exc
        return {
            "root": str(self.root),
            "lock": "ok",
            "atomic_replace": "ok",
            "temporary_files": "ok",
        }

    def _read_list_file(self, path: Path, key: str, default: list[Any]) -> list[Any]:
        if not path.exists():
            return list(default)
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise TraceError(
                E_INPUT,
                "cannot parse workspace source JSON",
                {"path": str(path), "error": str(exc)},
            ) from exc
        if isinstance(payload, list):
            return payload
        if isinstance(payload, Mapping) and isinstance(payload.get(key), list):
            return list(payload[key])
        raise TraceError(
            E_INPUT,
            f"workspace source must contain a {key} list",
            {"path": str(path)},
        )

    def read_snapshots(self) -> list[Any]:
        return self._read_list_file(self.snapshot_source, "snapshots", [])

    def read_edits(self) -> list[Any]:
        return self._read_list_file(self.edit_source, "edits", [])

    def write_snapshots(self, snapshots: list[Any]) -> str:
        return atomic_write_json(
            self.snapshot_source,
            {
                "schema_version": SOURCE_SCHEMA_VERSION,
                "rule_version": RULESET_VERSION,
                "snapshots": snapshots,
            },
        )

    def write_edits(self, edits: list[Any]) -> str:
        return atomic_write_json(
            self.edit_source,
            {
                "schema_version": SOURCE_SCHEMA_VERSION,
                "rule_version": RULESET_VERSION,
                "edits": edits,
            },
        )

    def write_report(self, report: Mapping[str, Any], path: str | Path | None = None) -> str:
        destination = Path(path) if path is not None else self.reports_dir / "diagnostics.json"
        return atomic_write_json(
            destination,
            dict(report),
            validator=_validate_report_document,
        )

    def write_graph(self, graph: Mapping[str, Any], path: str | Path | None = None) -> str:
        destination = (
            Path(path) if path is not None else self.reports_dir / "flow_graph.json"
        )
        return atomic_write_json(destination, dict(graph), validator=_validate_graph_document)


def _validate_report_document(value: Any) -> None:
    required = {
        "schema_version",
        "rule_version",
        "status",
        "summary",
        "documents",
        "rejections",
        "recovery",
        "state_hash",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise TraceError(E_IO, "diagnostic report is missing required stable fields")
    if value["schema_version"] != REPORT_SCHEMA_VERSION:
        raise TraceError(E_IO, "diagnostic report schema version is invalid")
    if not isinstance(value["documents"], list) or not isinstance(
        value["rejections"], list
    ):
        raise TraceError(E_IO, "diagnostic report collections are invalid")


def _validate_graph_document(value: Any) -> None:
    required = {"schema_version", "rule_version", "documents", "summary"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise TraceError(E_IO, "flow graph is missing required stable fields")
    if not isinstance(value["documents"], list):
        raise TraceError(E_IO, "flow graph documents must be a list")
    for document in value["documents"]:
        if not isinstance(document, dict) or not {"document_id", "nodes", "edges"}.issubset(
            document
        ):
            raise TraceError(E_IO, "flow graph document shape is invalid")


def load_json_file(path: str | Path) -> Any:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise TraceError(E_ARGUMENT, "input file does not exist", {"path": str(source)}) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TraceError(
            E_INPUT,
            "input file is not valid JSON",
            {"path": str(source), "error": str(exc)},
        ) from exc


def combine_snapshot_payloads(payloads: list[Any]) -> list[Any]:
    from .engine import extract_snapshot_items

    combined: list[Any] = []
    for payload in payloads:
        combined.extend(extract_snapshot_items(payload))
    return combined


def combine_edit_payloads(payloads: list[Any]) -> list[Any]:
    from .engine import extract_edit_items

    combined: list[Any] = []
    for payload in payloads:
        combined.extend(extract_edit_items(payload))
    return combined
