from __future__ import annotations

import copy
import json
import math
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import (
    E_FORMAT_CORRUPT,
    E_TARGET_EXISTS,
    E_VERSION_UNSUPPORTED,
    WorkbenchError,
)
from .util import (
    as_int,
    atomic_write_json,
    canonical_json_bytes,
    content_hash,
    ensure_parent,
    fsync_directory,
    json_type_name,
    read_json_file,
    require_list,
    require_mapping,
    require_string,
)


SCHEMA_VERSION = 1
METADATA_FIELDS = (
    "dictionary_version",
    "rule_version",
    "workers",
    "batch_size",
    "logical_clock_ms",
    "generation",
)
SHARD_FIELDS = {
    "core": ("locale", "domain", "normalization", "disambiguation", "limits"),
    "dictionary": ("dictionary",),
    "rules": ("rules",),
    "tagging": ("domain_tags", "domain_pos_table", "tagger"),
}
V2_ONLY_FIELDS = (
    "metadata_extensions",
    "migration_audit",
    "shard_integrity",
    "shard_provenance",
    "schema_version",
    "snapshot_id",
)
DEFAULT_UPGRADE_VALUES: dict[str, Any] = {
    "limits.max_batch_sentences": 64,
    "limits.max_workers": 8,
    "limits.max_token_cache_bytes": 4 * 1024 * 1024,
    "limits.resource_policy": "degrade_serial",
    "limits.estimated_bytes_per_token": 96,
    "disambiguation.priority": {
        "dictionary": 300,
        "prefix_rule": 200,
        "suffix_rule": 100,
    },
    "tagger.default_emission": 0.01,
    "tagger.dictionary_emission": 50.0,
    "tagger.unknown_emission": 50.0,
}


@dataclass
class ConfigBundle:
    data: dict[str, Any]
    format_version: int
    source: str
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def effective_hash(self) -> str:
        return content_hash(self.effective_data())

    @property
    def normalization_hash(self) -> str:
        return content_hash(
            {
                "locale": self.data["locale"],
                "domain": self.data["domain"],
                "normalization": self.data["normalization"],
            }
        )

    @property
    def dictionary_hash(self) -> str:
        return content_hash(
            {
                "dictionary_version": self.data["dictionary_version"],
                "dictionary": self.data["dictionary"],
            }
        )

    @property
    def rule_hash(self) -> str:
        return content_hash(
            {
                "rule_version": self.data["rule_version"],
                "rules": self.data["rules"],
            }
        )

    @property
    def disambiguation_hash(self) -> str:
        return content_hash(self.data["disambiguation"])

    @property
    def tagging_hash(self) -> str:
        return content_hash(
            {
                "domain": self.data["domain"],
                "domain_tags": self.data["domain_tags"],
                "domain_pos_table": self.data["domain_pos_table"],
                "tagger": self.data["tagger"],
            }
        )

    @property
    def semantic_hashes(self) -> dict[str, str]:
        return {
            "normalization": self.normalization_hash,
            "dictionary": self.dictionary_hash,
            "rules": self.rule_hash,
            "disambiguation": self.disambiguation_hash,
            "tagging": self.tagging_hash,
        }

    def effective_data(self) -> dict[str, Any]:
        ignored = {"format_version", "content_checksum", "migration_audit"}
        return {key: value for key, value in self.data.items() if key not in ignored}


@dataclass
class MigrationReport:
    source: str
    target: str
    source_version: int
    target_version: int
    audit: list[dict[str, Any]]
    dropped: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "audit": self.audit,
            "dropped": self.dropped,
        }


def _parse_version(value: Any) -> int:
    if isinstance(value, bool):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "format_version is not an integer",
            {"value": value, "type": json_type_name(value)},
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[vV]?\d+", value.strip()):
        return int(value.strip().lstrip("vV"))
    raise WorkbenchError(
        E_FORMAT_CORRUPT,
        "format_version is missing or not parseable",
        {"value": value, "type": json_type_name(value)},
    )


