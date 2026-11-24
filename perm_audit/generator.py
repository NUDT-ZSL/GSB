from __future__ import annotations

import json
import random
import unicodedata
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, canonical_json_bytes
from .engine import AuditEngine
from .parser import parse_bundle

SEED = 20261125

READ = 1 << 0
WRITE = 1 << 1
APPEND = 1 << 2
EXECUTE = 1 << 3
DELETE = 1 << 4
READ_ATTRS = 1 << 5
WRITE_ATTRS = 1 << 6
READ_PERMS = 1 << 7
CHANGE_PERMS = 1 << 8
OWNER = 1 << 9
SYNCHRONIZE = 1 << 10
AUDIT = 1 << 11
FULL = (1 << 12) - 1


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in rows:
        lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _hex(path: str) -> str:
    return path.encode("utf-8").hex()


def build_principals() -> list[dict[str, Any]]:
    return [
        {"trustee_id": "u_alice", "kind": "user", "groups": ["g_eng"]},
        {"trustee_id": "u_bob", "kind": "user", "groups": ["g_sales"]},
        {"trustee_id": "u_carol", "kind": "user", "groups": ["g_child"]},
        {"trustee_id": "u_dave", "kind": "user", "groups": []},
        {"trustee_id": "u_amp", "kind": "user", "groups": ["g_amp"]},
        {"trustee_id": "u_trunc", "kind": "user", "groups": []},
        {"trustee_id": "u_named", "kind": "user", "groups": []},
        {"trustee_id": "u_policy", "kind": "user", "groups": []},
        {"trustee_id": "u_error", "kind": "user", "groups": []},
        {"trustee_id": "g_eng", "kind": "group", "groups": ["g_staff"]},
        {"trustee_id": "g_sales", "kind": "group", "groups": ["g_staff"]},
        {"trustee_id": "g_child", "kind": "group", "groups": ["g_parent"]},
        {"trustee_id": "g_parent", "kind": "group", "groups": []},
        {"trustee_id": "g_amp", "kind": "group", "groups": []},
        {"trustee_id": "g_staff", "kind": "group", "groups": []},
    ]


def build_objects() -> list[dict[str, Any]]:
    cafe_nfc = "/Aliases/caf\u00e9.txt"
    cafe_nfd = "/Aliases/cafe\u0301.txt"
    return [
        {"object_id": "O_ROOT", "path": "/", "object_type": "dir", "parent_id": None, "inherit_from_parent": True},
        {"object_id": "O_PUBLIC", "path": "/Public", "object_type": "dir", "parent_id": "O_ROOT"},
        {"object_id": "O_REPORTS", "path": "/Public/Reports", "object_type": "dir", "parent_id": "O_PUBLIC"},
        {"object_id": "O_Q1", "path": "/Public/Reports/Q1", "object_type": "dir", "parent_id": "O_REPORTS"},
        {"object_id": "O_PROJECT", "path": "/Project", "object_type": "dir", "parent_id": "O_ROOT"},
        {"object_id": "O_DOCS", "path": "/Project/Docs", "object_type": "dir", "parent_id": "O_PROJECT"},
        {"object_id": "O_SPEC", "path": "/Project/Docs/spec.md", "object_type": "file", "parent_id": "O_DOCS"},
        {"object_id": "O_README", "path": "/Project/README.md", "object_type": "file", "parent_id": "O_PROJECT"},
        {"object_id": "O_GROUP", "path": "/Grouped", "object_type": "dir", "parent_id": "O_ROOT"},
        {"object_id": "O_GROUP_CHILD", "path": "/Grouped/child.txt", "object_type": "file", "parent_id": "O_GROUP"},
        {"object_id": "O_SHADOW", "path": "/Shadow.bin", "object_type": "file", "parent_id": "O_ROOT"},
        {"object_id": "O_AMPLIFY", "path": "/Amplify", "object_type": "dir", "parent_id": "O_ROOT"},
        {"object_id": "O_AMPLIFY_CHILD", "path": "/Amplify/child.bin", "object_type": "file", "parent_id": "O_AMPLIFY"},
        {"object_id": "O_TRUNC", "path": "/Trunc.bin", "object_type": "file", "parent_id": "O_ROOT"},
        {"object_id": "O_NAMED", "path": "/Named.bin", "object_type": "file", "parent_id": "O_ROOT"},
        {"object_id": "O_ERROR_ROOT", "path": "/ErrorChain", "object_type": "dir", "parent_id": "O_ROOT"},
        {"object_id": "O_ERROR_MID", "path": "/ErrorChain/Mid", "object_type": "dir", "parent_id": "O_ERROR_ROOT"},
        {"object_id": "O_ERROR_LEAF", "path": "/ErrorChain/Mid/leaf.bin", "object_type": "file", "parent_id": "O_ERROR_MID"},
        {"object_id": "O_POLICY", "path": "/Policy", "object_type": "dir", "parent_id": "O_ROOT"},
        {"object_id": "O_POLICY_ALLOW", "path": "/Policy/Allow", "object_type": "file", "parent_id": "O_POLICY"},
        {"object_id": "O_ALIASES", "path": "/Aliases", "object_type": "dir", "parent_id": "O_ROOT"},
        {"object_id": "O_ALIAS_DOT_A", "path": "/Aliases/dotfile.txt", "object_type": "file", "parent_id": "O_ALIASES"},
        {"object_id": "O_ALIAS_DOT_B", "path": "/Aliases/./dotfile.txt", "object_type": "file", "parent_id": "O_ALIASES"},
        {"object_id": "O_CASE_A", "path": "/Aliases/CaseFile.txt", "object_type": "file", "parent_id": "O_ALIASES"},
        {"object_id": "O_CASE_B", "path": "/Aliases/casefile.txt", "object_type": "file", "parent_id": "O_ALIASES"},
        {"object_id": "O_UNICODE_NFC", "path": cafe_nfc, "object_type": "file", "parent_id": "O_ALIASES",
         "path_bytes_hex": _hex(cafe_nfc)},
        {"object_id": "O_UNICODE_NFD", "path": cafe_nfd, "object_type": "file", "parent_id": "O_ALIASES",
         "path_bytes_hex": _hex(cafe_nfd)},
    ]


