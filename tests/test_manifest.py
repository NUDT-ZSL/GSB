from __future__ import annotations

import hashlib
from pathlib import Path

from supply_diag.errors import (
    E_DUPLICATE_COMPONENT,
    E_HASH_ALGO,
    E_INVALID_JSON,
    E_SIGNATURE_STATUS,
    E_UNKNOWN_DEPENDENCY,
    E_VERSION_EXPR,
)
from supply_diag.manifest import parse_manifest, parse_manifest_file

from .helpers import component, content_bytes


def test_invalid_entries_are_rejected_with_indices_and_batch_continues() -> None:
    valid = component("valid")
    duplicate_a = component("dup")
    duplicate_b = component("dup")
    bad_algorithm = component("bad-algo")
    bad_algorithm["hash_algorithm"] = "md5"
    bad_signature = component("bad-signature")
    bad_signature["signature_status"] = "maybe"
    bad_version = component("bad-version")
    bad_version["version"] = "one.two"
    short_digest = component("short-digest")
    short_digest["content_hash"] = "abcd"

    parsed = parse_manifest(
        {
            "project_id": "p",
            "manifest_version": "1.0.0",
            "components": [
                valid,
                duplicate_a,
                duplicate_b,
                bad_algorithm,
                bad_signature,
                bad_version,
                short_digest,
            ],
        }
    )
    assert list(parsed.components) == ["valid"]
    assert {item.entry_index for item in parsed.rejections} == {2, 3, 4, 5, 6, 7}
    codes = {code for rejection in parsed.rejections for code in rejection.codes}
    assert E_DUPLICATE_COMPONENT in codes
    assert E_HASH_ALGO in codes
    assert E_SIGNATURE_STATUS in codes
    assert E_VERSION_EXPR in codes
    assert parsed.fatal is False


def test_unknown_dependency_is_reported_but_component_is_retained() -> None:
    parsed = parse_manifest(
        {
            "project_id": "p",
            "manifest_version": "1.0.0",
            "components": [
                component(
                    "root",
                    dependencies=[{"target": "absent", "constraint": ">=1.0.0"}],
                )
            ],
        }
    )
    assert "root" in parsed.components
    assert any(issue.code == E_UNKNOWN_DEPENDENCY for issue in parsed.errors)


def test_algorithm_and_digest_are_normalized() -> None:
    payload = content_bytes({"x": 1})
    parsed = parse_manifest(
        {
            "project_id": "p",
            "manifest_version": "1.0.0",
            "components": [
                {
                    "component_id": "a",
                    "version": "1.0.0",
                    "hash_algorithm": "SHA256",
                    "content_hash": hashlib.sha256(payload).hexdigest().upper(),
                    "signature_status": "已签",
                    "dependencies": [],
                    "content": {"x": 1},
                }
            ],
        }
    )
    component_result = parsed.components["a"]
    assert component_result.hash_algorithm == "sha256"
    assert component_result.content_hash == hashlib.sha256(payload).hexdigest()
    assert component_result.signature_status == "signed"


def test_invalid_json_file_is_reported_without_exception(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not-json", encoding="utf-8")
    parsed = parse_manifest_file(path)
    assert parsed.fatal
    assert parsed.errors[0].code == E_INVALID_JSON


def test_dependency_order_is_canonicalized() -> None:
    first = component(
        "root",
        dependencies=[
            {"target": "z", "constraint": ">=1.0.0"},
            {"target": "a", "constraint": "<2.0.0"},
        ],
    )
    second = component(
        "root",
        dependencies=[
            {"target": "a", "constraint": "<=1.999.0"},
            {"target": "z", "constraint": ">=1.0.0"},
        ],
    )
    parsed_first = parse_manifest(
        {
            "project_id": "p",
            "manifest_version": "1.0.0",
            "components": [first, component("a"), component("z")],
        }
    )
    parsed_second = parse_manifest(
        {
            "project_id": "p",
            "manifest_version": "1.0.0",
            "components": [second, component("a"), component("z")],
        }
    )
    deps_first = parsed_first.components["root"].dependencies
    deps_second = parsed_second.components["root"].dependencies
    assert [(dep.target_id, dep.edge_index) for dep in deps_first] == [
        ("a", 1),
        ("z", 2),
    ]
    assert [(dep.target_id, dep.edge_index) for dep in deps_second] == [
        ("a", 1),
        ("z", 2),
    ]


def test_schema_aliases_and_digest_object_are_supported() -> None:
    payload = content_bytes({"x": 2})
    parsed = parse_manifest(
        {
            "project": "alias-project",
            "version": "2.0.0",
            "artifacts": [
                {
                    "id": "root",
                    "version_expression": "1.0.0",
                    "hash_algo": "sha-256",
                    "digest": {
                        "algorithm": "sha256",
                        "value": hashlib.sha256(payload).hexdigest(),
                    },
                    "signature": "已签名",
                    "content": {"x": 2},
                    "dependencies": {"lib": ">=1.0.0"},
                },
                component("lib"),
            ],
        }
    )
    assert parsed.project_id == "alias-project"
    assert parsed.manifest_version_text == "2.0.0"
    assert parsed.components["root"].hash_algorithm == "sha256"
    assert parsed.components["root"].dependencies[0].target_id == "lib"


def test_missing_component_version_is_rejected() -> None:
    item = component("no-version")
    item.pop("version")
    parsed = parse_manifest(
        {
            "project_id": "p",
            "manifest_version": "1.0.0",
            "components": [item],
        }
    )
    assert not parsed.components
    assert E_VERSION_EXPR in {
        code for rejection in parsed.rejections for code in rejection.codes
    }


def test_manifest_level_hmac_key_is_inherited() -> None:
    payload = content_bytes({"hmac": True})
    import hmac

    digest = hmac.new(b"manifest-key", payload, hashlib.sha256).hexdigest()
    parsed = parse_manifest(
        {
            "project_id": "p",
            "manifest_version": "1.0.0",
            "hmac_key": "manifest-key",
            "components": [
                {
                    "component_id": "a",
                    "version": "1.0.0",
                    "hash_algorithm": "hmac-sha256",
                    "content_hash": digest,
                    "signature_status": "signed",
                    "content": {"hmac": True},
                }
            ],
        }
    )
    assert parsed.components["a"].hmac_key == "manifest-key"