def _strip_checksum(data: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    body = copy.deepcopy(data)
    checksum = body.pop("content_checksum", None)
    return body, checksum


def read_config(source: str | Path, *, verify_checksum: bool = True) -> ConfigBundle:
    path = Path(source)
    if path.is_dir():
        manifest_path = path / "config.json"
        if not manifest_path.exists():
            manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "v2 configuration directory has no config.json or manifest.json",
                {"path": str(path)},
            )
        return _read_v2(manifest_path, verify_checksum=verify_checksum)

    raw = read_json_file(path)
    if not isinstance(raw, dict):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "configuration root must be an object",
            {"path": str(path)},
        )
    if "format_version" not in raw:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "configuration has no format_version header",
            {"path": str(path)},
        )
    version = _parse_version(raw["format_version"])
    if version == 1:
        return _read_v1(path, raw, verify_checksum=verify_checksum)
    if version == 2:
        return _read_v2(path, verify_checksum=verify_checksum)
    raise WorkbenchError(
        E_VERSION_UNSUPPORTED,
        f"unsupported configuration version: {version}",
        {"path": str(path), "format_version": version, "supported": [1, 2]},
    )


def _read_v1(
    path: Path, raw: dict[str, Any], *, verify_checksum: bool
) -> ConfigBundle:
    body, checksum = _strip_checksum(raw)
    if verify_checksum and checksum is not None:
        actual = content_hash(body)
        if checksum != actual:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "v1 content checksum mismatch",
                {"path": str(path), "expected": checksum, "actual": actual},
            )
    body["format_version"] = 1
    validate_config_data(body)
    return ConfigBundle(
        data=body,
        format_version=1,
        source=str(path),
        provenance={key: f"v1:{path.name}" for key in METADATA_FIELDS},
    )


def _merge_shard(
    merged: dict[str, Any], payload: dict[str, Any], shard_name: str
) -> None:
    for key, value in payload.items():
        if key == "format_version":
            continue
        if key in merged and merged[key] != value:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"conflicting field {key!r} while reading v2 shard {shard_name}",
                {"field": key, "shard": shard_name},
            )
        merged[key] = value


def _read_v2(manifest_path: Path, *, verify_checksum: bool) -> ConfigBundle:
    raw_manifest = read_json_file(manifest_path)
    if not isinstance(raw_manifest, dict):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "v2 manifest root must be an object",
            {"path": str(manifest_path)},
        )
    if "format_version" not in raw_manifest:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "v2 manifest has no format_version header",
            {"path": str(manifest_path)},
        )
    version = _parse_version(raw_manifest["format_version"])
    if version != 2:
        if version in (1,):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "v2 manifest path resolved to a non-v2 file",
                {"path": str(manifest_path), "format_version": version},
            )
        raise WorkbenchError(
            E_VERSION_UNSUPPORTED,
            f"unsupported configuration version: {version}",
            {"path": str(manifest_path), "format_version": version},
        )

    manifest = copy.deepcopy(raw_manifest)
    manifest_checksum = manifest.pop("manifest_checksum", None)
    if verify_checksum and manifest_checksum is not None:
        actual = content_hash(manifest)
        if actual != manifest_checksum:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "v2 manifest checksum mismatch",
                {"path": str(manifest_path), "expected": manifest_checksum, "actual": actual},
            )

    metadata_db = require_string(
        manifest.get("metadata_db"), "manifest.metadata_db"
    )
    db_path = (manifest_path.parent / metadata_db).resolve()
    if not db_path.exists():
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "v2 metadata database is missing",
            {"path": str(db_path)},
        )

    meta: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    db_shards: dict[str, str] = {}
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            meta_rows = connection.execute(
                "SELECT key, value, provenance FROM meta ORDER BY key"
            ).fetchall()
            for key, value_text, source in meta_rows:
                try:
                    meta[key] = json.loads(value_text)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise WorkbenchError(
                        E_FORMAT_CORRUPT,
                        f"unparseable v2 metadata value for {key}",
                        {"key": key, "value": value_text, "error": str(exc)},
                    ) from exc
                provenance[key] = str(source)
            try:
                shard_rows = connection.execute(
                    "SELECT name, sha256 FROM shards ORDER BY name"
                ).fetchall()
                db_shards = {str(name): str(sha) for name, sha in shard_rows}
            except sqlite3.OperationalError as exc:
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    "v2 metadata database has no valid shard checksum table",
                    {"path": str(db_path), "error": str(exc)},
                ) from exc
        finally:
            connection.close()
    except WorkbenchError:
        raise
    except sqlite3.Error as exc:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "cannot read v2 metadata database",
            {"path": str(db_path), "error": str(exc)},
        ) from exc

    for required in METADATA_FIELDS:
        if required not in meta:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"v2 metadata field missing: {required}",
                {"field": required, "path": str(db_path)},
            )

    shards = require_mapping(manifest.get("shards"), "manifest.shards")
    merged: dict[str, Any] = {}
    for name, descriptor in shards.items():
        if isinstance(descriptor, str):
            relative_path = descriptor
            manifest_checksum = None
            expected_checksum = db_shards.get(str(name))
        elif isinstance(descriptor, dict):
            relative_path = require_string(
                descriptor.get("path"), f"manifest.shards.{name}.path"
            )
            manifest_checksum = descriptor.get("sha256")
            expected_checksum = db_shards.get(str(name))
            if (
                manifest_checksum is not None
                and expected_checksum is not None
                and manifest_checksum != expected_checksum
            ):
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    f"v2 shard checksum metadata disagrees: {name}",
                    {
                        "shard": name,
                        "manifest": manifest_checksum,
                        "metadata_db": expected_checksum,
                    },
                )
        else:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"invalid shard descriptor: {name}",
                {"shard": name, "type": json_type_name(descriptor)},
            )
        if not isinstance(expected_checksum, str):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"v2 shard checksum is missing from metadata database: {name}",
                {"shard": name, "path": str(db_path)},
            )
        shard_path = (manifest_path.parent / relative_path).resolve()
        if not shard_path.exists():
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"v2 shard missing: {name}",
                {"shard": name, "path": str(shard_path)},
            )
        payload = read_json_file(shard_path)
        if not isinstance(payload, dict):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"v2 shard root is not an object: {name}",
                {"shard": name, "path": str(shard_path)},
            )
        if verify_checksum and expected_checksum is not None:
            actual = content_hash(payload)
            if actual != expected_checksum:
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    f"v2 shard checksum mismatch: {name}",
                    {
                        "shard": name,
                        "expected": expected_checksum,
                        "actual": actual,
                    },
                )
        _merge_shard(merged, payload, str(name))

    merged.update(meta)
    merged["format_version"] = 2
    validate_config_data(merged)
    return ConfigBundle(
        data=merged,
        format_version=2,
        source=str(manifest_path.parent),
        provenance=provenance,
    )