def build_aces() -> list[dict[str, Any]]:
    return [
        {
            "ace_id": "A100_ENG_READ",
            "object_id": "O_PROJECT",
            "trustee_id": "g_eng",
            "effect": "allow",
            "mask_style": "named",
            "mask": ["read", "read_attributes", "synchronize"],
            "inheritance_scope": "descendants",
        },
        {
            "ace_id": "A101_BOB_WRITE_DENY",
            "object_id": "O_DOCS",
            "trustee_id": "u_bob",
            "effect": "deny",
            "mask_style": "integer",
            "mask": WRITE,
            "inheritance_scope": "descendants",
        },
        {
            "ace_id": "A102_SALES_READ_OCT",
            "object_id": "O_PROJECT",
            "trustee_id": "g_sales",
            "effect": "allow",
            "mask_style": "octal",
            "mask": "0o0001",
            "inheritance_scope": "descendants",
        },
        {
            "ace_id": "A200_PARENT_FULL",
            "object_id": "O_GROUP",
            "trustee_id": "g_parent",
            "effect": "allow",
            "mask_style": "named",
            "mask": ["full"],
            "inheritance_scope": "descendants",
        },
        {
            "ace_id": "A201_CHILD_TRUNC_OCT",
            "object_id": "O_GROUP",
            "trustee_id": "g_child",
            "effect": "allow",
            "mask_style": "octal",
            "mask": "1",
            "inheritance_scope": "descendants",
        },
        {
            "ace_id": "A300_SHADOW_DENY",
            "object_id": "O_SHADOW",
            "trustee_id": "u_dave",
            "effect": "deny",
            "mask_style": "integer",
            "mask": WRITE | DELETE,
            "inheritance_scope": "self",
        },
        {
            "ace_id": "A301_SHADOW_ALLOW",
            "object_id": "O_SHADOW",
            "trustee_id": "u_dave",
            "effect": "allow",
            "mask_style": "named",
            "mask": ["write", "delete"],
            "inheritance_scope": "self",
        },
        {
            "ace_id": "A400_INHERIT_ONLY_EXEC",
            "object_id": "O_AMPLIFY",
            "trustee_id": "g_amp",
            "effect": "allow",
            "mask_style": "integer",
            "mask": EXECUTE,
            "inheritance_scope": "children",
            "inherit_only": True,
        },
        {
            "ace_id": "A500_TRUNC_OCT_DELETE",
            "object_id": "O_TRUNC",
            "trustee_id": "u_trunc",
            "effect": "allow",
            "mask_style": "octal",
            # Intentionally one octal digit: low three bits are observed, high
            # canonical digit positions are derivation-bounded as truncated.
            "mask": "4",
            "inheritance_scope": "self",
        },
        {
            "ace_id": "A501_NAMED_MISSING_WRITE",
            "object_id": "O_NAMED",
            "trustee_id": "u_named",
            "effect": "allow",
            "mask_style": "named",
            "mask": ["read"],
            "inheritance_scope": "self",
        },
        {
            "ace_id": "A700_ERROR_ROOT_OCT",
            "object_id": "O_ERROR_ROOT",
            "trustee_id": "u_error",
            "effect": "allow",
            "mask_style": "octal",
            "mask": "1",
            "inheritance_scope": "descendants",
        },
        {
            "ace_id": "A701_ERROR_MID_NAMED",
            "object_id": "O_ERROR_MID",
            "trustee_id": "u_error",
            "effect": "allow",
            "mask_style": "named",
            "mask": ["write"],
            "inheritance_scope": "descendants",
        },
        {
            "ace_id": "A702_ERROR_LEAF_NAMED",
            "object_id": "O_ERROR_LEAF",
            "trustee_id": "u_error",
            "effect": "allow",
            "mask_style": "named",
            "mask": ["execute"],
            "inheritance_scope": "self",
        },
        {
            "ace_id": "A600_POLICY_READ",
            "object_id": "O_POLICY_ALLOW",
            "trustee_id": "u_policy",
            "effect": "allow",
            "mask_style": "integer",
            "mask": READ,
            "inheritance_scope": "self",
        },
    ]


