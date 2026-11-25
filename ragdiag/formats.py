"""Readers for the three tool-result formats (v1, v2, v3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, checksum_of, content_hash, sha256_value
from .errors import E_FORMAT_CORRUPT, E_VERSION_UNSUPPORTED, WorkbenchError


SUPPORTED_VERSIONS = {"v1", "v2", "v3"}


def normalize_version(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise WorkbenchError(
            E_VERSION_UNSUPPORTED,
            "format version is missing or not a string",
            format_version=repr(value),
        )
    normalized = value.strip().lower()
    if not normalized.startswith("v"):
        normalized = f"v{normalized}"
    if normalized not in SUPPORTED_VERSIONS:
        raise WorkbenchError(
            E_VERSION_UNSUPPORTED,
            "unsupported tool result format version",
            format_version=value,
        )
    return normalized


def _corrupt(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(E_FORMAT_CORRUPT, message, **details)


def _raw_checksum(content: Any) -> str:
    return checksum_of(content)


def detect_version(content: Any) -> str | None:
    if isinstance(content, dict):
        header = content.get("format_version", content.get("version"))
        return normalize_version(header) if header is not None else "v3"
    if not isinstance(content, str):
        raise _corrupt(
            "tool result content must be text or a structured object",
            content_type=type(content).__name__,
        )
    stripped = content.lstrip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise _corrupt("v3 JSON cannot be parsed", error=str(exc)) from exc
        header = parsed.get("format_version", parsed.get("version"))
        return normalize_version(header) if header is not None else "v3"
    if stripped.startswith("v1|"):
        return "v1"
    first_line = stripped.splitlines()[0] if stripped else ""
    if first_line.startswith("format_version="):
        return normalize_version(first_line.split("=", 1)[1])
    if "|" in stripped:
        return "v1"
    if "=" in first_line and "\n" in stripped:
        return "v2"
    if "=" in stripped:
        return "v2"
    return None


def explicit_version_header(content: Any) -> str | None:
    """Return only a version explicitly written in the content."""

    if isinstance(content, dict):
        header = content.get("format_version", content.get("version"))
        return normalize_version(header) if header is not None else None
    if not isinstance(content, str):
        return None
    stripped = content.lstrip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            header = parsed.get(
                "format_version", parsed.get("version")
            )
            return normalize_version(header) if header is not None else None
        return None
    if stripped.startswith("v1|"):
        return "v1"
    first_line = stripped.splitlines()[0] if stripped else ""
    for key in ("format_version", "version"):
        if first_line.startswith(f"{key}="):
            return normalize_version(first_line.split("=", 1)[1])
    return None


def sniff_version(content: Any) -> str:
    detected = detect_version(content)
    if detected is not None:
        return detected
    prefix = content.lstrip()[:32] if isinstance(content, str) else repr(content)
    raise WorkbenchError(
        E_VERSION_UNSUPPORTED,
        "format version cannot be inferred from content",
        content_prefix=prefix,
    )


def _parse_float(field: str, raw: Any) -> float:
    if isinstance(raw, bool):
        raise _corrupt("numeric field contains a boolean", field=field, value=raw)
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise _corrupt(
            "numeric field cannot be parsed",
            field=field,
            value=raw,
        ) from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise _corrupt("numeric field is not finite", field=field, value=raw)
    return number


def _parse_v1(content: str, entry: dict[str, Any]) -> tuple[dict[str, str], dict[str, float]]:
    tokens = content.split("|")
    if tokens and tokens[0].strip().lower() == "v1":
        body = tokens[1:]
    else:
        body = tokens
    columns = entry.get("columns")
    if columns is not None:
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise _corrupt("v1 columns metadata is invalid")
        if len(body) != len(columns):
            raise _corrupt(
                "v1 field count does not match schema",
                expected=len(columns),
                actual=len(body),
            )
        required_order = list(columns)
    else:
        required_order = []
        for index, token in enumerate(body):
            if "=" not in token:
                raise _corrupt("v1 column lacks key=value", column=index)
            key, _ = token.split("=", 1)
            required_order.append(key)

    fields: dict[str, str] = {}
    for index, token in enumerate(body):
        if columns is not None:
            key = required_order[index]
            value = token
        else:
            if "=" not in token:
                raise _corrupt("v1 column lacks key=value", column=index)
            key, value = token.split("=", 1)
            if key != required_order[index]:
                raise _corrupt(
                    "v1 column key does not match schema",
                    column=index,
                    expected=required_order[index],
                    actual=key,
                )
        if key in fields:
            raise _corrupt("v1 duplicate field", field=key)
        fields[key] = value

    numeric_fields = entry.get("numeric_fields", [])
    if not isinstance(numeric_fields, list):
        raise _corrupt("numeric_fields metadata must be a list")
    numbers: dict[str, float] = {}
    for field in numeric_fields:
        if field not in fields:
            raise _corrupt("numeric field is missing", field=field)
        numbers[str(field)] = _parse_float(str(field), fields[str(field)])
    return dict(sorted(fields.items())), dict(sorted(numbers.items()))


def _parse_v2(content: str, entry: dict[str, Any]) -> tuple[dict[str, str], dict[str, float]]:
    fields: dict[str, str] = {}
    stripped = content.lstrip()
    if stripped.startswith("{"):
        try:
            structured = json.loads(content)
        except json.JSONDecodeError as exc:
            raise _corrupt("v2 JSON cannot be parsed", error=str(exc)) from exc
        if not isinstance(structured, dict):
            raise _corrupt("v2 JSON root must be an object")
        for key, value in structured.items():
            if isinstance(value, (dict, list)):
                raise _corrupt(
                    "v2 values must be flat scalars", key=str(key)
                )
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif value is None:
                value = "null"
            fields[str(key)] = str(value)
        line_source: list[str] = []
    else:
        line_source = (
            content.splitlines()
            if "\n" in content
            else content.split(";")
        )
    for line_no, line in enumerate(line_source, start=1):
        if not line:
            continue
        if "=" not in line:
            raise _corrupt(
                "v2 line lacks key=value",
                line=line_no,
            )
        key, value = line.split("=", 1)
        if not key:
            raise _corrupt("v2 key is empty", line=line_no)
        if key in fields:
            raise _corrupt("v2 duplicate key", key=key, line=line_no)
        fields[key] = value
    version_header = fields.get(
        "format_version", fields.get("version")
    )
    if version_header is not None and normalize_version(version_header) != "v2":
        raise _corrupt(
            "v2 version header is invalid",
            header=version_header,
        )
    required = entry.get("fields", entry.get("columns", []))
    if not isinstance(required, list):
        raise _corrupt("v2 required fields metadata must be a list")
    for key in required:
        if not isinstance(key, str) or key not in fields:
            raise _corrupt("v2 required key is missing", key=key)
    field_count = entry.get("field_count")
    if field_count is not None:
        if (
            not isinstance(field_count, int)
            or isinstance(field_count, bool)
            or field_count < 0
        ):
            raise _corrupt("v2 field_count metadata must be a non-negative integer")
        if len(fields) != field_count:
            raise _corrupt(
                "v2 field count does not match schema",
                expected=field_count,
                actual=len(fields),
            )
    numeric_fields = entry.get("numeric_fields", [])
    if not isinstance(numeric_fields, list):
        raise _corrupt("numeric_fields metadata must be a list")
    numbers: dict[str, float] = {}
    for field in numeric_fields:
        if field not in fields:
            raise _corrupt("numeric field is missing", field=field)
        numbers[str(field)] = _parse_float(str(field), fields[str(field)])
    return dict(sorted(fields.items())), dict(sorted(numbers.items()))


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(value[key], child))
    elif isinstance(value, list):
        flat[prefix] = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    elif isinstance(value, bool):
        flat[prefix] = "true" if value else "false"
    elif value is None:
        flat[prefix] = "null"
    else:
        flat[prefix] = str(value)
    return flat


def _lookup_path(structured: Any, path: str) -> Any:
    current = structured
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise _corrupt("v3 required path is missing", path=path)
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                raise _corrupt("v3 required path is missing", path=path)
            current = current[int(part)]
        else:
            raise _corrupt("v3 required path is missing", path=path)
    return current


def _parse_v3(content: Any, entry: dict[str, Any]) -> tuple[dict[str, str], dict[str, float]]:
    if isinstance(content, str):
        try:
            structured = json.loads(content)
        except json.JSONDecodeError as exc:
            raise _corrupt("v3 JSON cannot be parsed", error=str(exc)) from exc
    else:
        structured = content
    if not isinstance(structured, dict):
        raise _corrupt("v3 root must be an object")
    header = structured.get("format_version", structured.get("version"))
    if header is not None and normalize_version(header) != "v3":
        raise _corrupt("v3 version header is invalid", header=header)

    required_paths = entry.get("required_paths", [])
    if not isinstance(required_paths, list):
        raise _corrupt("required_paths metadata must be a list")
    for path in required_paths:
        _lookup_path(structured, str(path))

    numeric_paths = entry.get("numeric_paths", [])
    if not isinstance(numeric_paths, list):
        raise _corrupt("numeric_paths metadata must be a list")
    numbers: dict[str, float] = {}
    for path in numeric_paths:
        value = _lookup_path(structured, str(path))
        numbers[str(path)] = _parse_float(str(path), value)

    flattened = _flatten(structured)
    flattened.pop("format_version", None)
    flattened.pop("version", None)
    return dict(sorted(flattened.items())), dict(sorted(numbers.items()))


def parse_tool_result(
    entry: dict[str, Any],
    snapshot_version: int,
    requested_version: Any | None = None,
) -> dict[str, Any]:
    """Parse one repository entry and return its canonical artifact."""

    if not isinstance(entry, dict):
        raise _corrupt("repository entry must be an object")
    result_ref = entry.get("result_ref")
    if not isinstance(result_ref, str) or not result_ref:
        raise _corrupt("repository entry result_ref is missing")
    if "content" not in entry:
        raise _corrupt("repository entry content is missing", result_ref=result_ref)
    content = entry["content"]
    declared = entry.get("format_version")
    detected_version = detect_version(content)
    explicit_header = explicit_version_header(content)
    if declared is None:
        if detected_version is None:
            raise WorkbenchError(
                E_VERSION_UNSUPPORTED,
                "format version cannot be inferred from content or metadata",
                result_ref=result_ref,
            )
        declared_version = detected_version
    else:
        declared_version = normalize_version(declared)
    if requested_version is not None:
        expected = normalize_version(requested_version)
        if expected != declared_version:
            raise WorkbenchError(
                E_VERSION_UNSUPPORTED,
                "requested format version does not match repository entry",
                result_ref=result_ref,
                requested=expected,
                actual=declared_version,
            )
    if explicit_header is not None and explicit_header != declared_version:
        raise _corrupt(
            "content version header conflicts with repository metadata",
            result_ref=result_ref,
            declared=declared_version,
            header=explicit_header,
        )

    expected_checksum = entry.get("checksum")
    actual_checksum = _raw_checksum(content)
    if expected_checksum != actual_checksum:
        raise _corrupt(
            "tool result checksum mismatch",
            result_ref=result_ref,
            expected=expected_checksum,
            actual=actual_checksum,
        )

    if declared_version == "v1":
        if not isinstance(content, str):
            raise _corrupt("v1 content must be text", result_ref=result_ref)
        fields, numeric = _parse_v1(content, entry)
    elif declared_version == "v2":
        if not isinstance(content, str):
            raise _corrupt("v2 content must be text", result_ref=result_ref)
        fields, numeric = _parse_v2(content, entry)
    else:
        fields, numeric = _parse_v3(content, entry)

    if "version" in entry:
        entry_version = entry["version"]
        if not isinstance(entry_version, int) or isinstance(entry_version, bool):
            raise _corrupt("entry version must be an integer", result_ref=result_ref)
    else:
        entry_version = 1
    rendered = "; ".join(f"{key}={fields[key]}" for key in sorted(fields))
    artifact = {
        "result_ref": result_ref,
        "format_version": declared_version,
        "entry_version": entry_version,
        "snapshot_version": int(snapshot_version),
        "content_hash": content_hash(content),
        "checksum": actual_checksum,
        "fields": fields,
        "numeric": numeric,
        "rendered": rendered,
    }
    artifact["artifact_hash"] = sha256_value(artifact)
    return artifact


@dataclass(frozen=True)
class Repository:
    snapshot_version: int
    seed: int
    entries: dict[str, dict[str, Any]]
    raw: dict[str, Any]

    def plan_fingerprint(self, result_ref: str) -> dict[str, Any]:
        """A tolerant fingerprint used before a request-level reader runs."""

        entry = self.entries.get(result_ref)
        if entry is None:
            return {
                "result_ref": result_ref,
                "missing": True,
            }
        if "content" not in entry:
            return {
                "result_ref": result_ref,
                "invalid": "content_missing",
                "entry_version": entry.get("version", 1),
            }
        content = entry["content"]
        actual_checksum = _raw_checksum(content)
        declared = entry.get("format_version")
        try:
            version = (
                normalize_version(declared)
                if declared is not None
                else sniff_version(content)
            )
        except WorkbenchError:
            version = str(declared) if declared is not None else "unknown"
        expected_checksum = entry.get("checksum")
        if expected_checksum != actual_checksum or version not in SUPPORTED_VERSIONS:
            return {
                "result_ref": result_ref,
                "format_version": version,
                "entry_version": entry.get("version", 1),
                "content_hash": content_hash(content),
                "checksum": actual_checksum,
                "declared_checksum": expected_checksum,
                "invalid": (
                    "checksum"
                    if expected_checksum != actual_checksum
                    else "version"
                ),
            }
        return {
            "result_ref": result_ref,
            "format_version": version,
            "entry_version": entry.get("version", 1),
            "content_hash": content_hash(content),
            "checksum": actual_checksum,
        }

    def entry_fingerprint(self, result_ref: str) -> dict[str, Any]:
        entry = self.entries.get(result_ref)
        if entry is None:
            raise _corrupt("tool result reference is missing", result_ref=result_ref)
        if "content" not in entry:
            raise _corrupt("tool result content is missing", result_ref=result_ref)
        declared = entry.get("format_version")
        version = normalize_version(declared) if declared is not None else sniff_version(
            entry["content"]
        )
        checksum = entry.get("checksum")
        actual = _raw_checksum(entry["content"])
        if checksum != actual:
            raise _corrupt(
                "tool result checksum mismatch",
                result_ref=result_ref,
                expected=checksum,
                actual=actual,
            )
        return {
            "result_ref": result_ref,
            "format_version": version,
            "entry_version": entry.get("version", 1),
            "content_hash": content_hash(entry["content"]),
            "checksum": actual,
        }

    def canonical_index(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.snapshot_version,
            "seed": self.seed,
            "entries": [
                self.plan_fingerprint(result_ref)
                for result_ref in sorted(self.entries)
            ],
        }

    def index_hash(self) -> str:
        return sha256_value(self.canonical_index())


def load_repository(path: str | Path | dict[str, Any]) -> Repository:
    if isinstance(path, dict):
        raw = path
    else:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise _corrupt("tool result repository cannot be loaded", error=str(exc)) from exc
    if not isinstance(raw, dict):
        raise _corrupt("tool result repository root must be an object")
    snapshot_version = raw.get("snapshot_version")
    if not isinstance(snapshot_version, int) or isinstance(snapshot_version, bool):
        raise _corrupt("repository snapshot_version must be an integer")
    seed = raw.get("seed", 0)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise _corrupt("repository seed must be an integer")
    raw_entries = raw.get("entries")
    entries: dict[str, dict[str, Any]] = {}
    if isinstance(raw_entries, dict):
        iterable = []
        for ref, entry in raw_entries.items():
            if not isinstance(entry, dict):
                raise _corrupt("repository entry must be an object", result_ref=ref)
            with_ref = dict(entry)
            with_ref.setdefault("result_ref", ref)
            iterable.append(with_ref)
    elif isinstance(raw_entries, list):
        iterable = raw_entries
    else:
        raise _corrupt("repository entries must be a list or object")
    for entry in iterable:
        if not isinstance(entry, dict):
            raise _corrupt("repository entry must be an object")
        ref = entry.get("result_ref")
        if not isinstance(ref, str) or not ref:
            raise _corrupt("repository entry result_ref is missing")
        if ref in entries:
            raise _corrupt("duplicate repository result_ref", result_ref=ref)
        entries[ref] = entry
    if not entries:
        raise _corrupt("tool result repository is empty")
    return Repository(
        snapshot_version=snapshot_version,
        seed=seed,
        entries=entries,
        raw=raw,
    )


def read_repository_bytes(repository: Repository, requested_versions: dict[str, Any] | None = None) -> bytes:
    requested_versions = requested_versions or {}
    results = [
        parse_tool_result(
            repository.entries[result_ref],
            repository.snapshot_version,
            requested_versions.get(result_ref),
        )
        for result_ref in sorted(repository.entries)
    ]
    return canonical_bytes(results)

