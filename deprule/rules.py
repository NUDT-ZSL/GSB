"""Rule snapshot readers, canonical IR and cross-format migrators."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .common import (
    canonical_hash,
    file_sha256,
    read_json,
    short_hash,
    write_json,
)
from .errors import Codes, DomainError


PARSER_CONFIG_VERSION = "det-parser-v1"

SYNTAX_TAGS = [
    "ROOT",
    "PRON",
    "NOUN",
    "PROPN",
    "VERB",
    "AUX",
    "ADV",
    "ADJ",
    "DET",
    "ADP",
    "PART",
    "PUNCT",
    "DEP",
]

DEPENDENCY_LABELS = [
    "root",
    "nsubj",
    "aux",
    "advmod",
    "det",
    "amod",
    "case",
    "compound",
    "obj",
    "obl",
    "punct",
    "dep",
]

DEFAULT_CONSTRAINTS = {
    "single_root": True,
    "acyclic": True,
    "one_head_per_token": True,
    "direction_contract": True,
}

DEFAULT_IDEMPOTENCY = {
    "arc_identity": ["head", "dependent", "label"],
    "deduplicate": True,
    "reverse_conflict_policy": "drop_reversed_duplicate",
    "direction_replay": "same_direction_same_arcs",
    "idempotency_scope": "sentence+rule+config",
}

DIRECTION_BY_LABEL = {
    "nsubj": "dependent_before_head",
    "aux": "dependent_before_head",
    "advmod": "dependent_before_head",
    "det": "dependent_before_head",
    "amod": "dependent_before_head",
    "case": "dependent_before_head",
    "compound": "dependent_before_head",
    "obj": "head_before_dependent",
    "obl": "head_before_dependent",
    "punct": "head_before_dependent",
    "root": "artificial_root",
    "dep": "any",
}

DIRECTION_TO_CODE = {
    "dependent_before_head": "pre",
    "head_before_dependent": "post",
    "any": "any",
    "artificial_root": "root",
}
CODE_TO_DIRECTION = {value: key for key, value in DIRECTION_TO_CODE.items()}

SEMANTIC_KEYS = (
    "rule_id",
    "rule_revision",
    "syntax_tags",
    "dependency_labels",
    "direction",
    "root_policy",
    "constraints",
)


def format_error(field: str, message: str, **position: Any) -> DomainError:
    details = {"field": field}
    details.update(position)
    return DomainError(message, code=Codes.E_FORMAT_CORRUPT, **details)


def require_key(doc: Any, key: str, location: str) -> Any:
    if not isinstance(doc, dict):
        raise format_error(location, f"{location} must be an object")
    if key not in doc:
        raise format_error(f"{location}.{key}", f"missing field: {key}")
    value = doc[key]
    if value is None or (isinstance(value, str) and value == ""):
        raise format_error(f"{location}.{key}", f"field is empty: {key}")
    return value


def require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise format_error(field, "tag set must be a JSON array of strings")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise format_error(
                f"{field}[{index}]",
                "tag set item must be a string",
                item_type=type(item).__name__,
            )
        if not item:
            raise format_error(f"{field}[{index}]", "tag set item must not be empty")
    if not value:
        raise format_error(field, "tag set must not be empty")
    return list(value)


def parse_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise format_error(field, "integer field must not be boolean")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise format_error(field, "value cannot be parsed as an integer", value=value) from exc
    if number == 0:
        raise format_error(field, "zero value is not allowed for positive integer field", value=0)
    if number < 0:
        raise format_error(field, "negative value is not allowed", value=number)
    return number


def split_csv(value: str, field: str) -> list[str]:
    if not isinstance(value, str):
        raise format_error(field, "CSV field must be a string")
    return [item.strip() for item in value.split(",") if item.strip()]


def canonical_ir(*, revision: int = 1, changed: bool = False, extended: bool = False) -> dict:
    """Build the canonical semantic rule set used by deterministic samples."""

    priority = ["AUX", "VERB"] if changed else ["VERB", "AUX"]
    root_policy = {
        "strategy": "pos_priority_first",
        "pos_priority": priority,
        "search_window": 16,
        "root_label": "root",
        "single_root": True,
        "fallback": {"strategy": "preanalysis_head"},
        "fallback_chain": [
            {"strategy": "preanalysis_head"},
            {"strategy": "first_content"},
        ],
    }
    direction = {
        "default_direction": "any",
        "by_label": dict(DIRECTION_BY_LABEL),
        "idempotency": dict(DEFAULT_IDEMPOTENCY),
    }
    ir = {
        "rule_id": "mini-syntax-dependency-rules",
        "rule_revision": int(revision),
        "syntax_tags": list(SYNTAX_TAGS),
        "dependency_labels": list(DEPENDENCY_LABELS),
        "direction": direction,
        "root_policy": root_policy,
        "constraints": dict(DEFAULT_CONSTRAINTS),
        "provenance": [
            {"field": "*", "action": "seed", "source": "deprule.seed:fixed-canonical-rules"}
        ],
    }
    if extended:
        ir["extensions"] = {
            "direction_lang_overrides": {
                "zh": {"aux": "head_before_dependent"},
            },
            "extra_fallback_chain": [{"strategy": "rightmost_content"}],
        }
    return finalize_ir(ir)


def finalize_ir(ir: dict) -> dict:
    ir["syntax_tags"] = sorted(set(ir.get("syntax_tags", [])))
    ir["dependency_labels"] = sorted(set(ir.get("dependency_labels", [])))
    ir.setdefault("constraints", dict(DEFAULT_CONSTRAINTS))
    ir.setdefault("provenance", [])

    ir["tag_set_checksum"] = "sha256:" + canonical_hash(ir["syntax_tags"])
    ir["tag_set_version"] = "tagset-" + short_hash(ir["tag_set_checksum"].split(":", 1)[1])
    ir["label_set_checksum"] = "sha256:" + canonical_hash(ir["dependency_labels"])
    ir["label_set_version"] = "labelset-" + short_hash(ir["label_set_checksum"].split(":", 1)[1])
    ir["direction_version"] = "direction-" + short_hash(canonical_hash(ir["direction"]))
    ir["root_policy_version"] = "rootpolicy-" + short_hash(canonical_hash(ir["root_policy"]))
    ir["semantic_hash"] = canonical_hash(semantic_view(ir))
    return ir


def semantic_view(ir: dict) -> dict:
    return {key: ir.get(key) for key in SEMANTIC_KEYS}


def diff_first(expected: Any, actual: Any, path: str = "$") -> str | None:
    """Return the first deterministic semantic difference path."""

    if isinstance(expected, dict) and isinstance(actual, dict):
        keys = sorted(set(expected) | set(actual))
        for key in keys:
            next_path = f"{path}.{key}" if path != "$" else f"$.{key}"
            if key not in expected:
                return next_path + ":unexpected"
            if key not in actual:
                return next_path + ":missing"
            found = diff_first(expected[key], actual[key], next_path)
            if found:
                return found
        return None

    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return f"{path}:length({len(expected)}!={len(actual)})"
        for index, (left, right) in enumerate(zip(expected, actual)):
            found = diff_first(left, right, f"{path}[{index}]")
            if found:
                return found
        return None

    if expected != actual:
        return f"{path}:value"
    return None


def assert_ir_equivalent(expected: dict, actual: dict, context: str) -> None:
    difference = diff_first(semantic_view(expected), semantic_view(actual))
    if difference:
        raise DomainError(
            f"semantic drift while {context}: first difference at {difference}",
            code=Codes.E_SEMANTIC_DRIFT,
            difference_field=difference,
            context=context,
            expected_semantic_hash=expected.get("semantic_hash"),
            actual_semantic_hash=actual.get("semantic_hash"),
        )


def normalize_version_header(version: Any) -> str | None:
    if isinstance(version, bool):
        return None
    if version in ("v1", "1", 1, "1.0", 1.0):
        return "v1"
    if version in ("v2", "2", 2, "2.0", 2.0):
        return "v2"
    if version in ("v3", "3", 3, "3.0", 3.0):
        return "v3"
    return None


def detect_format(doc: Any) -> str:
    if not isinstance(doc, dict):
        raise format_error("$", "snapshot root must be a JSON object")
    version = doc.get("version", doc.get("schema_version", doc.get("format_version")))
    if version is not None:
        normalized = normalize_version_header(version)
        if normalized is None:
            raise DomainError(
                f"unsupported rule version: {version!r}",
                code=Codes.E_VERSION_UNSUPPORTED,
                field="version",
                version=version,
                supported=["v1", "v2", "v3"],
            )
        return normalized

    if "pipe_record" in doc:
        return "v1"
    if "shards" in doc and "meta_db" in doc:
        return "v2"
    if "rule" in doc and "checksums" in doc:
        return "v3"
    raise DomainError(
        "cannot dispatch rule snapshot: missing supported version header or field set",
        code=Codes.E_VERSION_UNSUPPORTED,
        field="version",
        supported=["v1", "v2", "v3"],
    )


def read_snapshot(path: str | Path) -> dict:
    p = Path(path)
    if p.is_dir():
        p = p / "v2_manifest.json"
    doc = read_json(p)
    fmt = detect_format(doc)
    if fmt == "v1":
        return read_v1(p, doc)
    if fmt == "v2":
        return read_v2(p, doc)
    return read_v3(p, doc)


def read_v1(path: Path, doc: dict) -> dict:
    record = require_key(doc, "pipe_record", "$")
    if not isinstance(record, str):
        raise format_error("pipe_record", "pipe_record must be a string")

    expected_fields = 11
    parts = record.split("|")
    if len(parts) != expected_fields:
        index = len(parts) if len(parts) < expected_fields else expected_fields
        raise format_error(
            f"pipe_record[{index}]",
            "pipe record field count mismatch",
            expected_fields=expected_fields,
            actual_fields=len(parts),
            first_failed_field=index,
        )

    if parts[0] != "V1":
        raise format_error("pipe_record[0]", "v1 record header must be V1", actual=parts[0])
    rule_id = parts[1].strip()
    if not rule_id:
        raise format_error("pipe_record[1]", "rule_id must not be empty")
    revision = parse_positive_int(parts[2], "pipe_record[2]")
    syntax_tags = require_string_list(split_csv(parts[3], "pipe_record[3]"), "pipe_record[3]")
    labels = require_string_list(split_csv(parts[4], "pipe_record[4]"), "pipe_record[4]")

    by_label: dict[str, str] = {}
    direction_field = "pipe_record[5]"
    for assignment_index, assignment in enumerate(parts[5].split(";")):
        assignment = assignment.strip()
        if not assignment:
            continue
        if "=" not in assignment:
            raise format_error(
                f"{direction_field}[{assignment_index}]",
                "direction assignment must use label=code",
                assignment=assignment,
            )
        label, code = assignment.split("=", 1)
        label = label.strip()
        code = code.strip()
        if not label or code not in CODE_TO_DIRECTION:
            raise format_error(
                f"{direction_field}[{assignment_index}]",
                "unknown direction code or empty label",
                allowed_codes=sorted(CODE_TO_DIRECTION),
            )
        by_label[label] = CODE_TO_DIRECTION[code]

    strategy = parts[6].strip()
    if strategy not in {"pos_priority_first", "fixed_token", "first_verb"}:
        raise format_error("pipe_record[6]", "unsupported root strategy", strategy=strategy)
    priority = split_csv(parts[7], "pipe_record[7]")
    if strategy == "pos_priority_first" and not priority:
        raise format_error("pipe_record[7]", "POS priority must not be empty")
    fallback_strategy = parts[8].strip()
    if fallback_strategy not in {"preanalysis_head", "first_content", "rightmost_content"}:
        raise format_error(
            "pipe_record[8]", "unsupported fallback strategy", strategy=fallback_strategy
        )
    window = parse_positive_int(parts[9], "pipe_record[9]")
    root_label = parts[10].strip()
    if not root_label:
        raise format_error("pipe_record[10]", "root label must not be empty")
    if root_label not in labels:
        raise format_error(
            "pipe_record[10]", "root label must be present in dependency label set", label=root_label
        )

    provenance = [
        {"field": "direction.idempotency", "action": "fill_default", "source": "v1-upgrade-default:idempotency"},
        {"field": "constraints", "action": "fill_default", "source": "v1-upgrade-default:constraints"},
        {"field": "root_policy.single_root", "action": "fill_default", "source": "v1-upgrade-default:true"},
        {
            "field": "root_policy.fallback_chain[0]",
            "action": "expand_scalar",
            "source": "v1:pipe_record[8]",
            "value": fallback_strategy,
        },
        {
            "field": "root_policy.fallback_chain[1]",
            "action": "fill_default",
            "source": "v1-upgrade-default:terminal-first_content",
        },
        {"field": "tag_set_checksum", "action": "compute", "source": "sha256:canonical(syntax_tags)"},
        {"field": "label_set_checksum", "action": "compute", "source": "sha256:canonical(dependency_labels)"},
    ]

    ir = {
        "rule_id": rule_id,
        "rule_revision": revision,
        "syntax_tags": syntax_tags,
        "dependency_labels": labels,
        "direction": {
            "default_direction": "any",
            "by_label": by_label,
            "idempotency": dict(DEFAULT_IDEMPOTENCY),
        },
        "root_policy": {
            "strategy": strategy,
            "pos_priority": priority,
            "search_window": window,
            "root_label": root_label,
            "single_root": True,
            "fallback": {"strategy": fallback_strategy},
            "fallback_chain": [
                {"strategy": fallback_strategy},
                {"strategy": "first_content"},
            ],
        },
        "constraints": dict(DEFAULT_CONSTRAINTS),
        "provenance": provenance,
    }
    finalize_ir(ir)
    source_hash = "sha256:" + canonical_hash(doc)
    ir["source_hash"] = source_hash
    return {"format": "v1", "path": str(path), "source_hash": source_hash, "ir": ir, "raw": doc}


def _sqlite_metadata(db_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    if not db_path.exists():
        raise format_error("meta_db", "v2 SQLite metadata database is missing", path=str(db_path))
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            meta = {
                str(row[0]): str(row[1])
                for row in conn.execute("SELECT key, value FROM rule_meta ORDER BY key")
            }
            checksums = {
                str(row[0]): str(row[1])
                for row in conn.execute("SELECT shard, sha256 FROM shard_checksum ORDER BY shard")
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise format_error("meta_db", f"cannot read SQLite metadata: {exc}") from exc
    return meta, checksums


def read_v2(path: Path, doc: dict) -> dict:
    version = require_key(doc, "version", "$")
    if normalize_version_header(version) != "v2":
        raise DomainError(
            f"unsupported v2 version header: {version!r}",
            code=Codes.E_VERSION_UNSUPPORTED,
            field="version",
        )
    shards = require_key(doc, "shards", "$")
    if not isinstance(shards, dict):
        raise format_error("shards", "shards must be an object")
    for key in ("tags", "direction", "root"):
        require_key(shards, key, "shards")
    meta_db_name = require_key(doc, "meta_db", "$")
    meta_db = (path.parent / str(meta_db_name)).resolve()
    meta, db_checksums = _sqlite_metadata(meta_db)
    if meta.get("format_version") != "v2":
        raise format_error("sqlite.rule_meta.format_version", "SQLite metadata is not v2", value=meta.get("format_version"))

    loaded: dict[str, Any] = {}
    for shard_name, relative in shards.items():
        shard_path = (path.parent / str(relative)).resolve()
        shard_doc = read_json(shard_path)
        loaded[shard_name] = shard_doc
        expected = db_checksums.get(str(relative)) or db_checksums.get(shard_name)
        actual = "sha256:" + file_sha256(shard_path)
        if expected and expected != actual:
            raise format_error(
                f"sqlite.shard_checksum.{relative}",
                "v2 shard checksum mismatch",
                expected=expected,
                actual=actual,
            )

    tags_doc = loaded["tags"]
    syntax_tags = require_string_list(
        require_key(tags_doc, "syntax_tags", "tags.json"), "tags.json.syntax_tags"
    )
    labels = require_string_list(
        require_key(tags_doc, "dependency_labels", "tags.json"), "tags.json.dependency_labels"
    )
    direction_doc = loaded["direction"]
    by_label = require_key(direction_doc, "by_label", "direction.json")
    if not isinstance(by_label, dict):
        raise format_error("direction.json.by_label", "by_label must be an object")
    idempotency = require_key(direction_doc, "idempotency", "direction.json")
    root_doc = loaded["root"]
    root_policy = require_key(root_doc, "root_policy", "root.json")
    if not isinstance(root_policy, dict):
        raise format_error("root.json.root_policy", "root_policy must be an object")
    parse_positive_int(
        require_key(root_policy, "search_window", "root.json.root_policy"),
        "root.json.root_policy.search_window",
    )
    require_key(root_policy, "fallback_chain", "root.json.root_policy")

    revision = parse_positive_int(doc.get("rule_revision", meta.get("rule_revision", 1)), "rule_revision")
    ir = {
        "rule_id": str(doc.get("rule_id") or meta.get("rule_id") or "mini-syntax-dependency-rules"),
        "rule_revision": revision,
        "syntax_tags": syntax_tags,
        "dependency_labels": labels,
        "direction": {
            "default_direction": direction_doc.get("default_direction", "any"),
            "by_label": by_label,
            "idempotency": idempotency,
        },
        "root_policy": root_policy,
        "constraints": doc.get("constraints", DEFAULT_CONSTRAINTS),
        "provenance": [{"field": "*", "action": "read", "source": "v2-shards+sqlite"}],
    }
    finalize_ir(ir)
    source_material = {
        "manifest": doc,
        "shards": loaded,
        "sqlite_meta": meta,
        "sqlite_checksums": db_checksums,
    }
    source_hash = "sha256:" + canonical_hash(source_material)
    ir["source_hash"] = source_hash
    return {"format": "v2", "path": str(path), "source_hash": source_hash, "ir": ir, "raw": source_material}


def verify_declared_checksum(doc: dict, name: str, value: Any) -> None:
    checksums = require_key(doc, "checksums", "$")
    expected = require_key(checksums, name, "checksums")
    actual = "sha256:" + canonical_hash(value)
    if expected != actual:
        raise format_error(
            f"checksums.{name}",
            "declared v3 checksum mismatch",
            expected=expected,
            actual=actual,
        )


def read_v3(path: Path, doc: dict) -> dict:
    version = require_key(doc, "version", "$")
    if normalize_version_header(version) != "v3":
        raise DomainError(
            f"unsupported v3 version header: {version!r}",
            code=Codes.E_VERSION_UNSUPPORTED,
            field="version",
        )
    rule = require_key(doc, "rule", "$")
    syntax = require_key(rule, "syntax", "rule")
    dependency = require_key(rule, "dependency", "rule")
    root = require_key(rule, "root", "rule")

    syntax_tags = require_string_list(
        require_key(syntax, "tag_set", "rule.syntax"), "rule.syntax.tag_set"
    )
    labels = require_string_list(
        require_key(syntax, "dependency_tag_set", "rule.syntax"),
        "rule.syntax.dependency_tag_set",
    )
    verify_declared_checksum(doc, "syntax_tags", syntax_tags)
    verify_declared_checksum(doc, "dependency_labels", labels)

    direction_container = require_key(dependency, "direction_map", "rule.dependency")
    if not isinstance(direction_container, dict):
        raise format_error("rule.dependency.direction_map", "direction_map must be an object")
    by_label: dict[str, str] = {}
    groups = direction_container.get("groups", {})
    if not isinstance(groups, dict):
        raise format_error("rule.dependency.direction_map.groups", "groups must be an object")
    for group_name in sorted(groups):
        mapping = groups[group_name]
        if not isinstance(mapping, dict):
            raise format_error(f"rule.dependency.direction_map.groups.{group_name}", "group must be object")
        for label in sorted(mapping):
            direction = mapping[label]
            if label in by_label and by_label[label] != direction:
                raise format_error(
                    f"rule.dependency.direction_map.groups.{group_name}.{label}",
                    "conflicting flattened direction",
                    existing=by_label[label],
                    actual=direction,
                )
            by_label[label] = direction
    flat = direction_container.get("flat", {})
    if flat:
        if not isinstance(flat, dict):
            raise format_error("rule.dependency.direction_map.flat", "flat must be object")
        by_label.update(flat)
    verify_declared_checksum(doc, "direction_map", {"default_direction": dependency.get("default_direction", "any"), "by_label": by_label})

    idempotency = require_key(dependency, "idempotency", "rule.dependency")
    decision = require_key(root, "decision", "rule.root")
    parse_positive_int(
        require_key(decision, "search_window", "rule.root.decision"),
        "rule.root.decision.search_window",
    )
    require_key(decision, "fallback_chain", "rule.root.decision")
    verify_declared_checksum(doc, "root_policy", decision)

    checksums = doc.get("checksums", {})
    declaration_expected = checksums.get("declaration")
    declaration_material = {key: value for key, value in checksums.items() if key != "declaration"}
    declaration_actual = "sha256:" + canonical_hash(declaration_material)
    if declaration_expected and declaration_expected != declaration_actual:
        raise format_error(
            "checksums.declaration",
            "v3 checksum declaration seal mismatch",
            expected=declaration_expected,
            actual=declaration_actual,
        )

    ir = {
        "rule_id": str(doc.get("rule_id", "mini-syntax-dependency-rules")),
        "rule_revision": parse_positive_int(doc.get("rule_revision", 1), "rule_revision"),
        "syntax_tags": syntax_tags,
        "dependency_labels": labels,
        "direction": {
            "default_direction": dependency.get("default_direction", "any"),
            "by_label": by_label,
            "idempotency": idempotency,
        },
        "root_policy": decision,
        "constraints": rule.get("constraints", DEFAULT_CONSTRAINTS),
        "provenance": [{"field": "*", "action": "read", "source": "v3-nested-checksum"}],
    }
    if "extensions" in rule:
        ir["extensions"] = rule["extensions"]
    finalize_ir(ir)
    source_hash = "sha256:" + canonical_hash(doc)
    ir["source_hash"] = source_hash
    return {"format": "v3", "path": str(path), "source_hash": source_hash, "ir": ir, "raw": doc}


def ir_to_v1_record(ir: dict, downgrade: str = "flatten") -> tuple[str, list[dict]]:
    if downgrade not in {"flatten", "discard"}:
        raise DomainError("downgrade policy must be flatten or discard", code=Codes.E_CONFLICT)
    actions: list[dict] = []
    direction = dict(ir["direction"]["by_label"])

    extensions = ir.get("extensions", {})
    overrides = extensions.get("direction_lang_overrides", {})
    if overrides:
        if downgrade == "discard":
            actions.append({"field": "extensions.direction_lang_overrides", "action": "discard", "languages": sorted(overrides)})
        else:
            for lang in sorted(overrides):
                mapping = overrides[lang]
                for label in sorted(mapping):
                    actions.append({
                        "field": f"extensions.direction_lang_overrides.{lang}.{label}",
                        "action": "flatten_merge_global",
                        "from": direction.get(label),
                        "to": mapping[label],
                    })
                    direction[label] = mapping[label]

    assignments = []
    for label in sorted(direction):
        value = direction[label]
        code = DIRECTION_TO_CODE.get(value)
        if code is None:
            actions.append({"field": f"direction.by_label.{label}", "action": "discard_unknown_direction", "value": value})
            continue
        assignments.append(f"{label}={code}")

    root_policy = ir["root_policy"]
    fallback_chain = root_policy.get("fallback_chain", [root_policy.get("fallback", {})])
    primary_fallback = fallback_chain[0].get("strategy", "preanalysis_head") if fallback_chain else "preanalysis_head"
    if len(fallback_chain) > 1:
        action = "flatten_chain_to_primary" if downgrade == "flatten" else "discard_chain_remainder"
        actions.append({
            "field": "root_policy.fallback_chain",
            "action": action,
            "kept": primary_fallback,
            "dropped": [item.get("strategy") for item in fallback_chain[1:]],
        })
    extra_chain = extensions.get("extra_fallback_chain", [])
    if extra_chain:
        actions.append({
            "field": "extensions.extra_fallback_chain",
            "action": "flatten_chain_to_primary" if downgrade == "flatten" else "discard",
            "dropped": [item.get("strategy") for item in extra_chain],
        })

    fields = [
        "V1",
        ir["rule_id"],
        str(ir["rule_revision"]),
        ",".join(ir["syntax_tags"]),
        ",".join(ir["dependency_labels"]),
        ";".join(assignments),
        root_policy["strategy"],
        ",".join(root_policy.get("pos_priority", [])),
        primary_fallback,
        str(root_policy["search_window"]),
        root_policy["root_label"],
    ]
    record = "|".join(fields)
    return record, actions


def ir_to_v1_doc(ir: dict, downgrade: str = "flatten") -> dict:
    record, actions = ir_to_v1_record(ir, downgrade=downgrade)
    return {
        "version": "v1",
        "rule_id": ir["rule_id"],
        "rule_revision": ir["rule_revision"],
        "format_note": "pipe-record compatibility snapshot",
        "pipe_record": record,
        "downgrade_actions": actions,
    }


def ir_to_v3_doc(ir: dict) -> dict:
    direction_value = {
        "default_direction": ir["direction"].get("default_direction", "any"),
        "by_label": ir["direction"]["by_label"],
    }
    groups = {
        "declared": {
            label: value
            for label, value in sorted(ir["direction"]["by_label"].items())
        }
    }
    rule = {
        "syntax": {
            "tag_set": ir["syntax_tags"],
            "dependency_tag_set": ir["dependency_labels"],
        },
        "dependency": {
            "default_direction": ir["direction"].get("default_direction", "any"),
            "direction_map": {"groups": groups},
            "idempotency": ir["direction"]["idempotency"],
        },
        "root": {"decision": ir["root_policy"]},
        "constraints": ir["constraints"],
    }
    if ir.get("extensions"):
        rule["extensions"] = ir["extensions"]
    checksums = {
        "syntax_tags": "sha256:" + canonical_hash(ir["syntax_tags"]),
        "dependency_labels": "sha256:" + canonical_hash(ir["dependency_labels"]),
        "direction_map": "sha256:" + canonical_hash(direction_value),
        "root_policy": "sha256:" + canonical_hash(ir["root_policy"]),
    }
    checksums["declaration"] = "sha256:" + canonical_hash(checksums)
    return {
        "version": "v3",
        "rule_id": ir["rule_id"],
        "rule_revision": ir["rule_revision"],
        "checksums": checksums,
        "rule": rule,
        "migration_provenance": ir.get("provenance", []),
    }


def write_v2_bundle(target_dir: str | Path, ir: dict) -> dict:
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    tags_doc = {
        "version": "v2",
        "syntax_tags": ir["syntax_tags"],
        "dependency_labels": ir["dependency_labels"],
        "tag_set_checksum": ir["tag_set_checksum"],
        "label_set_checksum": ir["label_set_checksum"],
    }
    direction_doc = {
        "version": "v2",
        "default_direction": ir["direction"].get("default_direction", "any"),
        "by_label": ir["direction"]["by_label"],
        "idempotency": ir["direction"]["idempotency"],
    }
    root_doc = {"version": "v2", "root_policy": ir["root_policy"]}
    write_json(target / "tags.json", tags_doc)
    write_json(target / "direction.json", direction_doc)
    write_json(target / "root.json", root_doc)

    db_path = target / "rules-meta.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE rule_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE shard_checksum (shard TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
        meta_rows = [
            ("format_version", "v2"),
            ("rule_id", ir["rule_id"]),
            ("rule_revision", str(ir["rule_revision"])),
            ("tag_set_version", ir["tag_set_version"]),
            ("root_policy_version", ir["root_policy_version"]),
        ]
        conn.executemany("INSERT INTO rule_meta VALUES (?, ?)", meta_rows)
        for relative in ("tags.json", "direction.json", "root.json"):
            digest = "sha256:" + file_sha256(target / relative)
            conn.execute("INSERT INTO shard_checksum VALUES (?, ?)", (relative, digest))
        conn.commit()
    finally:
        conn.close()

    manifest = {
        "version": "v2",
        "format": "sharded-json+sqlite",
        "rule_id": ir["rule_id"],
        "rule_revision": ir["rule_revision"],
        "meta_db": "rules-meta.sqlite",
        "shards": {
            "tags": "tags.json",
            "direction": "direction.json",
            "root": "root.json",
        },
        "migration_provenance": ir.get("provenance", []),
    }
    write_json(target / "v2_manifest.json", manifest)
    return manifest


def migrate_snapshot(source_path: str | Path, target_format: str, target: str | Path,
                     downgrade: str = "flatten", verify: bool = True) -> dict:
    bundle = read_snapshot(source_path)
    ir = bundle["ir"]
    target_path = Path(target)
    actions: list[dict] = []

    if target_format == "v1":
        doc = ir_to_v1_doc(ir, downgrade=downgrade)
        actions = doc.get("downgrade_actions", [])
        write_json(target_path, doc)
        output_path = target_path
    elif target_format == "v2":
        write_v2_bundle(target_path if target_path.suffix == "" else target_path.parent, ir)
        output_path = target_path if target_path.is_dir() else target_path.parent / "v2_manifest.json"
    elif target_format == "v3":
        doc = ir_to_v3_doc(ir)
        write_json(target_path, doc)
        output_path = target_path
    elif target_format == "ir":
        write_json(target_path, ir)
        output_path = target_path
    else:
        raise DomainError(f"unsupported target format: {target_format}", code=Codes.E_VERSION_UNSUPPORTED)

    report = {
        "source_format": bundle["format"],
        "target_format": target_format,
        "output_path": str(output_path),
        "downgrade_actions": actions,
        "completion_sources": ir.get("provenance", []),
        "source_semantic_hash": ir["semantic_hash"],
    }
    if verify and target_format in {"v1", "v2", "v3"}:
        reread = read_snapshot(output_path)
        try:
            assert_ir_equivalent(ir, reread["ir"], f"migrate {bundle['format']}->{target_format}")
            report["verified"] = True
        except DomainError as exc:
            if downgrade == "discard" and actions:
                report["verified"] = False
                report["accepted_lossy"] = True
                report["drift"] = exc.details
            else:
                raise
    return report