def build_requests() -> list[dict[str, Any]]:
    return [
        {"sync_marker": "SYNC-0001", "trustee_id": "u_alice", "path": "/Project/Docs/spec.md", "operation": "read"},
        {"sync_marker": "SYNC-0002", "trustee_id": "u_bob", "path": "/Project/Docs/spec.md", "operation": "read"},
        {"sync_marker": "SYNC-0003", "trustee_id": "u_bob", "path": "/Project/Docs/spec.md", "operation": "write"},
        {"sync_marker": "SYNC-0004", "trustee_id": "u_carol", "path": "/Grouped/child.txt", "operation": "chown"},
        {"sync_marker": "SYNC-0005", "trustee_id": "u_carol", "path": "/Grouped/child.txt", "operation": "read"},
        {"sync_marker": "SYNC-0006", "trustee_id": "u_dave", "path": "/Shadow.bin", "operation": "write"},
        {"sync_marker": "SYNC-0007", "trustee_id": "u_dave", "path": "/Shadow.bin", "operation": "delete"},
        {"sync_marker": "SYNC-0008", "trustee_id": "u_amp", "path": "/Amplify/child.bin", "operation": "execute"},
        {"sync_marker": "SYNC-0009", "trustee_id": "u_amp", "path": "/Amplify", "operation": "execute"},
        {"sync_marker": "SYNC-0010", "trustee_id": "u_trunc", "path": "/Trunc.bin", "operation": "append",
         "sender_original_permission_bits": APPEND | DELETE},
        {"sync_marker": "SYNC-0011", "trustee_id": "u_trunc", "path": "/Trunc.bin", "operation": "delete",
         "sender_original_permission_bits": APPEND | DELETE},
        {"sync_marker": "SYNC-0012", "trustee_id": "u_named", "path": "/Named.bin", "operation": "write",
         "sender_original_permission_bits": READ},
        {"sync_marker": "SYNC-0013", "trustee_id": "u_policy", "path": "/Policy/ALLOW", "operation": "read"},
        {"sync_marker": "SYNC-0014", "trustee_id": "u_alice", "path": "/Project/README.md", "operation": "read"},
        {"sync_marker": "SYNC-0015", "trustee_id": "u_error", "path": "/ErrorChain/Mid/leaf.bin", "operation": "read"},
        {"sync_marker": "SYNC-0016", "trustee_id": "u_error", "path": "/ErrorChain/Mid/leaf.bin", "operation": "write"},
        {"sync_marker": "SYNC-0017", "trustee_id": "u_error", "path": "/ErrorChain/Mid/leaf.bin", "operation": "delete"},
    ]


def generate_sample(out: str | Path, seed: int = SEED) -> dict[str, Any]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    principals = build_principals()
    objects = build_objects()
    aces = build_aces()
    requests = build_requests()

    # Arrival order is deliberately seed-shuffled while IDs/canonical keys stay fixed.
    rng.shuffle(aces)
    rng.shuffle(objects)
    rng.shuffle(requests)

    write_jsonl(out / "principals.jsonl", principals)
    write_jsonl(out / "objects.jsonl", objects)
    write_jsonl(out / "aces.jsonl", aces)
    write_jsonl(out / "requests.jsonl", requests)

    manifest = {
        "sample_id": "ntfs-style-deterministic-v1",
        "seed": seed,
        "generator": "perm_audit.generator.generate_sample",
        "canonical_bits": 12,
        "files": [
            "principals.jsonl",
            "objects.jsonl",
            "aces.jsonl",
            "requests.jsonl",
            "truth.jsonl",
        ],
    }
    atomic_write_json(out / "manifest.json", manifest)

    # Synchronous truth generation. The audit pipeline never reads truth.jsonl.
    bundle = parse_bundle(out)
    engine = AuditEngine(bundle)
    analysis = engine.analyze()
    reports = {item["sync_marker"]: item for item in analysis["request_reports"]}
    truth_rows: list[dict[str, Any]] = []
    for request in sorted(requests, key=lambda item: item["sync_marker"]):
        marker = request["sync_marker"]
        report = reports[marker]
        original_mask = int(
            request.get("sender_original_permission_bits", report["effective_mask"])
        )
        requested_bit = report["requested_weight"]
        truth_rows.append(
            {
                "sync_marker": marker,
                "trustee_id": request["trustee_id"],
                "path": request["path"],
                "original_path_bytes_hex": _hex(request["path"]),
                "operation": request["operation"],
                "requested_bit": report["requested_bit"],
                "requested_weight": requested_bit,
                "sender_original_permission_bits": original_mask,
                "expected_effective_mask": original_mask,
                "expected_ground_truth_decision": "allow" if original_mask & requested_bit else "deny",
                "expected_observed_effective_mask": report["effective_mask"],
                "sync_note": "truth written synchronously by fixed-seed generator",
            }
        )
    write_jsonl(out / "truth.jsonl", truth_rows)
    return {"out": str(out), "manifest": manifest, "truth_count": len(truth_rows)}


