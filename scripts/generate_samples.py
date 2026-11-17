from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def event(
    event_id: str,
    trace_id: str,
    actor: str,
    action: str,
    object_name: str,
    ts: int,
    seq: int,
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "trace_id": trace_id,
        "actor": actor,
        "action": action,
        "object": object_name,
        "ts": ts,
        "seq": seq,
        "parent_event_id": parent_event_id,
    }


def clean_events() -> list[dict[str, Any]]:
    return [
        event("evt-a1", "trace-a", "alice", "login", "workstation-1", 1000, 1),
        event(
            "evt-a2",
            "trace-a",
            "alice",
            "read",
            "case-doc-1",
            1200,
            2,
            "evt-a1",
        ),
        event(
            "evt-a3",
            "trace-a",
            "alice",
            "export",
            "case-doc-1",
            1800,
            3,
            "evt-a2",
        ),
        event("evt-b1", "trace-b", "bob", "inspect", "case-doc-2", 2000, 1),
        event(
            "evt-b2",
            "trace-b",
            "bob",
            "inspect",
            "case-doc-2",
            2600,
            2,
            "evt-b1",
        ),
        event(
            "evt-b3",
            "trace-b",
            "alice",
            "copy",
            "case-doc-1",
            3000,
            3,
            "evt-b2",
        ),
        event("evt-c1", "trace-c", "carol", "login", "server-7", 9000, 1),
        event(
            "evt-c2",
            "trace-c",
            "carol",
            "read",
            "server-7",
            9400,
            2,
            "evt-c1",
        ),
        event(
            "evt-c3",
            "trace-c",
            "carol",
            "logout",
            "server-7",
            9800,
            3,
            "evt-c2",
        ),
    ]


def deterministic_shuffle(
    events: list[dict[str, Any]], salt: str
) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda item: hashlib.sha256(
            f"{salt}:{item['event_id']}".encode("utf-8")
        ).hexdigest(),
    )


def duplicate_events() -> list[dict[str, Any]]:
    shuffled = deterministic_shuffle(clean_events(), "duplicates-v1")
    result = list(shuffled)
    result.insert(2, dict(shuffled[5]))
    result.insert(6, dict(shuffled[1]))
    return result


def anomaly_events() -> list[dict[str, Any]]:
    events = deterministic_shuffle(clean_events(), "anomalies-v1")
    conflict = dict(next(item for item in events if item["event_id"] == "evt-a2"))
    conflict["actor"] = "mallory"
    events.extend(
        [
            event(
                "evt-x2",
                "trace-x",
                "dave",
                "read",
                "missing-parent-object",
                5000,
                2,
                "evt-does-not-exist",
            ),
            event(
                "evt-x3",
                "trace-x",
                "dave",
                "write",
                "self-cycle-object",
                5100,
                3,
                "evt-x3",
            ),
            event(
                "evt-y1",
                "trace-y",
                "erin",
                "open",
                "cycle-object",
                6000,
                1,
                "evt-y2",
            ),
            event(
                "evt-y2",
                "trace-y",
                "erin",
                "close",
                "cycle-object",
                6100,
                2,
                "evt-y1",
            ),
            event(
                "evt-z1",
                "trace-z",
                "frank",
                "read",
                "cross-trace-object",
                7000,
                1,
                "evt-a1",
            ),
            event(
                "evt-z2",
                "trace-z",
                "frank",
                "write",
                "duplicate-seq-object",
                7100,
                1,
            ),
            event(
                "evt-g5",
                "trace-g",
                "grace",
                "read",
                "gap-object",
                8000,
                5,
            ),
            dict(next(item for item in events if item["event_id"] == "evt-a3")),
            conflict,
        ]
    )
    return events


def write_jsonl(path: Path, rows: list[dict[str, Any] | str]) -> None:
    lines = []
    for row in rows:
        if isinstance(row, str):
            lines.append(row)
        else:
            lines.append(
                json.dumps(row, ensure_ascii=False, sort_keys=True)
            )
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def generate(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    clean = clean_events()
    shuffled = deterministic_shuffle(clean, "clean-v1")
    duplicates = duplicate_events()
    anomalies = anomaly_events()
    malformed = [
        '{"event_id":"broken-json","trace_id":"trace-bad"',
        '{"event_id":"missing-field","trace_id":"trace-bad"}',
    ]

    write_jsonl(output_dir / "events_ordered.jsonl", clean)
    write_jsonl(output_dir / "events_shuffled.jsonl", shuffled)
    write_jsonl(output_dir / "events_duplicates.jsonl", duplicates)
    write_jsonl(output_dir / "events_anomalies.jsonl", anomalies + malformed)
    write_jsonl(output_dir / "events_empty.jsonl", [])

    default_rules = {
        "version": "default-v1",
        "rules": [
            {
                "id": "same-actor-object",
                "match_fields": ["actor", "object"],
                "window_ms": 5000,
            }
        ],
    }
    short_rules = {
        "version": "short-window-v1",
        "rules": [
            {
                "id": "same-actor-object",
                "match_fields": ["actor", "object"],
                "window_ms": 150,
            }
        ],
    }
    write_json(output_dir / "rules-default.json", default_rules)
    write_json(output_dir / "rules-short-window.json", short_rules)

    manifest_files = sorted(
        path.name
        for path in output_dir.iterdir()
        if path.is_file() and path.name != "manifest.json"
    )
    manifest = {
        "generator": "audit-forge deterministic samples",
        "shuffle_salt": "clean-v1",
        "files": {
            name: hashlib.sha256(
                (output_dir / name).read_bytes()
            ).hexdigest()
            for name in manifest_files
        },
        "counts": {
            "ordered": len(clean),
            "shuffled": len(shuffled),
            "duplicates": len(duplicates),
            "anomalies": len(anomalies),
            "malformed": len(malformed),
        },
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default="samples",
        help="directory for deterministic sample files",
    )
    args = parser.parse_args()
    manifest = generate(Path(args.output_dir))
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