def validate_config_data(data: dict[str, Any]) -> None:
    require_mapping(data, "config")
    for key in ("locale", "domain", "dictionary_version", "rule_version"):
        require_string(data.get(key), key)
    as_int(data.get("workers"), "workers", minimum=1)
    as_int(data.get("batch_size"), "batch_size", minimum=1)
    as_int(data.get("logical_clock_ms"), "logical_clock_ms", minimum=0)
    as_int(data.get("generation"), "generation", minimum=1)
    normalization = require_mapping(data.get("normalization"), "normalization")
    unicode_form = normalization.get("unicode_form", "NFKC")
    if unicode_form not in ("NFKC", "NFC", "NFD", "NFKD", "none"):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "unsupported normalization.unicode_form",
            {"value": unicode_form},
        )
    if normalization.get("case_mode", "casefold") not in (
        "casefold",
        "lower",
        "preserve",
    ):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "unsupported normalization.case_mode",
            {"value": normalization.get("case_mode")},
        )
    if normalization.get("whitespace_mode", "collapse") not in (
        "collapse",
        "preserve",
    ):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "unsupported normalization.whitespace_mode",
            {"value": normalization.get("whitespace_mode")},
        )
    replacements = normalization.get("domain_replacements", {})
    require_mapping(replacements, "normalization.domain_replacements")
    for key, value in replacements.items():
        if (
            not isinstance(key, str)
            or len(key) != 1
            or not isinstance(value, str)
            or not value
        ):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "domain_replacements keys must be one code point and values non-empty strings",
                {"key": key, "value": value},
            )

    disambiguation = require_mapping(
        data.get("disambiguation"), "disambiguation"
    )
    priority = require_mapping(
        disambiguation.get("priority", {}), "disambiguation.priority"
    )
    for source in ("dictionary", "prefix_rule", "suffix_rule"):
        if source not in priority:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"disambiguation priority missing: {source}",
                {"field": "disambiguation.priority", "source": source},
            )
        if not isinstance(priority[source], (int, float)) or isinstance(
            priority[source], bool
        ):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"disambiguation priority is not numeric: {source}",
                {"field": "disambiguation.priority", "source": source},
            )
        if float(priority[source]) < 0:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"disambiguation priority must be non-negative: {source}",
                {"field": "disambiguation.priority", "source": source},
            )

    dictionary = require_list(data.get("dictionary"), "dictionary")
    entry_ids: set[str] = set()
    for index, entry in enumerate(dictionary):
        field = f"dictionary[{index}]"
        require_mapping(entry, field)
        entry_id = require_string(entry.get("entry_id"), f"{field}.entry_id")
        if entry_id in entry_ids:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"duplicate dictionary entry_id: {entry_id}",
                {"field": field, "entry_id": entry_id},
            )
        entry_ids.add(entry_id)
        require_string(entry.get("term"), f"{field}.term")
        require_string(entry.get("tag"), f"{field}.tag")
        if "version" in entry:
            require_string(entry.get("version"), f"{field}.version")
        for list_key in ("domains", "locales"):
            values = require_list(entry.get(list_key, []), f"{field}.{list_key}")
            if not all(isinstance(value, str) for value in values):
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    f"{field}.{list_key} entries must be strings",
                    {"field": f"{field}.{list_key}"},
                )

    rules = require_list(data.get("rules"), "rules")
    rule_ids: set[str] = set()
    for index, rule in enumerate(rules):
        field = f"rules[{index}]"
        require_mapping(rule, field)
        rule_id = require_string(rule.get("rule_id"), f"{field}.rule_id")
        if rule_id in rule_ids:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"duplicate rule_id: {rule_id}",
                {"field": field, "rule_id": rule_id},
            )
        rule_ids.add(rule_id)
        if rule.get("source") not in ("prefix_rule", "suffix_rule"):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"{field}.source must be prefix_rule or suffix_rule",
                {"field": f"{field}.source", "value": rule.get("source")},
            )
        pattern = require_string(rule.get("pattern"), f"{field}.pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"invalid regular expression in {field}",
                {"field": f"{field}.pattern", "error": str(exc)},
            ) from exc
        require_string(rule.get("tag"), f"{field}.tag")
        if "version" in rule:
            require_string(rule.get("version"), f"{field}.version")
        for list_key in ("domains", "locales"):
            values = require_list(rule.get(list_key, []), f"{field}.{list_key}")
            if not all(isinstance(value, str) for value in values):
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    f"{field}.{list_key} entries must be strings",
                    {"field": f"{field}.{list_key}"},
                )

    domain_tags = require_mapping(data.get("domain_tags"), "domain_tags")
    for domain, tags in domain_tags.items():
        tag_list = require_list(tags, f"domain_tags.{domain}")
        if not tag_list or not all(isinstance(tag, str) and tag for tag in tag_list):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"domain_tags.{domain} must be a non-empty string array",
                {"domain": domain},
            )
    active_domain = data["domain"]
    if active_domain not in domain_tags:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            f"active domain has no controlled tag set: {active_domain}",
            {"domain": active_domain},
        )
    active_tags = set(domain_tags[active_domain])
    for required_tag in ("UNK", "WS"):
        if required_tag not in active_tags:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"active domain tag set must include {required_tag}",
                {"domain": active_domain, "tag": required_tag},
            )
    for index, entry in enumerate(dictionary):
        entry_domains = entry.get("domains") or list(domain_tags)
        for domain in entry_domains:
            if domain not in domain_tags:
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    "dictionary entry references a domain without a tag set",
                    {"field": f"dictionary[{index}].domains", "domain": domain},
                )
            if entry["tag"] not in set(domain_tags[domain]):
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    "dictionary tag is outside a domain controlled set",
                    {
                        "field": f"dictionary[{index}].tag",
                        "tag": entry["tag"],
                        "domain": domain,
                    },
                )
    for index, rule in enumerate(rules):
        rule_domains = rule.get("domains") or list(domain_tags)
        for domain in rule_domains:
            if domain not in domain_tags:
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    "rule references a domain without a tag set",
                    {"field": f"rules[{index}].domains", "domain": domain},
                )
            if rule["tag"] not in set(domain_tags[domain]):
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    "rule tag is outside a domain controlled set",
                    {
                        "field": f"rules[{index}].tag",
                        "tag": rule["tag"],
                        "domain": domain,
                    },
                )
    domain_pos_table = require_mapping(
        data.get("domain_pos_table"), "domain_pos_table"
    )
    for domain, token_table in domain_pos_table.items():
        token_table = require_mapping(
            token_table, f"domain_pos_table.{domain}"
        )
        for token_text, tag_weights in token_table.items():
            tag_weights = require_mapping(
                tag_weights,
                f"domain_pos_table.{domain}.{token_text}",
            )
            for tag, weight in tag_weights.items():
                _require_nonnegative_number(
                    weight,
                    f"domain_pos_table.{domain}.{token_text}.{tag}",
                )
    tagger = require_mapping(data.get("tagger"), "tagger")
    initial = require_mapping(tagger.get("initial"), "tagger.initial")
    for tag, weight in initial.items():
        _require_nonnegative_number(weight, f"tagger.initial.{tag}")
    transitions = require_mapping(
        tagger.get("transitions"), "tagger.transitions"
    )
    for previous, row in transitions.items():
        row = require_mapping(row, f"tagger.transitions.{previous}")
        for tag, weight in row.items():
            _require_nonnegative_number(
                weight, f"tagger.transitions.{previous}.{tag}"
            )
    for key in (
        "default_emission",
        "dictionary_emission",
        "unknown_emission",
    ):
        if key in tagger:
            _require_nonnegative_number(tagger[key], f"tagger.{key}")
    limits = require_mapping(data.get("limits"), "limits")
    for key in (
        "max_batch_sentences",
        "max_workers",
        "max_token_cache_bytes",
        "estimated_bytes_per_token",
    ):
        as_int(limits.get(key), f"limits.{key}", minimum=1)
    if limits.get("resource_policy", "degrade_serial") not in (
        "degrade_serial",
        "fail",
    ):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            "limits.resource_policy must be degrade_serial or fail",
            {"value": limits.get("resource_policy")},
        )