def generate_invalid_sample(out: str | Path) -> dict[str, Any]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    principals = [
        {"trustee_id": "P_OK", "kind": "user", "groups": []},
        {"trustee_id": "P_BAD_GROUP", "kind": "user", "groups": ["G_MISSING"]},
    ]
    objects = [
        {"object_id": "O_OK", "path": "/ok", "object_type": "file", "parent_id": None},
        {"object_id": "O_DUP", "path": "/dup", "object_type": "file", "parent_id": None},
        {"object_id": "O_DUP", "path": "/dup2", "object_type": "file", "parent_id": None},
        {"object_id": "O_BAD_PATH", "path": "/bad|name", "object_type": "file", "parent_id": None},
        {"object_id": "O_MISSING_PARENT", "path": "/missing-parent", "object_type": "file", "parent_id": "NOPE"},
        {"object_id": "O_CYCLE_A", "path": "/cycle-a", "object_type": "dir", "parent_id": "O_CYCLE_B"},
        {"object_id": "O_CYCLE_B", "path": "/cycle-b", "object_type": "dir", "parent_id": "O_CYCLE_A"},
        {"object_id": "O_BAD_TYPE", "path": "/bad-type", "object_type": "symlink", "parent_id": None},
    ]
    aces = [
        {"ace_id": "ACE_BAD_EFFECT", "object_id": "O_OK", "trustee_id": "P_OK", "effect": "maybe",
         "mask_style": "integer", "mask": 1, "inheritance_scope": "self"},
        {"ace_id": "ACE_BAD_SCOPE", "object_id": "O_OK", "trustee_id": "P_OK", "effect": "allow",
         "mask_style": "integer", "mask": 1, "inheritance_scope": "galaxy"},
        {"ace_id": "ACE_BAD_MASK", "object_id": "O_OK", "trustee_id": "P_OK", "effect": "allow",
         "mask_style": "octal", "mask": "89", "inheritance_scope": "self"},
        {"ace_id": "ACE_MISSING_TRUSTEE", "object_id": "O_OK", "trustee_id": "P_GHOST", "effect": "allow",
         "mask_style": "integer", "mask": 1, "inheritance_scope": "self"},
        {"ace_id": "ACE_MISSING_OBJECT", "object_id": "O_GHOST", "trustee_id": "P_OK", "effect": "allow",
         "mask_style": "integer", "mask": 1, "inheritance_scope": "self"},
    ]
    requests = [
        {"sync_marker": "BAD-OP", "trustee_id": "P_OK", "path": "/ok", "operation": "teleport"},
        {"sync_marker": "BAD-TRUSTEE", "trustee_id": "P_GHOST", "path": "/ok", "operation": "read"},
    ]
    write_jsonl(out / "principals.jsonl", principals)
    write_jsonl(out / "objects.jsonl", objects)
    write_jsonl(out / "aces.jsonl", aces)
    write_jsonl(out / "requests.jsonl", requests)
    (out / "objects.jsonl").write_text(
        (out / "objects.jsonl").read_text(encoding="utf-8") + '{"object_id":"BROKEN_JSON",\n',
        encoding="utf-8",
    )
    expected = {
        "expected_codes": [
            "E_GROUP_REF",
            "E_ID_DUPLICATE",
            "E_PATH_SYNTAX",
            "E_PARENT_MISSING",
            "E_PARENT_CYCLE",
            "E_OBJECT_TYPE",
            "E_ACE_TYPE",
            "E_INHERIT_SCOPE",
            "E_MASK_SYNTAX",
            "E_TRUSTEE_REF",
            "E_OBJECT_REF",
            "E_PERMISSION_NAME",
            "E_JSON_SYNTAX",
        ]
    }
    atomic_write_json(out / "expected_rejections.json", expected)
    return {"out": str(out), **expected}
