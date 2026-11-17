from __future__ import annotations

import json
from pathlib import Path

from authdiag.engine import EventDocument, Workbench


def _documents(sample_dir: Path, event_names: list[str]):
    return [
        EventDocument(
            str(sample_dir / name),
            (sample_dir / name).read_text(encoding="utf-8"),
        )
        for name in event_names
    ]


def _run(
    policy: dict,
    sample_dir: Path,
    workdir: Path,
    event_names: list[str],
    documents=None,
):
    return Workbench(
        policy,
        source=str(sample_dir / "policy.json"),
        workdir=workdir,
        output_dir=workdir,
    ).run(documents or _documents(sample_dir, event_names))


def _fingerprint(result):
    return {
        "tokens": sorted(item["token_id"] for item in result.report["affected_tokens"]),
        "sessions": sorted(
            item["session_id"] for item in result.report["affected_sessions"]
        ),
        "chains": [
            (
                chain["chain_id"],
                tuple((node["type"], node["id"]) for node in chain["nodes"]),
                tuple(chain["edge_kinds"]),
            )
            for chain in result.report["propagation_chains"]
        ],
        "updates": [
            (
                item["update_id"],
                tuple(item["affected_tokens"]),
                tuple(item["affected_sessions"]),
            )
            for item in result.report["updates"]
        ],
    }


def test_shuffled_updates_and_event_files_have_identical_invalidation(
    sample_dir: Path,
    tmp_path: Path,
):
    original_policy = json.loads(
        (sample_dir / "policy.json").read_text(encoding="utf-8")
    )
    shuffled_policy = dict(original_policy)
    shuffled_policy["updates"] = list(reversed(original_policy["updates"]))
    shuffled_policy["sync_markers"] = list(reversed(original_policy["sync_markers"]))

    first = _run(
        original_policy,
        sample_dir,
        tmp_path / "original",
        ["events_a.json", "events_b.json"],
    )
    second = _run(
        shuffled_policy,
        sample_dir,
        tmp_path / "shuffled",
        ["events_b.json", "events_a.json"],
    )
    assert _fingerprint(first) == _fingerprint(second)
    assert first.exit_code == second.exit_code == 0
    assert len(first.report["updates"]) == 3


def test_same_or_missing_timestamps_use_version_and_stable_key(
    sample_dir: Path,
    tmp_path: Path,
):
    policy = {
        "schema_version": "1.0",
        "policy_version": "1.0.0",
        "entries": [
            {"id": "role:base", "kind": "role", "version": "1.0.0"},
            {
                "id": "role:child",
                "kind": "role",
                "version": "1.0.0",
                "parents": ["role:base"],
            },
        ],
        "updates": [
            {
                "update_id": "upd-b",
                "timestamp": "2026-01-01T00:00:00Z",
                "version": "1.2.0",
                "op": "put",
                "entry": {
                    "id": "role:base",
                    "kind": "role",
                    "version": "1.2.0",
                },
            },
            {
                "update_id": "upd-a",
                "timestamp": "2026-01-01T00:00:00Z",
                "version": "1.1.0",
                "op": "put",
                "entry": {
                    "id": "role:base",
                    "kind": "role",
                    "version": "1.1.0",
                },
            },
            {
                "update_id": "upd-missing",
                "version": "1.3.0",
                "op": "put",
                "entry": {
                    "id": "role:base",
                    "kind": "role",
                    "version": "1.3.0",
                },
            },
        ],
        "sync_markers": [],
    }
    result = Workbench(
        policy,
        source="inline-policy",
        workdir=tmp_path / "timestamps",
        output_dir=tmp_path / "timestamps",
    ).run(
        [
            EventDocument(
                "events.json",
                json.dumps(
                    [
                        {
                            "event_id": "evt-z",
                            "type": "logout",
                            "timestamp": "2026-01-02T00:00:00Z",
                            "session_id": "sess-z",
                        },
                        {
                            "event_id": "evt-a",
                            "type": "logout",
                            "session_id": "sess-a",
                        },
                    ]
                ),
            )
        ]
    )
    assert [item["update_id"] for item in result.report["updates"]] == [
        "upd-a",
        "upd-b",
        "upd-missing",
    ]
    assert result.report["checkpoint"]["watermark"] is not None
