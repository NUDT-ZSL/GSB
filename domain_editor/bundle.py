"""Local sample bundle discovery and source fingerprints."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import JsonInvalidError
from .jsonutil import sha256_bytes


@dataclass(frozen=True)
class Bundle:
    manifest_path: Path
    base_dir: Path
    documents_path: Path
    rules_path: Path
    v1_path: Path
    v2_path: Path
    commands_path: Path
    injections_path: Path | None
    max_workers: int
    default_source_version: str

    def source_path(self, version: str) -> Path:
        if version == "v1":
            return self.v1_path
        if version == "v2":
            return self.v2_path
        raise JsonInvalidError(f"unknown source version {version!r}")

    def fingerprint(self, version: str) -> str:
        pieces: list[bytes] = []
        for label, path in (
            ("documents", self.documents_path),
            ("rules", self.rules_path),
            ("source", self.source_path(version)),
        ):
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise JsonInvalidError(f"cannot read {label} file {path}: {exc}") from exc
            pieces.extend(
                [
                    label.encode("ascii"),
                    str(len(data)).encode("ascii"),
                    data,
                ]
            )
        return sha256_bytes(b"|".join(pieces))


def load_bundle(path: str | Path) -> Bundle:
    manifest_path = Path(path).resolve()
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JsonInvalidError(f"cannot load bundle manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise JsonInvalidError("bundle manifest must be an object")
    required = {
        "documents",
        "rules",
        "v1",
        "v2",
        "commands",
    }
    missing = required - set(value)
    if missing:
        raise JsonInvalidError(f"bundle manifest missing keys: {sorted(missing)}")
    base = manifest_path.parent

    def resolve(name: str) -> Path:
        candidate = Path(value[name])
        return candidate if candidate.is_absolute() else (base / candidate).resolve()

    injections = value.get("injections")
    injections_path = resolve("injections") if injections else None
    return Bundle(
        manifest_path=manifest_path,
        base_dir=base,
        documents_path=resolve("documents"),
        rules_path=resolve("rules"),
        v1_path=resolve("v1"),
        v2_path=resolve("v2"),
        commands_path=resolve("commands"),
        injections_path=injections_path,
        max_workers=max(1, int(value.get("max_workers", 4))),
        default_source_version=str(value.get("default_source_version", "v1")),
    )


def load_commands(path: str | Path) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise JsonInvalidError(f"cannot read command stream: {exc}") from exc
    for line_index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JsonInvalidError(
                f"invalid command JSON at line {line_index + 1}: {exc.msg}"
            ) from exc
        if not isinstance(command, dict):
            raise JsonInvalidError(f"command line {line_index + 1} must be an object")
        for required in ("op", "command_id", "type"):
            if required not in command:
                raise JsonInvalidError(
                    f"command line {line_index + 1} missing key {required!r}"
                )
        if isinstance(command["op"], bool) or not isinstance(command["op"], int):
            raise JsonInvalidError(f"command line {line_index + 1} op must be an integer")
        commands.append(command)
    return commands


def first_loaded_source(commands: list[dict[str, Any]], fallback: str) -> str:
    for command in commands:
        if command.get("type") == "load_project":
            value = str(command.get("source_version", fallback))
            if value not in ("v1", "v2"):
                raise JsonInvalidError(f"load_project source_version {value!r} is invalid")
            return value
    return fallback
