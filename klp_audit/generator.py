"""Deterministic local KLP sample and ground-truth generator."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    DIRECTORY_ENTRY_SIZE,
    FILE_HEADER_SIZE,
    TRUTH_SCHEMA_VERSION,
)
from .parser import build_file, build_payload, encode_algorithm, encode_stage


@dataclass(frozen=True)
class SampleEvent:
    session_id: str
    stage: str
    algorithm: str
    seq: int
    iterations: int
    salt: bytes
    output_length: int
    level: int
    sync_marker: int
    stage_id_override: int | None = None
    algorithm_id_override: int | None = None
    payload_extra: bytes = b""
    corrupt_crc: bool = False
    expected_code: str | None = None


def _salt(session_id: str, seq: int, length: int = 16) -> bytes:
    return hashlib.sha256(f"{session_id}:{seq}".encode("utf-8")).digest()[:length]


def _derive_specs(
    session_id: str,
    *,
    levels: int = 3,
    sync_base: int,
) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = []
    for level in range(1, levels + 1):
        specs.append(
            {
                "level": level,
                "iterations": 100_000 + (level - 1) * 10_000,
                "output_length": 128 - (level - 1) * 16,
                "salt": _salt(session_id, level + 1, 16),
                "sync_marker": sync_base + 100 + level,
            }
        )
    return specs


def _lifecycle(
    session_id: str,
    *,
    sync_base: int,
    derives: list[dict[str, object]],
    order: tuple[str, ...] = ("negotiate", "derives", "rotate", "seal", "destroy"),
) -> list[SampleEvent]:
    events: list[SampleEvent] = []
    seq = 1
    sync = sync_base
    for marker in order:
        if marker == "derives":
            for derive in derives:
                events.append(
                    SampleEvent(
                        session_id=session_id,
                        stage="derive",
                        algorithm="PBKDF2-SHA256",
                        seq=seq,
                        iterations=int(derive["iterations"]),
                        salt=bytes(derive["salt"]),
                        output_length=int(derive["output_length"]),
                        level=int(derive["level"]),
                        sync_marker=int(derive["sync_marker"]),
                    )
                )
                seq += 1
            continue
        algorithm = {
            "negotiate": "HKDF-SHA256",
            "rotate": "HKDF-SHA256",
            "seal": "HMAC-SHA256",
            "destroy": "HMAC-SHA256",
        }[marker]
        events.append(
            SampleEvent(
                session_id=session_id,
                stage=marker,
                algorithm=algorithm,
                seq=seq,
                iterations=90_000,
                salt=_salt(session_id, seq, 8),
                output_length=32,
                level=1,
                sync_marker=sync,
            )
        )
        seq += 1
        sync += 1
    return events


def _payload_for(event: SampleEvent, record_index: int) -> bytes:
    stage_id = (
        event.stage_id_override
        if event.stage_id_override is not None
        else encode_stage(event.stage)
    )
    algorithm_id = (
        event.algorithm_id_override
        if event.algorithm_id_override is not None
        else encode_algorithm(event.algorithm)
    )
    payload = build_payload(
        record_index=record_index,
        session_id=event.session_id,
        stage_id=stage_id,
        algorithm_id=algorithm_id,
        seq=event.seq,
        iterations=event.iterations,
        salt=event.salt,
        output_length=event.output_length,
        level=event.level,
        sync_marker=event.sync_marker,
    )
    return payload + event.payload_extra


def _encode_sample(
    events: list[SampleEvent],
    *,
    max_iterations: int = 1_000_000,
    shuffle_seed: int = 0,
) -> tuple[bytes, list[tuple[int, SampleEvent]]]:
    shuffled = sorted(
        events,
        key=lambda event: (
            hashlib.sha256(
                (
                    f"{shuffle_seed}|{event.session_id}|{event.seq}|"
                    f"{event.sync_marker}|{event.stage}"
                ).encode("utf-8")
            ).digest(),
            event.session_id,
            event.seq,
            event.sync_marker,
        ),
    )
    payloads: list[bytes] = []
    records: list[tuple[int, SampleEvent]] = []
    for record_index, event in enumerate(shuffled):
        payloads.append(_payload_for(event, record_index))
        records.append((record_index, event))

    encoded = bytearray(build_file(payloads, max_iterations=max_iterations))
    header_size = FILE_HEADER_SIZE + DIRECTORY_ENTRY_SIZE * len(payloads)
    payload_offsets: list[int] = []
    offset = header_size
    for payload in payloads:
        payload_offsets.append(offset)
        offset += len(payload)

    for record_index, event in records:
        if event.corrupt_crc:
            position = payload_offsets[record_index] + len(payloads[record_index]) - 1
            encoded[position] ^= 0x01
    return bytes(encoded), records


def _truth_document(
    *,
    sample_name: str,
    sha256: str,
    max_iterations: int,
    records: list[tuple[int, SampleEvent]],
    expected: dict[str, object],
) -> dict[str, object]:
    sessions: dict[str, list[dict[str, object]]] = {}
    for record_index, event in sorted(
        records, key=lambda item: (item[1].session_id, item[1].seq, item[0])
    ):
        sessions.setdefault(event.session_id, []).append(
            {
                "record_index": record_index,
                "sync_marker": event.sync_marker,
                "stage": event.stage,
                "algorithm": event.algorithm,
                "stage_seq": event.seq,
                "params": {
                    "iterations": event.iterations,
                    "salt_length": len(event.salt),
                    "output_length": event.output_length,
                    "level": event.level,
                },
                "salt_hex": event.salt.hex(),
                "accepted": event.expected_code is None,
                "expected_code": event.expected_code,
            }
        )
    ordered_sessions = {
        session_id: sessions[session_id] for session_id in sorted(sessions)
    }
    return {
        "schema_version": TRUTH_SCHEMA_VERSION,
        "sample": sample_name,
        "sha256": sha256,
        "max_iterations": max_iterations,
        "record_count": len(records),
        "sessions": ordered_sessions,
        "expected": expected,
    }


def _expected(
    conclusion: str,
    codes: list[str],
    *,
    session_statuses: dict[str, str] | None = None,
    config_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "file_conclusion": conclusion,
        "error_codes": sorted(set(codes)),
        "session_statuses": dict(sorted((session_statuses or {}).items())),
        "config_overrides": dict(sorted((config_overrides or {}).items())),
    }


def _legal_events() -> list[SampleEvent]:
    events: list[SampleEvent] = []
    for index, session_id in enumerate(("alpha", "beta"), start=1):
        events.extend(
            _lifecycle(
                session_id,
                sync_base=1_000 * index,
                derives=_derive_specs(session_id, levels=3, sync_base=1_000 * index),
            )
        )
    return events


def _idempotent_events() -> list[SampleEvent]:
    session_id = "idempotent"
    events = _lifecycle(
        session_id,
        sync_base=9_000,
        derives=_derive_specs(session_id, levels=2, sync_base=9_000),
    )
    duplicate = next(event for event in events if event.stage == "derive")
    return events + [dataclasses.replace(duplicate)]


def _state_error_events() -> list[SampleEvent]:
    events: list[SampleEvent] = []
    events.extend(
        _lifecycle(
            "bad-transition",
            sync_base=20_000,
            derives=_derive_specs("bad-transition", levels=2, sync_base=20_000),
            order=("negotiate", "rotate", "derives", "seal", "destroy"),
        )
    )
    events.extend(
        _lifecycle(
            "incomplete",
            sync_base=21_000,
            derives=_derive_specs("incomplete", levels=2, sync_base=21_000),
            order=("negotiate", "derives", "rotate"),
        )
    )
    conflict = _lifecycle(
        "seq-conflict",
        sync_base=22_000,
        derives=_derive_specs("seq-conflict", levels=2, sync_base=22_000),
    )
    first_derive = next(event for event in conflict if event.stage == "derive")
    events.extend(conflict)
    events.append(
        dataclasses.replace(
            first_derive,
            salt=b"conflicting-salt",
            sync_marker=first_derive.sync_marker + 99_000,
        )
    )
    return events


def _chain_error_events() -> list[SampleEvent]:
    salt_events: list[SampleEvent] = []
    reused = b"0123456789abcdef"
    derives = [
        {
            "level": 1,
            "iterations": 100_000,
            "output_length": 96,
            "salt": reused,
            "sync_marker": 30_101,
        },
        {
            "level": 2,
            "iterations": 120_000,
            "output_length": 64,
            "salt": reused,
            "sync_marker": 30_102,
        },
    ]
    salt_events.extend(_lifecycle("salt-reuse", sync_base=30_000, derives=derives))

    length_events = _lifecycle(
        "length-up",
        sync_base=31_000,
        derives=[
            {
                "level": 1,
                "iterations": 100_000,
                "output_length": 32,
                "salt": _salt("length-up", 2),
                "sync_marker": 31_101,
            },
            {
                "level": 2,
                "iterations": 110_000,
                "output_length": 64,
                "salt": _salt("length-up", 3),
                "sync_marker": 31_102,
            },
        ],
    )
    iter_events = _lifecycle(
        "iter-down",
        sync_base=32_000,
        derives=[
            {
                "level": 1,
                "iterations": 200_000,
                "output_length": 96,
                "salt": _salt("iter-down", 2),
                "sync_marker": 32_101,
            },
            {
                "level": 2,
                "iterations": 100_000,
                "output_length": 64,
                "salt": _salt("iter-down", 3),
                "sync_marker": 32_102,
            },
        ],
    )
    return salt_events + length_events + iter_events


def _parse_error_events() -> list[SampleEvent]:
    survivor = _lifecycle(
        "survivor",
        sync_base=40_000,
        derives=_derive_specs("survivor", levels=2, sync_base=40_000),
    )
    malformed = [
        SampleEvent(
            session_id="bad-stage",
            stage="negotiate",
            algorithm="HKDF-SHA256",
            seq=1,
            iterations=90_000,
            salt=_salt("bad-stage", 1),
            output_length=32,
            level=1,
            sync_marker=41_000,
            stage_id_override=250,
            expected_code="E_STAGE",
        ),
        SampleEvent(
            session_id="bad-alg",
            stage="negotiate",
            algorithm="HKDF-SHA256",
            seq=1,
            iterations=90_000,
            salt=_salt("bad-alg", 1),
            output_length=32,
            level=1,
            sync_marker=41_001,
            algorithm_id_override=250,
            expected_code="E_ALGORITHM",
        ),
        SampleEvent(
            session_id="iter-over",
            stage="derive",
            algorithm="PBKDF2-SHA256",
            seq=1,
            iterations=1_000_001,
            salt=_salt("iter-over", 1),
            output_length=32,
            level=1,
            sync_marker=41_002,
            expected_code="E_PARAM",
        ),
        SampleEvent(
            session_id="zero-output",
            stage="derive",
            algorithm="PBKDF2-SHA256",
            seq=1,
            iterations=100_000,
            salt=_salt("zero-output", 1),
            output_length=0,
            level=1,
            sync_marker=41_003,
            expected_code="E_PARAM",
        ),
        SampleEvent(
            session_id="bad-crc",
            stage="negotiate",
            algorithm="HKDF-SHA256",
            seq=1,
            iterations=90_000,
            salt=_salt("bad-crc", 1),
            output_length=32,
            level=1,
            sync_marker=41_004,
            corrupt_crc=True,
            expected_code="E_CRC",
        ),
        SampleEvent(
            session_id="short-payload",
            stage="negotiate",
            algorithm="HKDF-SHA256",
            seq=1,
            iterations=90_000,
            salt=_salt("short-payload", 1),
            output_length=32,
            level=1,
            sync_marker=41_005,
            payload_extra=b"X",
            expected_code="E_FORMAT",
        ),
    ]
    return survivor + malformed


def _scale_events(session_count: int, levels: int, prefix: str) -> list[SampleEvent]:
    events: list[SampleEvent] = []
    for index in range(session_count):
        session_id = f"{prefix}-{index:02d}"
        sync_base = 50_000 + index * 1_000
        events.extend(
            _lifecycle(
                session_id,
                sync_base=sync_base,
                derives=_derive_specs(session_id, levels=levels, sync_base=sync_base),
            )
        )
    return events


def _budget_events() -> list[SampleEvent]:
    events: list[SampleEvent] = []
    events.extend(
        _lifecycle(
            "budget-small",
            sync_base=70_000,
            derives=_derive_specs("budget-small", levels=2, sync_base=70_000),
        )
    )
    events.extend(
        _lifecycle(
            "budget-large",
            sync_base=71_000,
            derives=_derive_specs("budget-large", levels=7, sync_base=71_000),
        )
    )
    return events


def _checkpoint_events() -> list[SampleEvent]:
    return _scale_events(6, 3, "ck")


def _lock_events() -> list[SampleEvent]:
    return _scale_events(2, 2, "lock")


def generate_samples(output_dir: str | Path) -> Path:
    root = Path(output_dir)
    samples_dir = root / "samples"
    truth_dir = root / "truth"
    reports_dir = root / "reports"
    checkpoints_dir = root / "checkpoints"
    for directory in (samples_dir, truth_dir, reports_dir, checkpoints_dir):
        directory.mkdir(parents=True, exist_ok=True)

    sample_definitions: list[
        tuple[
            str,
            list[SampleEvent],
            int,
            int,
            dict[str, object],
        ]
    ] = [
        ("legal", _legal_events(), 1_000_000, 17, _expected("PASS", [])),
        (
            "idempotent",
            _idempotent_events(),
            1_000_000,
            3,
            _expected("PASS", [], session_statuses={"idempotent": "pass"}),
        ),
        (
            "state_errors",
            _state_error_events(),
            1_000_000,
            11,
            _expected(
                "FAIL",
                ["E_TRANSITION", "E_INCOMPLETE", "E_SEQ_CONFLICT"],
                session_statuses={
                    "bad-transition": "fail",
                    "incomplete": "fail",
                    "seq-conflict": "fail",
                },
            ),
        ),
        (
            "chain_errors",
            _chain_error_events(),
            1_000_000,
            23,
            _expected(
                "FAIL",
                ["E_SALT_REUSE", "E_LENGTH_ESCALATION", "E_ITER_REGRESSION"],
                session_statuses={
                    "salt-reuse": "fail",
                    "length-up": "fail",
                    "iter-down": "fail",
                },
            ),
        ),
        (
            "parse_errors",
            _parse_error_events(),
            1_000_000,
            5,
            _expected(
                "FAIL",
                ["E_STAGE", "E_ALGORITHM", "E_PARAM", "E_CRC", "E_FORMAT"],
                session_statuses={"survivor": "pass"},
            ),
        ),
        (
            "scale_small",
            _scale_events(2, 2, "small"),
            1_000_000,
            29,
            _expected("PASS", []),
        ),
        (
            "scale_large",
            _scale_events(12, 5, "large"),
            1_000_000,
            31,
            _expected("PASS", []),
        ),
        (
            "checkpoint",
            _checkpoint_events(),
            1_000_000,
            37,
            _expected("PASS", []),
        ),
        (
            "timeout",
            _scale_events(2, 2, "timeout"),
            1_000_000,
            41,
            _expected(
                "FAIL",
                ["E_TIMEOUT"],
                session_statuses={"timeout-00": "fail", "timeout-01": "fail"},
                config_overrides={"session_timeout_seconds": 0.0},
            ),
        ),
        ("lock", _lock_events(), 1_000_000, 43, _expected("PASS", [])),
        (
            "budget",
            _budget_events(),
            1_000_000,
            47,
            _expected(
                "FAIL",
                ["E_BUDGET"],
                session_statuses={"budget-small": "pass", "budget-large": "fail"},
                config_overrides={"max_derivations_per_session": 5},
            ),
        ),
    ]

    manifest_files: dict[str, object] = {}
    for name, events, max_iterations, seed, expected in sample_definitions:
        encoded, records = _encode_sample(
            events,
            max_iterations=max_iterations,
            shuffle_seed=seed,
        )
        sha256 = hashlib.sha256(encoded).hexdigest()
        sample_path = samples_dir / f"{name}.klp"
        truth_path = truth_dir / f"{name}.json"
        sample_path.write_bytes(encoded)
        truth = _truth_document(
            sample_name=name,
            sha256=sha256,
            max_iterations=max_iterations,
            records=records,
            expected=expected,
        )
        truth_path.write_text(
            json.dumps(truth, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        manifest_files[name] = {
            "sample": str(sample_path.relative_to(root)),
            "truth": str(truth_path.relative_to(root)),
            "sha256": sha256,
            "expected": expected,
        }

    manifest = {
        "schema_version": TRUTH_SCHEMA_VERSION,
        "files": manifest_files,
    }
    manifest_path = truth_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path