def _require_nonnegative_number(value: Any, field: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            f"{field} must be a non-negative number",
            {"field": field, "value": value},
        )


def write_v1(
    bundle: ConfigBundle,
    target: str | Path,
    *,
    overwrite: bool = True,
) -> Path:
    path = Path(target)
    if path.exists() and not overwrite:
        raise WorkbenchError(
            E_TARGET_EXISTS, "v1 target already exists", {"path": str(path)}
        )
    data = copy.deepcopy(bundle.data)
    for field_name in V2_ONLY_FIELDS:
        data.pop(field_name, None)
    data.pop("content_checksum", None)
    data["format_version"] = 1
    validate_config_data(data)
    checksum = content_hash(data)
    data["content_checksum"] = checksum
    atomic_write_json(path, data)
    return path


def write_v2(
    bundle: ConfigBundle,
    target: str | Path,
    *,
    overwrite: bool = False,
    provenance: dict[str, str] | None = None,
) -> Path:
    directory = Path(target)
    if directory.exists() and any(directory.iterdir()) and not overwrite:
        raise WorkbenchError(
            E_TARGET_EXISTS,
            "v2 target directory is not empty",
            {"path": str(directory)},
        )
    directory.mkdir(parents=True, exist_ok=True)
    data = copy.deepcopy(bundle.data)
    validate_config_data(data)
    consumed = set(METADATA_FIELDS) | set(SHARD_FIELDS["core"]) | set(
        SHARD_FIELDS["dictionary"]
    ) | set(SHARD_FIELDS["rules"]) | set(SHARD_FIELDS["tagging"]) | {
        "format_version",
        "content_checksum",
    }
    extras = {
        key: value
        for key, value in data.items()
        if key not in consumed
    }

    shard_payloads: dict[str, dict[str, Any]] = {}
    for shard_name, fields in SHARD_FIELDS.items():
        shard_payloads[shard_name] = {
            field_name: copy.deepcopy(data[field_name])
            for field_name in fields
            if field_name in data
        }
    shard_payloads["extras"] = extras

    shard_checksums: dict[str, str] = {}
    for shard_name, payload in shard_payloads.items():
        shard_path = directory / f"{shard_name}.json"
        atomic_write_json(shard_path, payload)
        shard_checksums[shard_name] = content_hash(payload)

    db_path = directory / "metadata.sqlite3"
    db_tmp = directory / ".metadata.sqlite3.tmp"
    if db_tmp.exists():
        db_tmp.unlink()
    connection = sqlite3.connect(str(db_tmp))
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, provenance TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE shards (name TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL)"
        )
        for key in METADATA_FIELDS:
            source = (provenance or {}).get(key, f"config:{bundle.source}")
            connection.execute(
                "INSERT INTO meta(key, value, provenance) VALUES (?, ?, ?)",
                (key, json.dumps(data[key], ensure_ascii=False), source),
            )
        for shard_name, checksum in shard_checksums.items():
            connection.execute(
                "INSERT INTO shards(name, path, sha256) VALUES (?, ?, ?)",
                (shard_name, f"{shard_name}.json", checksum),
            )
        connection.execute(
            "INSERT INTO meta(key, value, provenance) VALUES (?, ?, ?)",
            (
                "schema_version",
                json.dumps(SCHEMA_VERSION),
                "generated:workbench-write-v2",
            ),
        )
        connection.execute(
            "INSERT INTO meta(key, value, provenance) VALUES (?, ?, ?)",
            (
                "snapshot_id",
                json.dumps(bundle.effective_hash),
                "computed:sha256-effective-config",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    os.replace(db_tmp, db_path)
    fsync_directory(directory)

    manifest = {
        "format_version": 2,
        "metadata_db": "metadata.sqlite3",
        "shards": {
            name: {"path": f"{name}.json", "sha256": checksum}
            for name, checksum in sorted(shard_checksums.items())
        },
    }
    manifest["manifest_checksum"] = content_hash(manifest)
    atomic_write_json(directory / "config.json", manifest)
    return directory


def _get_path(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_if_missing(data: dict[str, Any], dotted: str, value: Any) -> bool:
    parts = dotted.split(".")
    current: Any = data
    for part in parts[:-1]:
        next_value = current.get(part)
        if next_value is None:
            next_value = {}
            current[part] = next_value
        if not isinstance(next_value, dict):
            return False
        current = next_value
    if parts[-1] in current:
        return False
    current[parts[-1]] = copy.deepcopy(value)
    return True


def migrate_config(
    source: str | Path,
    target: str | Path,
    target_version: int,
    *,
    overwrite: bool = False,
) -> MigrationReport:
    bundle = read_config(source)
    if target_version not in (1, 2):
        raise WorkbenchError(
            E_VERSION_UNSUPPORTED,
            f"unsupported target version: {target_version}",
            {"target_version": target_version, "supported": [1, 2]},
        )

    audit: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    data = copy.deepcopy(bundle.data)

    if target_version == 2:
        data.pop("content_checksum", None)
        for key, value in DEFAULT_UPGRADE_VALUES.items():
            if _set_if_missing(data, key, value):
                audit.append(
                    {
                        "field": key,
                        "action": "fill",
                        "source": "v1-default",
                        "value": value,
                    }
                )
            else:
                audit.append(
                    {
                        "field": key,
                        "action": "preserve",
                        "source": "source-config",
                    }
                )
        for key in METADATA_FIELDS:
            audit.append(
                {
                    "field": f"metadata_db.{key}",
                    "action": "preserve",
                    "source": f"source-config:{key}",
                }
            )
        audit.extend(
            [
                {
                    "field": "metadata_db",
                    "action": "compute",
                    "source": "generated:sqlite3",
                },
                {
                    "field": "shards.checksum",
                    "action": "compute",
                    "source": "sha256:canonical-shard-json",
                },
                {
                    "field": "manifest.checksum",
                    "action": "compute",
                    "source": "sha256:canonical-manifest-json",
                },
            ]
        )
        if "metadata_extensions" not in data:
            data["metadata_extensions"] = {
                "snapshot_id": content_hash(data),
                "cache_namespace": "domain-seg-workbench",
            }
            audit.append(
                {
                    "field": "metadata_extensions",
                    "action": "fill",
                    "source": "v2-default",
                }
            )
        data["migration_audit"] = audit
        upgraded = ConfigBundle(
            data=data,
            format_version=2,
            source=str(source),
            provenance={
                key: next(
                    (
                        item["source"]
                        for item in audit
                        if item["field"] == f"metadata_db.{key}"
                    ),
                    "source-config",
                )
                for key in METADATA_FIELDS
            },
        )
        write_v2(
            upgraded,
            target,
            overwrite=overwrite,
            provenance=upgraded.provenance,
        )
    else:
        for field_name in V2_ONLY_FIELDS:
            if field_name in data:
                dropped.append(
                    {
                        "field": field_name,
                        "rule": "V1_DROP_RULE: v1 single-file schema has no v2-only container",
                    }
                )
                data.pop(field_name, None)
        dropped.append(
            {
                "field": "metadata_db.shards",
                "rule": "V1_DROP_RULE: v1 cannot express per-shard checksums",
            }
        )
        dropped.append(
            {
                "field": "manifest.manifest_checksum",
                "rule": "V1_DROP_RULE: v1 keeps only content_checksum",
            }
        )
        audit.append(
            {
                "field": "content_checksum",
                "action": "compute",
                "source": "sha256:canonical-v1-json",
            }
        )
        downgraded = ConfigBundle(
            data=data,
            format_version=1,
            source=str(source),
            provenance={},
        )
        write_v1(downgraded, target, overwrite=overwrite)

    return MigrationReport(
        source=str(source),
        target=str(target),
        source_version=bundle.format_version,
        target_version=target_version,
        audit=audit,
        dropped=dropped,
    )

