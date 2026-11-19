"""Sample loading and strict pre-write validation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BizError, canonical_json, digest
from .rules import (
    QueryTemplate,
    RuleSnapshot,
    validate_query_against_snapshot,
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BizError("E_BAD_SAMPLE", "duplicate JSON object key", details={"key": key})
        result[key] = value
    return result


def read_json_file(path: Path, code: str = "E_BAD_SAMPLE") -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BizError(code, "cannot read JSON file", details={"path": str(path)}) from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except BizError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BizError(code, "invalid JSON", details={"path": str(path), "reason": str(exc)}) from exc


@dataclass(frozen=True)
class RulePackage:
    version: str
    digest: str
    effective_clock_ms: int
    rollback_target: str | None
    snapshot: RuleSnapshot

    @classmethod
    def from_dict(cls, raw: Any) -> "RulePackage":
        if not isinstance(raw, dict):
            raise BizError("E_BAD_RULE", "rule package must be an object")
        version = raw.get("version")
        if not isinstance(version, str) or not version or len(version) > 64:
            raise BizError("E_BAD_RULE", "rule version must be a non-empty string")
        package_digest = raw.get("digest")
        if not isinstance(package_digest, str) or not package_digest.startswith("sha256:"):
            raise BizError("E_BAD_RULE", "rule digest must use sha256:<hex>", details={"version": version})
        effective_clock_ms = raw.get("effective_clock_ms")
        if type(effective_clock_ms) is not int or effective_clock_ms < 0:
            raise BizError(
                "E_BAD_RULE",
                "effective_clock_ms must be a non-negative integer",
                details={"version": version},
            )
        rollback_target = raw.get("rollback_target")
        if rollback_target is not None and not isinstance(rollback_target, str):
            raise BizError(
                "E_BAD_RULE",
                "rollback_target must be null or a version string",
                details={"version": version},
            )
        snapshot = RuleSnapshot.from_dict(
            {
                "logical_tables": raw.get("logical_tables"),
                "physical_mappings": raw.get("physical_mappings"),
            }
        )
        computed_digest = snapshot.content_digest()
        if computed_digest != package_digest:
            raise BizError(
                "E_BAD_RULE",
                "rule content digest does not match the package",
                details={
                    "actual_digest": computed_digest,
                    "declared_digest": package_digest,
                    "version": version,
                },
            )
        return cls(
            version=version,
            digest=package_digest,
            effective_clock_ms=effective_clock_ms,
            rollback_target=rollback_target,
            snapshot=snapshot,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "digest": self.digest,
            "effective_clock_ms": self.effective_clock_ms,
            "rollback_target": self.rollback_target,
            "version": self.version,
            **self.snapshot.to_dict(),
        }
        return result


@dataclass(frozen=True)
class Sample:
    path: Path
    root: Path
    sample_digest: str
    initial_version: str
    rules: dict[str, RulePackage]
    query_templates: dict[str, QueryTemplate]

    def rule(self, version: str) -> RulePackage:
        try:
            return self.rules[version]
        except KeyError as exc:
            raise BizError(
                "E_RULE_NOT_FOUND",
                "rule version is not present in the sample",
                details={"version": version, "known_versions": sorted(self.rules)},
            ) from exc

    def query_template(self, template_id: str) -> QueryTemplate:
        try:
            return self.query_templates[template_id]
        except KeyError as exc:
            raise BizError(
                "E_TEMPLATE_NOT_FOUND",
                "query template is not present in the sample",
                details={"known_templates": sorted(self.query_templates), "query_template": template_id},
            ) from exc


def _open_physical(path: Path, details: dict[str, Any]) -> sqlite3.Connection:
    if not path.is_file():
        raise BizError(
            "E_BAD_MAPPING",
            "physical SQLite database does not exist or is not a file",
            details={**details, "path": str(path)},
        )
    uri = "file:" + path.resolve().as_posix() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("SELECT 1").fetchone()
        return connection
    except sqlite3.Error as exc:
        raise BizError(
            "E_BAD_MAPPING",
            "physical SQLite database cannot be opened",
            details={**details, "path": str(path), "reason": str(exc)},
        ) from exc


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _validate_physical_mapping(
    root: Path,
    package: RulePackage,
    cache: dict[tuple[str, str], Any],
) -> None:
    for logical_name, table in package.snapshot.logical_tables.items():
        for shard_id, mapping in package.snapshot.mappings[logical_name].items():
            for role, entry in (("primary", mapping.primary), ("standby", mapping.standby)):
                if entry is None:
                    continue
                details = {
                    "logical_table": logical_name,
                    "role": role,
                    "rule_version": package.version,
                    "shard_id": shard_id,
                }
                db_path = root / entry.database
                cache_key = (str(db_path.resolve()), entry.table)
                if cache_key not in cache:
                    connection = _open_physical(db_path, details)
                    try:
                        row = connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (entry.table,),
                        ).fetchone()
                        if row is None:
                            raise BizError(
                                "E_BAD_MAPPING",
                                "physical table is missing",
                                details={**details, "database": entry.database, "table": entry.table},
                            )
                        columns = {
                            str(item[1]): int(item[6])
                            for item in connection.execute(
                                f"PRAGMA table_xinfo({_quote_identifier(entry.table)})"
                            ).fetchall()
                        }
                        missing = sorted(table.column_names - set(columns))
                        if missing:
                            raise BizError(
                                "E_BAD_MAPPING",
                                "physical table is missing logical columns",
                                details={
                                    **details,
                                    "database": entry.database,
                                    "missing_columns": missing,
                                    "table": entry.table,
                                },
                            )
                        for column in table.columns:
                            if column.generated and columns.get(column.name, 0) not in (2, 3):
                                raise BizError(
                                    "E_BAD_MAPPING",
                                    "declared generated column is not generated in the physical table",
                                    details={
                                        **details,
                                        "column": column.name,
                                        "database": entry.database,
                                        "table": entry.table,
                                    },
                                )
                        if role == "primary":
                            try:
                                values = [
                                    int(item[0])
                                    for item in connection.execute(
                                        f"SELECT {_quote_identifier(table.shard_key)} "
                                        f"FROM {_quote_identifier(entry.table)}"
                                    ).fetchall()
                                ]
                            except (sqlite3.Error, TypeError, ValueError) as exc:
                                raise BizError(
                                    "E_BAD_MAPPING",
                                    "cannot read shard key values from the physical table",
                                    details={
                                        **details,
                                        "database": entry.database,
                                        "table": entry.table,
                                        "reason": str(exc),
                                    },
                                ) from exc
                            for value in values:
                                try:
                                    actual_shard = table.shard_for_value(value)
                                except BizError as exc:
                                    raise BizError(
                                        "E_BAD_MAPPING",
                                        "physical data contains an out-of-domain shard key",
                                        details={
                                            **details,
                                            "database": entry.database,
                                            "shard_key": table.shard_key,
                                            "table": entry.table,
                                            "value": value,
                                        },
                                    ) from exc
                                if actual_shard != shard_id:
                                    raise BizError(
                                        "E_BAD_MAPPING",
                                        "physical data violates the declared shard rule",
                                        details={
                                            **details,
                                            "database": entry.database,
                                            "shard_key": table.shard_key,
                                            "table": entry.table,
                                            "value": value,
                                        },
                                    )
                    finally:
                        connection.close()
                    cache[cache_key] = True


def load_sample(path: str | Path) -> Sample:
    sample_path = Path(path)
    raw = read_json_file(sample_path)
    if not isinstance(raw, dict):
        raise BizError("E_BAD_SAMPLE", "sample root must be an object")
    if raw.get("format_version") != 1:
        raise BizError("E_BAD_SAMPLE", "unsupported sample format_version")
    initial_version = raw.get("initial_version")
    if not isinstance(initial_version, str) or not initial_version:
        raise BizError("E_BAD_SAMPLE", "initial_version must be a non-empty string")
    raw_rules = raw.get("rule_versions")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise BizError("E_BAD_RULE", "rule_versions must be a non-empty array")
    rules: dict[str, RulePackage] = {}
    for raw_rule in raw_rules:
        package = RulePackage.from_dict(raw_rule)
        if package.version in rules:
            raise BizError(
                "E_BAD_RULE",
                "rule versions must be unique",
                details={"version": package.version},
            )
        rules[package.version] = package
    if initial_version not in rules:
        raise BizError(
            "E_BAD_RULE",
            "initial_version is not present in rule_versions",
            details={"initial_version": initial_version},
        )
    for package in rules.values():
        if package.rollback_target is not None and package.rollback_target not in rules:
            raise BizError(
                "E_BAD_RULE",
                "rollback_target refers to an unknown version",
                details={"rollback_target": package.rollback_target, "version": package.version},
            )
    raw_templates = raw.get("query_templates")
    if not isinstance(raw_templates, list) or not raw_templates:
        raise BizError("E_BAD_TEMPLATE", "query_templates must be a non-empty array")
    templates: dict[str, QueryTemplate] = {}
    for raw_template in raw_templates:
        template = QueryTemplate.from_dict(raw_template)
        if template.id in templates:
            raise BizError(
                "E_BAD_TEMPLATE",
                "query template ids must be unique",
                details={"query_template": template.id},
            )
        templates[template.id] = template
    root = sample_path.resolve().parent
    for package in rules.values():
        for logical_name in package.snapshot.logical_tables:
            if logical_name not in package.snapshot.mappings:
                raise BizError(
                    "E_BAD_MAPPING",
                    "logical table has no mapping container",
                    details={"logical_table": logical_name, "version": package.version},
                )
    mapping_cache: dict[tuple[str, str], Any] = {}
    for package in rules.values():
        _validate_physical_mapping(root, package, mapping_cache)
    for template in templates.values():
        for package in rules.values():
            validate_query_against_snapshot(template, package.snapshot)
    return Sample(
        path=sample_path,
        root=root,
        sample_digest=digest(raw),
        initial_version=initial_version,
        rules=rules,
        query_templates=templates,
    )


def sample_summary(sample: Sample) -> dict[str, Any]:
    return {
        "initial_version": sample.initial_version,
        "query_templates": sorted(sample.query_templates),
        "rule_versions": sorted(sample.rules),
        "sample_digest": sample.sample_digest,
        "sample_root": str(sample.root),
    }


def dump_canonical(value: Any) -> str:
    return canonical_json(value)

