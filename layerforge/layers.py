from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Tuple

from .canonical import byte_digest, pretty_json_bytes
from .errors import LayerForgeError
from .validation import safe_relative_path


LAYER_SCHEMA = "layerforge.layer/v1"


def normalize_action(
    raw: Mapping[str, Any],
    *,
    workspace_root: Path,
) -> Dict[str, Any]:
    op = raw.get("op")
    aliases = {"create": "add", "write": "add", "update": "modify", "remove": "delete"}
    if not isinstance(op, str):
        raise LayerForgeError("action.op must be a string", details={"action": dict(raw)})
    op = aliases.get(op, op)
    if op not in {"add", "modify", "delete"}:
        raise LayerForgeError(
            "unsupported file action",
            details={"op": op, "allowed": ["add", "modify", "delete"]},
        )
    path = safe_relative_path(raw.get("path"), workspace_root=workspace_root)
    content = raw.get("content", "")
    if op == "delete":
        if "content" in raw and raw.get("content") not in (None, ""):
            raise LayerForgeError(
                "delete action must not carry content",
                details={"path": path},
            )
        content_text = ""
        content_bytes = b""
        content_digest = None
        size = 0
    else:
        if content is None:
            content_text = ""
        elif isinstance(content, str):
            content_text = content
        else:
            raise LayerForgeError(
                "action.content must be a string",
                details={"path": path, "type": type(content).__name__},
            )
        content_bytes = content_text.encode("utf-8")
        content_digest = byte_digest(content_bytes)
        size = len(content_bytes)
    mode = raw.get("mode", "0644")
    if not isinstance(mode, str) or not mode:
        raise LayerForgeError("action.mode must be an octal-like string", details={"path": path})
    return {
        "op": op,
        "path": path,
        "content": content_text if op != "delete" else None,
        "content_sha256": content_digest,
        "size": size,
        "mode": mode,
    }


def normalize_actions(
    raw_actions: Iterable[Mapping[str, Any]],
    *,
    workspace_root: Path,
) -> List[Dict[str, Any]]:
    actions = [normalize_action(item, workspace_root=workspace_root) for item in raw_actions]
    seen: Dict[str, str] = {}
    for action in actions:
        path = action["path"]
        previous = seen.get(path)
        op = action["op"]
        if op == "add" and previous is not None:
            raise LayerForgeError(
                "duplicate add for path in one step",
                details={"path": path, "previous_op": previous},
            )
        if op in {"modify", "delete"} and previous == "delete":
            raise LayerForgeError(
                "cannot modify or delete a path deleted earlier in same step",
                details={"path": path},
            )
        seen[path] = op
    return actions


def apply_actions_to_file_table(
    parent_files: Mapping[str, Mapping[str, Any]],
    actions: Iterable[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    files: Dict[str, Dict[str, Any]] = {path: dict(meta) for path, meta in parent_files.items()}
    for action in actions:
        path = action["path"]
        op = action["op"]
        if op == "delete":
            files.pop(path, None)
        else:
            files[path] = {
                "sha256": action["content_sha256"],
                "size": action["size"],
                "mode": action["mode"],
            }
    return {path: files[path] for path in sorted(files)}


def diff_summary(actions: Iterable[Mapping[str, Any]]) -> Dict[str, List[str]]:
    result = {"added": [], "modified": [], "deleted": []}
    key = {"add": "added", "modify": "modified", "delete": "deleted"}
    for action in actions:
        result[key[action["op"]]].append(action["path"])
    for paths in result.values():
        paths.sort()
    return result


def estimate_cost_ms(step: Mapping[str, Any]) -> int:
    explicit = step.get("work_cost_ms")
    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, int) or explicit < 1:
            raise LayerForgeError(
                "work_cost_ms must be a positive integer",
                details={"step_id": step.get("step_id"), "value": explicit},
            )
        return explicit
    actions = step.get("actions") or []
    total_bytes = 0
    for action in actions:
        content = action.get("content") if isinstance(action, Mapping) else None
        if isinstance(content, str):
            total_bytes += len(content.encode("utf-8"))
    return 1 + len(actions) + total_bytes // 64


def build_layer_spec(
    step: Mapping[str, Any],
    *,
    actions: List[Dict[str, Any]],
    parent_digest: str,
    parent_ref: str,
    written_at_ms: int,
    parent_files: Mapping[str, Mapping[str, Any]],
    dependency_digests: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    result_files = apply_actions_to_file_table(parent_files, actions)
    return {
        "schema": LAYER_SCHEMA,
        "algorithm": "sha256",
        "step_id": step["step_id"],
        "seq": int(step["seq"]),
        "parent_ref": parent_ref,
        "parent_digest": parent_digest,
        "dependencies": sorted(
            dependency_digests or [], key=lambda item: item["step_id"]
        ),
        "written_at_ms": written_at_ms,
        "time_conversion": step.get("time_conversion"),
        "env": dict(step.get("env") or {}),
        "args": dict(step.get("args") or {}),
        "actions": actions,
        "diff": diff_summary(actions),
        "result_files": result_files,
        "work_cost_ms": estimate_cost_ms(step),
    }


def encode_layer_object(spec: Mapping[str, Any]) -> Tuple[bytes, str]:
    data = pretty_json_bytes(spec)
    return data, byte_digest(data)


def object_path(object_dir: Path, digest: str) -> Path:
    algorithm, value = digest.split(":", 1)
    return object_dir / algorithm / value[:2] / f"{value}.json"


def write_layer_object(object_dir: Path, spec: Mapping[str, Any]) -> Tuple[str, Path, bool]:
    data, digest = encode_layer_object(spec)
    path = object_path(object_dir, digest)
    reused = False
    if path.exists():
        existing = path.read_bytes()
        if byte_digest(existing) != digest or existing != data:
            from .errors import ErrorCode

            raise LayerForgeError(
                "content-addressed object is corrupt or non-canonical",
                code=ErrorCode.OBJECT_CORRUPT,
                details={"path": str(path), "expected_digest": digest},
            )
        reused = True
        return digest, path, reused
    from .canonical import atomic_write_bytes

    atomic_write_bytes(path, data)
    return digest, path, reused


def apply_actions_to_directory(
    target_dir: Path,
    actions: Iterable[Mapping[str, Any]],
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for action in actions:
        target = target_dir / action["path"]
        if action["op"] == "delete":
            target.unlink(missing_ok=True)
            _remove_empty_parents(target.parent, target_dir)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.write.tmp")
        tmp.write_bytes((action.get("content") or "").encode("utf-8"))
        tmp.replace(target)


def populate_root_from_parent(target_dir: Path, parent_root: Optional[Path]) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    if parent_root and parent_root.exists():
        shutil.copytree(parent_root, target_dir, dirs_exist_ok=True)


def _remove_empty_parents(path: Path, stop: Path) -> None:
    stop = stop.resolve()
    current = path
    while True:
        try:
            resolved = current.resolve()
            resolved.relative_to(stop)
        except (ValueError, OSError):
            break
        if resolved == stop or not resolved.exists():
            break
        try:
            resolved.rmdir()
        except OSError:
            break
        current = resolved.parent
