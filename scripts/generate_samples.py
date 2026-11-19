#!/usr/bin/env python3
"""Generate deterministic local fixtures for the sampling compatibility lab."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sampling_compat.engine import SamplingApplication
from sampling_compat.snapshots import (
    SNAPSHOT_FORMAT,
    SNAPSHOT_FORMAT_VERSION,
    make_envelope,
)
from sampling_compat.utils import checksum_obj, pretty_json

SAMPLE_DIR = ROOT / "samples"


def _uniform(seed_material: str, offset: int = 0) -> float:
    digest = hashlib.sha256(f"{seed_material}\0{offset}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / float(1 << 53)


def deterministic_logits(
    *,
    seed: int,
    vocab_size: int,
    steps: int,
) -> list[list[float]]:
    rows: list[list[float]] = []
    for step in range(steps):
        row: list[float] = []
        peak = (seed + step * 7) % vocab_size
        secondary = (seed * 3 + step * 11 + 5) % vocab_size
        for token_id in range(vocab_size):
            material = f"logits:{seed}:{step}:{token_id}"
            u1 = max(_uniform(material, 0), 1.0 / float(1 << 53))
            u2 = _uniform(material, 1)
            gaussian = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
            wave = 0.42 * math.cos((token_id + 1) * (step + 2) * 0.173 + seed % 19)
            value = 1.08 * gaussian + wave
            if token_id == peak:
                value += 1.35
            if token_id == secondary:
                value += 0.55
            row.append(round(float(value), 10))
        rows.append(row)
    return rows


def make_vocabularies() -> dict[str, Any]:
    entries = {
        "tok-2024-01": {
            "size": 32,
            "tokens": [f"t24:{index:03d}" for index in range(32)],
        },
        "tok-2025-02": {
            "size": 48,
            "tokens": [f"t25:{index:03d}" for index in range(48)],
        },
    }
    return {
        "snapshot_version": 1,
        "checksum": checksum_obj(entries),
        "entries": entries,
    }


def v1_profile(temperature_literal: str, top_k: int) -> str:
    return pretty_json(
        {
            "version": "v1",
            "temperature": temperature_literal,
            "top_k": top_k,
        }
    )


def v2_profile(
    *,
    temperature_literal: str,
    top_k: int,
    top_p_literal: str,
    repetition_literal: str,
    vocab_version: str,
) -> str:
    return pretty_json(
        {
            "version": "v2",
            "temperature": {"value": temperature_literal, "unit": "ratio"},
            "top_k": {"value": top_k, "unit": "tokens"},
            "top_p": {"value": top_p_literal, "unit": "probability"},
            "repetition_penalty": {
                "value": repetition_literal,
                "unit": "multiplier",
            },
            "vocab_version": vocab_version,
        }
    )


def v3_profile(
    *,
    temperature_literal: str,
    top_k_literal: str,
    top_p_literal: str,
    repetition_literal: str,
    vocab_version: str,
) -> str:
    return "\n".join(
        [
            "!sampling-config v3",
            f"temperature = {temperature_literal} ratio",
            f"top_k = {top_k_literal} tokens",
            f"top_p = {top_p_literal} probability",
            f"repetition_penalty = {repetition_literal} multiplier",
            f"vocab_version = {vocab_version}",
            "",
        ]
    )


def build_requests() -> list[dict[str, Any]]:
    return [
        {
            "request_id": "req-0001",
            "prompt_tokens": [1, 5, 9, 3, 5],
            "tokenizer_version": "tok-2024-01",
            "sampling_seed": 240101,
            "_logits_seed": 41001,
            "_steps": 6,
            "_profiles": {
                "v1": v1_profile(" 7.00e-1 ", 8),
                "v2": v2_profile(
                    temperature_literal="7.00e-1",
                    top_k=8,
                    top_p_literal="1.0",
                    repetition_literal="1.0",
                    vocab_version="tok-2024-01",
                ),
                "v3": v3_profile(
                    temperature_literal="7.00e-1",
                    top_k_literal="8.0e0",
                    top_p_literal="1.0e0",
                    repetition_literal="1.0e0",
                    vocab_version="tok-2024-01",
                ),
            },
        },
        {
            "request_id": "req-0002",
            "prompt_tokens": [2, 7, 13, 21, 7],
            "tokenizer_version": "tok-2025-02",
            "sampling_seed": 250202,
            "_logits_seed": 42002,
            "_steps": 7,
            "_profiles": {
                "v1": v1_profile("1.250e+0", 12),
                "v2": v2_profile(
                    temperature_literal="1.250e+0",
                    top_k=12,
                    top_p_literal="8.50e-1",
                    repetition_literal="1.10e0",
                    vocab_version="tok-2025-02",
                ),
                "v3": v3_profile(
                    temperature_literal="1.250e+0",
                    top_k_literal="1.2e1",
                    top_p_literal="8.50e-1",
                    repetition_literal="1.10e0",
                    vocab_version="tok-2025-02",
                ),
            },
        },
        {
            "request_id": "req-0003",
            "prompt_tokens": [4, 4, 11, 0, 19],
            "tokenizer_version": "tok-2024-01",
            "sampling_seed": 240303,
            "_logits_seed": 43003,
            "_steps": 5,
            "_profiles": {
                "v1": v1_profile("5.0e-1", 6),
                "v2": v2_profile(
                    temperature_literal="5.0e-1",
                    top_k=6,
                    top_p_literal="1.0",
                    repetition_literal="1.0",
                    vocab_version="tok-2024-01",
                ),
                "v3": v3_profile(
                    temperature_literal="5.0e-1",
                    top_k_literal="6.0e0",
                    top_p_literal="1.0e0",
                    repetition_literal="1.0e0",
                    vocab_version="tok-2024-01",
                ),
            },
        },
    ]


def write_requests(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for spec in specs:
        logits = deterministic_logits(
            seed=spec["_logits_seed"],
            vocab_size=32 if spec["tokenizer_version"] == "tok-2024-01" else 48,
            steps=spec["_steps"],
        )
        request = {
            "request_id": spec["request_id"],
            "prompt_tokens": spec["prompt_tokens"],
            "tokenizer_version": spec["tokenizer_version"],
            "sampling_seed": spec["sampling_seed"],
            "sampling_profile": spec["_profiles"],
            "logits": logits,
        }
        output.append(request)
    lines = [
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for request in output
    ]
    (SAMPLE_DIR / "requests.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def _v2_payload(
    *,
    temperature: str,
    top_k: Any,
    top_p: str,
    repetition: str,
    vocab_version: str,
) -> dict[str, Any]:
    return {
        "version": "v2",
        "temperature": {"value": temperature, "unit": "ratio"},
        "top_k": {"value": top_k, "unit": "tokens"},
        "top_p": {"value": top_p, "unit": "probability"},
        "repetition_penalty": {"value": repetition, "unit": "multiplier"},
        "vocab_version": vocab_version,
    }


def write_error_samples(requests: list[dict[str, Any]]) -> None:
    config_dir = SAMPLE_DIR / "errors" / "configs"
    request_dir = SAMPLE_DIR / "errors" / "requests"
    snapshot_dir = SAMPLE_DIR / "errors" / "snapshots"
    config_dir.mkdir(parents=True, exist_ok=True)
    request_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    (config_dir / "truncated_header_v3.txt").write_text(
        "!sampling-con v3\n"
        "temperature = 7.0e-1 ratio\n"
        "top_k = 8 tokens\n"
        "top_p = 1.0 probability\n"
        "repetition_penalty = 1.0 multiplier\n"
        "vocab_version = tok-2024-01\n",
        encoding="utf-8",
    )
    (config_dir / "unknown_version_v3.txt").write_text(
        "!sampling-config v9\n"
        "temperature = 7.0e-1 ratio\n"
        "top_k = 8 tokens\n"
        "top_p = 1.0 probability\n"
        "repetition_penalty = 1.0 multiplier\n"
        "vocab_version = tok-2024-01\n",
        encoding="utf-8",
    )

    missing_top_p = _v2_payload(
        temperature="7.0e-1",
        top_k=8,
        top_p="1.0",
        repetition="1.0",
        vocab_version="tok-2024-01",
    )
    missing_top_p.pop("top_p")
    (config_dir / "missing_top_p_v2.json").write_text(
        pretty_json(missing_top_p),
        encoding="utf-8",
    )
    (config_dir / "truncated_header_v2.json").write_text(
        pretty_json(
            {
                "versio": "v2",
                "temperature": {"value": "0.7", "unit": "ratio"},
                "top_k": {"value": 8, "unit": "tokens"},
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "out_of_range_temperature_v2.json").write_text(
        pretty_json(
            _v2_payload(
                temperature="-0.5",
                top_k=8,
                top_p="1.0",
                repetition="1.0",
                vocab_version="tok-2024-01",
            )
        ),
        encoding="utf-8",
    )
    (config_dir / "top_p_out_of_range_v3.txt").write_text(
        v3_profile(
            temperature_literal="7.0e-1",
            top_k_literal="8",
            top_p_literal="1.5e0",
            repetition_literal="1.0",
            vocab_version="tok-2024-01",
        ),
        encoding="utf-8",
    )
    (config_dir / "top_k_nonpositive_v3.txt").write_text(
        v3_profile(
            temperature_literal="7.0e-1",
            top_k_literal="0",
            top_p_literal="1.0",
            repetition_literal="1.0",
            vocab_version="tok-2024-01",
        ),
        encoding="utf-8",
    )
    (config_dir / "nonpositive_repetition_v2.json").write_text(
        pretty_json(
            _v2_payload(
                temperature="0.7",
                top_k=8,
                top_p="1.0",
                repetition="0.0",
                vocab_version="tok-2024-01",
            )
        ),
        encoding="utf-8",
    )
    (config_dir / "bad_numeric_temperature_v1.json").write_text(
        pretty_json(
            {
                "version": "v1",
                "temperature": "warm-ish",
                "top_k": 8,
            }
        ),
        encoding="utf-8",
    )

    base = requests[0]
    token_range = {
        **base,
        "request_id": "err-token-range",
        "prompt_tokens": [1, 999, 3],
        "sampling_seed": 990001,
    }
    _write_jsonl(request_dir / "out_of_vocab_token.jsonl", [token_range])

    malformed_logits = json.loads(json.dumps(base))
    malformed_logits["request_id"] = "err-logits-shape"
    malformed_logits["sampling_seed"] = 990002
    malformed_logits["logits"] = [list(row) for row in base["logits"]]
    malformed_logits["logits"][2] = malformed_logits["logits"][2][:-1]
    _write_jsonl(request_dir / "logits_shape_mismatch.jsonl", [malformed_logits])

    vocab_mismatch = json.loads(json.dumps(requests[1]))
    vocab_mismatch["request_id"] = "err-vocab-mismatch"
    vocab_mismatch["tokenizer_version"] = "tok-2024-01"
    vocab_mismatch["prompt_tokens"] = [1, 2, 3]
    vocab_mismatch["sampling_seed"] = 990003
    vocab_mismatch["logits"] = deterministic_logits(seed=990003, vocab_size=32, steps=3)
    _write_jsonl(request_dir / "vocab_mismatch.jsonl", [vocab_mismatch])

    envelope = make_envelope(
        "sampling_decisions",
        7,
        {
            "request_id": "req-0001",
            "token_ids": [1, 2, 3],
            "note": "checksum was computed before this field was tampered",
        },
    )
    envelope["content"]["tampered"] = True
    corrupt = {
        "format": SNAPSHOT_FORMAT,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "snapshots": {"sampling_decisions": envelope},
    }
    (snapshot_dir / "corrupt_checksum.json").write_text(
        pretty_json(corrupt),
        encoding="utf-8",
    )


def write_bootstrap_snapshot(vocabularies: dict[str, Any]) -> None:
    snapshot_dir = SAMPLE_DIR / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "format": SNAPSHOT_FORMAT,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "snapshots": {
            "vocab_table": make_envelope("vocab_table", 1, vocabularies),
            "normalized_params": make_envelope("normalized_params", 1, {}),
            "sampling_decisions": make_envelope("sampling_decisions", 1, {}),
            "migration_records": make_envelope("migration_records", 1, {}),
        },
    }
    (snapshot_dir / "bootstrap.json").write_text(
        pretty_json(document),
        encoding="utf-8",
    )


def write_expected(app: SamplingApplication) -> None:
    expected: dict[str, Any] = {
        "algorithm": "sampling-compat.float64.v1",
        "generated_from": "committed fixed-seed JSONL fixtures",
        "requests": {},
        "adjudication": {},
    }
    from sampling_compat.errors import BusinessError

    for request_id, request in app.requests.items():
        request_expected: dict[str, Any] = {}
        for version in request.profile_versions():
            decision = app.run_request(
                request_id,
                version,
                seed=request.sampling_seed,
                record_error=False,
            )
            request_expected[version] = {
                "token_ids": decision["token_ids"],
                "token_sha256": decision["token_sha256"],
                "normalized_params_checksum": decision["normalized_params_checksum"],
                "artifact_checksum": decision["artifact_checksum"],
            }
        expected["requests"][request_id] = request_expected
        try:
            conclusion = app.adjudicate(request_id, record_error=False)
            expected["adjudication"][request_id] = conclusion
        except BusinessError as error:
            expected["adjudication"][request_id] = {
                "status": "ERROR",
                "error": error.to_dict(),
            }
    (SAMPLE_DIR / "expected.json").write_text(
        pretty_json(expected),
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    vocabularies = make_vocabularies()
    (SAMPLE_DIR / "vocabularies.json").write_text(
        pretty_json(vocabularies),
        encoding="utf-8",
    )
    request_specs = build_requests()
    requests = write_requests(request_specs)
    write_error_samples(requests)
    write_bootstrap_snapshot(vocabularies)

    app = SamplingApplication(
        samples_dir=SAMPLE_DIR,
        snapshot_path=ROOT / "runtime" / "generator-snapshots.json",
    )
    write_expected(app)
    print(f"Generated samples in {SAMPLE_DIR}")
    print(f"Requests: {', '.join(app.request_ids())}")


if __name__ == "__main__":
    main()
