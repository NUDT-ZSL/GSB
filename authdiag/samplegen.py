"""Deterministic local sample generator; no network or account access."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .util import atomic_write_json


def _entry(
    entry_id: str,
    kind: str,
    version: str,
    **values: Any,
) -> dict[str, Any]:
    return {"id": entry_id, "kind": kind, "version": version, **values}


def main_policy() -> dict[str, Any]:
    entries = [
        _entry("session:base", "session_policy", "1.0.0", ttl_seconds=3600),
        _entry("session:admin", "session_policy", "1.0.0", ttl_seconds=1800),
        _entry("role:member", "role", "1.0.0", parents=[]),
        _entry("role:editor", "role", "1.0.0", parents=["role:member"]),
        _entry("role:admin", "role", "1.0.0", parents=["role:editor"]),
        _entry(
            "token:standard",
            "token_rule",
            "1.0.0",
            refs=["role:member", "session:base"],
            template="standard-v1",
        ),
        _entry(
            "token:admin",
            "token_rule",
            "1.0.0",
            refs=["role:admin", "session:admin"],
            template="admin-v1",
        ),
        _entry(
            "binding:alice",
            "role_binding",
            "1.0.0",
            subject="alice",
            role="role:admin",
            token_rule="token:admin",
            session_policy="session:admin",
        ),
        _entry(
            "binding:bob",
            "role_binding",
            "1.0.0",
            subject="bob",
            role="role:member",
            token_rule="token:standard",
            session_policy="session:base",
        ),
        _entry(
            "binding:carol",
            "role_binding",
            "1.0.0",
            subject="carol",
            role="role:editor",
            token_rule="token:standard",
            session_policy="session:base",
        ),
    ]
    updates = [
        {
            "update_id": "upd-003",
            "timestamp": "2026-01-02T00:00:00Z",
            "version": "1.2.1",
            "op": "put",
            "entry": _entry(
                "session:admin",
                "session_policy",
                "1.2.1",
                ttl_seconds=900,
                description="shorter administrator session",
            ),
        },
        {
            "update_id": "upd-001",
            "timestamp": "2026-01-01T00:00:00Z",
            "version": "1.1.0",
            "op": "put",
            "entry": _entry(
                "token:standard",
                "token_rule",
                "1.1.0",
                refs=["role:member", "session:base"],
                template="standard-v2",
            ),
        },
        {
            "update_id": "upd-002",
            "timestamp": "2026-01-02T00:00:00Z",
            "version": "1.2.0",
            "op": "put",
            "entry": _entry(
                "role:member",
                "role",
                "1.2.0",
                parents=[],
                description="membership policy revision",
            ),
        },
    ]
    sync_markers = [
        {
            "marker_id": "sync-003",
            "update_id": "upd-003",
            "before_version": "1.2.0",
            "after_version": "1.2.1",
            "changed_entry_ids": ["session:admin"],
            "expected_tokens": ["tok-alice1", "tok-alice2"],
            "expected_sessions": ["sess-alice1"],
        },
        {
            "marker_id": "sync-001",
            "update_id": "upd-001",
            "before_version": "1.0.0",
            "after_version": "1.1.0",
            "changed_entry_ids": ["token:standard"],
            "expected_tokens": ["tok-dave1"],
            "expected_sessions": ["sess-carol1", "sess-dave1"],
        },
        {
            "marker_id": "sync-002",
            "update_id": "upd-002",
            "before_version": "1.1.0",
            "after_version": "1.2.0",
            "changed_entry_ids": ["role:member"],
            "expected_tokens": ["tok-alice1", "tok-alice2", "tok-dave1"],
            "expected_sessions": ["sess-alice1", "sess-carol1", "sess-dave1"],
        },
    ]
    return {
        "schema_version": "1.0",
        "policy_version": "1.0.0",
        "entries": entries,
        "updates": updates,
        "sync_markers": sync_markers,
    }


def main_events_a() -> list[dict[str, Any]]:
    return [
        {
            "event_id": "evt-001",
            "type": "login",
            "timestamp": "2026-02-01T00:01:00Z",
            "session_id": "sess-alice1",
            "token_id": "tok-alice1",
            "binding_id": "binding:alice",
        },
        {
            "event_id": "evt-002",
            "type": "login",
            "timestamp": "2026-02-01T00:02:00Z",
            "session_id": "sess-bob1",
            "token_id": "tok-bob1",
            "binding_id": "binding:bob",
        },
        {
            "event_id": "evt-003",
            "type": "login",
            "timestamp": "2026-02-01T00:03:00Z",
            "session_id": "sess-carol1",
            "token_id": "tok-carol1",
            "binding_id": "binding:carol",
        },
    ]


def main_events_b() -> list[dict[str, Any]]:
    return [
        {
            "event_id": "evt-004",
            "type": "renew",
            "timestamp": "2026-02-01T00:04:00Z",
            "session_id": "sess-alice1",
            "token_id": "tok-alice2",
            "binding_id": "binding:alice",
        },
        {
            "event_id": "evt-005",
            "type": "logout",
            "timestamp": "2026-02-01T00:05:00Z",
            "session_id": "sess-bob1",
        },
        {
            "event_id": "evt-006",
            "type": "token_revoke",
            "timestamp": "2026-02-01T00:06:00Z",
            "session_id": "sess-carol1",
            "token_id": "tok-carol1",
        },
        {
            "event_id": "evt-007",
            "type": "login",
            "timestamp": "2026-02-01T00:07:00Z",
            "session_id": "sess-dave1",
            "token_id": "tok-dave1",
            "binding_id": "binding:bob",
        },
    ]


def cycle_policy() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "policy_version": "2.0.0",
        "entries": [
            _entry("role:a", "role", "1.0.0", parents=["role:b"]),
            _entry("role:b", "role", "1.0.0", parents=["role:a"]),
            _entry("session:s", "session_policy", "1.0.0", ttl_seconds=60),
            _entry(
                "token:t",
                "token_rule",
                "1.0.0",
                refs=["role:a", "session:s"],
                template="cycle",
            ),
            _entry(
                "binding:x",
                "role_binding",
                "1.0.0",
                subject="x",
                role="role:a",
                token_rule="token:t",
                session_policy="session:s",
            ),
        ],
        "updates": [
            {
                "update_id": "upd-cycle",
                "timestamp": "2026-03-01T00:00:00Z",
                "version": "2.0.1",
                "op": "put",
                "entry": _entry(
                    "role:a",
                    "role",
                    "2.0.1",
                    parents=["role:b"],
                    description="cycle stays visible",
                ),
            }
        ],
        "sync_markers": [],
    }


def cycle_events() -> list[dict[str, Any]]:
    return [
        {
            "event_id": "evt-cycle-login",
            "type": "login",
            "timestamp": "2026-03-02T00:00:00Z",
            "session_id": "sess-cycle",
            "token_id": "tok-cycle",
            "binding_id": "binding:x",
        }
    ]


def invalid_policy() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "policy_version": "3.0.0",
        "entries": [
            _entry("role:base", "role", "1.0.0", parents=[]),
            _entry("role:badver", "role", "v1", parents=[]),
            _entry("role:orphan", "role", "1.0.0", parents=["role:missing"]),
            _entry("token:broken", "token_rule", "1.0.0", refs=["role:missing"]),
            _entry("session:base", "session_policy", "1.0.0", ttl_seconds=60),
            _entry(
                "token:good",
                "token_rule",
                "1.0.0",
                refs=["role:base", "session:base"],
                template="good",
            ),
            _entry(
                "binding:good",
                "role_binding",
                "1.0.0",
                subject="good",
                role="role:base",
                token_rule="token:good",
                session_policy="session:base",
            ),
            _entry("session:temp", "session_policy", "1.0.0", ttl_seconds=1),
            _entry("session:temp", "session_policy", "1.0.0", ttl_seconds=2),
            "not-an-object",
        ],
        "updates": [],
        "sync_markers": [],
    }


def invalid_events() -> list[dict[str, Any]]:
    return [
        {
            "event_id": "evt-good",
            "type": "login",
            "timestamp": "2026-04-01T00:00:00Z",
            "session_id": "sess-good",
            "token_id": "tok-good",
            "binding_id": "binding:good",
        },
        {
            "event_id": "evt-bad-session",
            "type": "login",
            "timestamp": "2026-04-01T00:01:00Z",
            "session_id": "alice",
            "token_id": "tok-bad-session",
            "binding_id": "binding:good",
        },
        {
            "event_id": "evt-bad-token",
            "type": "login",
            "timestamp": "2026-04-01T00:02:00Z",
            "session_id": "sess-bad-token",
            "token_id": "bad",
            "binding_id": "binding:good",
        },
        {
            "event_id": "evt-bad-type",
            "type": "mystery",
            "timestamp": "2026-04-01T00:03:00Z",
            "session_id": "sess-bad-type",
        },
        {
            "event_id": "evt-dup",
            "type": "logout",
            "timestamp": "2026-04-01T00:04:00Z",
            "session_id": "sess-good",
        },
        {
            "event_id": "evt-dup",
            "type": "logout",
            "timestamp": "2026-04-01T00:05:00Z",
            "session_id": "sess-good",
        },
    ]


def generate_samples(output_dir: Path | str) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    documents: dict[str, Any] = {
        "policy.json": main_policy(),
        "events_a.json": main_events_a(),
        "events_b.json": main_events_b(),
        "cycle_policy.json": cycle_policy(),
        "cycle_events.json": cycle_events(),
        "invalid_policy.json": invalid_policy(),
        "invalid_events.json": invalid_events(),
    }
    paths: dict[str, str] = {}
    for name in sorted(documents):
        path = output_dir / name
        atomic_write_json(path, documents[name])
        paths[name] = str(path)

    malformed = output_dir / "events_malformed.json"
    malformed.write_text('[{"event_id": "evt-malformed",\n', encoding="utf-8")
    paths[malformed.name] = str(malformed)

    manifest = {
        "generator": "authdiag.samplegen",
        "deterministic": True,
        "runtime_network": "none",
        "scenarios": {
            "main": {
                "policy": "policy.json",
                "events": ["events_a.json", "events_b.json"],
                "expected_exit_code": 0,
                "expected_affected_tokens": [
                    "tok-alice1",
                    "tok-alice2",
                    "tok-dave1",
                ],
                "expected_affected_sessions": [
                    "sess-alice1",
                    "sess-carol1",
                    "sess-dave1",
                ],
            },
            "cycle": {
                "policy": "cycle_policy.json",
                "events": ["cycle_events.json"],
                "expected_exit_code": 1,
                "expected_error_code": "E_CYCLE",
                "expected_cycle_indices": [0, 1],
            },
            "invalid": {
                "policy": "invalid_policy.json",
                "events": ["invalid_events.json", "events_malformed.json"],
                "expected_exit_code": 1,
                "expected_error_codes": [
                    "E_VERSION",
                    "E_ROLE_REF",
                    "E_TOKEN_REF",
                    "E_DUP",
                    "E_SCHEMA",
                    "E_EVENT_TYPE",
                    "E_EVENT_DUP",
                    "E_SESSION_FORMAT",
                    "E_TOKEN_FORMAT",
                    "E_JSON",
                ],
            },
        },
        "truth_policy": (
            "sync marker expected sets are explicit observations; "
            "comparison uses markers plus the dependency graph"
        ),
    }
    manifest_path = output_dir / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    paths[manifest_path.name] = str(manifest_path)
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="authdiag-samplegen",
        description="Generate deterministic offline sample policies and event streams",
    )
    parser.add_argument(
        "--out",
        default="samples",
        help="sample output directory (default: samples)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    paths = generate_samples(Path(args.out))
    print(json.dumps({"status": "ok", "files": paths}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
