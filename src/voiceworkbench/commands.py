"""Online edit command stream: parsing, validation and label resolution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .determinism import read_json
from .errors import CommandError

COMMAND_TYPES = {
    "set_cluster_threshold",
    "cut_interval",
    "merge_speakers",
    "split_speaker",
    "replace_interval_audio",
}


@dataclass(frozen=True)
class EditCommand:
    seq: int
    command_type: str
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "command_type": self.command_type, "params": dict(self.params)}


def load_commands(path: str) -> list[EditCommand]:
    commands: list[EditCommand] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = read_json_text(line)
            except ValueError as exc:
                raise CommandError(f"invalid JSON on line {line_no}: {exc}") from exc
            commands.append(parse_command(obj, line_no))
    if not commands:
        raise CommandError("command stream is empty")
    seqs = [command.seq for command in commands]
    if seqs != list(range(1, len(commands) + 1)):
        raise CommandError("command seq must be 1..N without gaps or duplicates")
    return commands


def read_json_text(text: str) -> dict[str, Any]:
    import json
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("command must be a JSON object")
    return obj


def parse_command(obj: dict[str, Any], line_no: int | None = None) -> EditCommand:
    location = f"line {line_no}" if line_no else "command"
    if set(obj) != {"seq", "command_type", "params"}:
        raise CommandError(f"{location}: keys must be exactly seq/command_type/params")
    seq = obj["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise CommandError(f"{location}: seq must be a positive integer")
    command_type = obj["command_type"]
    if not isinstance(command_type, str) or command_type not in COMMAND_TYPES:
        raise CommandError(f"{location}: unknown command_type {command_type!r}")
    params = obj["params"]
    if not isinstance(params, dict):
        raise CommandError(f"{location}: params must be an object")
    validator = _VALIDATORS[command_type]
    cleaned = validator(params, location)
    return EditCommand(seq=seq, command_type=command_type, params=cleaned)


def _require_number(params: dict[str, Any], key: str, location: str,
                    minimum: float | None = None) -> float:
    if key not in params:
        raise CommandError(f"{location}: missing param {key}")
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError(f"{location}: param {key} must be a number")
    if minimum is not None and value < minimum:
        raise CommandError(f"{location}: param {key} must be >= {minimum}")
    return float(value)


def validate_set_threshold(params: dict[str, Any], location: str) -> dict[str, Any]:
    extra = set(params) - {"cluster_distance_threshold"}
    if extra:
        raise CommandError(f"{location}: unexpected params {sorted(extra)}")
    value = _require_number(params, "cluster_distance_threshold", location, minimum=0.0)
    if value > 8.0:
        raise CommandError(f"{location}: cluster_distance_threshold must be <= 8")
    return {"cluster_distance_threshold": value}


def validate_cut(params: dict[str, Any], location: str) -> dict[str, Any]:
    extra = set(params) - {"file_name", "start_ms", "end_ms"}
    if extra:
        raise CommandError(f"{location}: unexpected params {sorted(extra)}")
    file_name = _require_text(params, "file_name", location)
    start_ms = _require_number(params, "start_ms", location, minimum=0.0)
    end_ms = _require_number(params, "end_ms", location, minimum=0.0)
    if end_ms <= start_ms:
        raise CommandError(f"{location}: end_ms must be greater than start_ms")
    return {"file_name": file_name, "start_ms": start_ms, "end_ms": end_ms}


def validate_merge(params: dict[str, Any], location: str) -> dict[str, Any]:
    extra = set(params) - {"label_a", "label_b"}
    if extra:
        raise CommandError(f"{location}: unexpected params {sorted(extra)}")
    label_a = _require_selector(params, "label_a", location)
    label_b = _require_selector(params, "label_b", location)
    return {"label_a": label_a, "label_b": label_b}


def validate_split(params: dict[str, Any], location: str) -> dict[str, Any]:
    extra = set(params) - {"label"}
    if extra:
        raise CommandError(f"{location}: unexpected params {sorted(extra)}")
    return {"label": _require_selector(params, "label", location)}


def validate_replace(params: dict[str, Any], location: str) -> dict[str, Any]:
    extra = set(params) - {"file_name", "start_ms", "end_ms", "speaker"}
    if extra:
        raise CommandError(f"{location}: unexpected params {sorted(extra)}")
    file_name = _require_text(params, "file_name", location)
    start_ms = _require_number(params, "start_ms", location, minimum=0.0)
    end_ms = _require_number(params, "end_ms", location, minimum=0.0)
    if end_ms <= start_ms:
        raise CommandError(f"{location}: end_ms must be greater than start_ms")
    speaker = _require_text(params, "speaker", location)
    return {
        "file_name": file_name,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "speaker": speaker,
    }


def _require_text(params: dict[str, Any], key: str, location: str) -> str:
    if key not in params:
        raise CommandError(f"{location}: missing param {key}")
    value = params[key]
    if not isinstance(value, str) or not value.strip():
        raise CommandError(f"{location}: param {key} must be a non-empty string")
    return value


def _require_selector(params: dict[str, Any], key: str, location: str) -> dict[str, Any]:
    if key not in params:
        raise CommandError(f"{location}: missing param {key}")
    value = params[key]
    if isinstance(value, str):
        if not value.strip():
            raise CommandError(f"{location}: selector {key} is empty")
        return {"kind": "label", "value": value}
    if isinstance(value, dict) and set(value) == {"nth"}:
        nth = value["nth"]
        if not isinstance(nth, int) or isinstance(nth, bool) or nth < 0:
            raise CommandError(f"{location}: nth selector must be a non-negative integer")
        return {"kind": "nth", "value": nth}
    raise CommandError(f"{location}: selector {key} must be a label string or {{'nth': k}}")


def resolve_selector(selector: dict[str, Any], labels: list[str]) -> str:
    ordered = sorted(labels)
    if selector["kind"] == "label":
        if selector["value"] not in ordered:
            raise CommandError(f"label not present: {selector['value']}",
                               available=ordered)
        return selector["value"]
    index = selector["value"]
    if index >= len(ordered):
        raise CommandError(f"nth selector {index} out of range",
                           available=ordered)
    return ordered[index]


_VALIDATORS = {
    "set_cluster_threshold": validate_set_threshold,
    "cut_interval": validate_cut,
    "merge_speakers": validate_merge,
    "split_speaker": validate_split,
    "replace_interval_audio": validate_replace,
}
