"""Generate deterministic offline samples and their truth manifest."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import shutil
from pathlib import Path
from typing import Any

from .canonical import normalize_config_path, observation_digest_payload

HMAC_KEY_ID = "offline-demo-key-001"
HMAC_KEY = "offline-demo-secret-001"


def _digest(path: str, value: Any, algorithm: str = "sha256") -> str:
    payload = observation_digest_payload(normalize_config_path(path), value)
    if algorithm == "sha256":
        return hashlib.sha256(payload).hexdigest()
    return hmac.new(HMAC_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _record(
    *,
    evidence_id: str,
    host_id: str,
    sequence: int,
    path: str,
    value: Any,
    previous: str | None,
    sync_marker: str,
    algorithm: str = "sha256",
    digest_bits: int = 256,
    sender_value: Any = None,
    digest_override: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "host_id": host_id,
        "sequence": sequence,
        "path": path,
        "value": value,
        "algorithm": algorithm,
        "digest_bits": digest_bits,
        "digest": digest_override
        if digest_override is not None
        else _digest(path, value, algorithm),
        "previous_evidence_id": previous,
        "sync_marker": sync_marker,
        "sender_observed_value": value if sender_value is None else sender_value,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _baseline_entries() -> list[dict[str, Any]]:
    return [
        {
            "host_id": "host-alpha",
            "path": "service.port",
            "source": "policy-service-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 100,
            "ranges": [{"interval": "[8000,8100)", "source": "range-service-main"}],
        },
        {
            "host_id": "host-beta",
            "path": "service.port",
            "source": "policy-service-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 100,
            "ranges": [{"interval": "[8000,8100)", "source": "range-service-main"}],
        },
        {
            "host_id": "host-gamma",
            "path": "features[0].enabled",
            "source": "policy-feature-v2",
            "effective_from_seq": 1,
            "effective_to_seq": 20,
            "ranges": [{"allowed_values": [True], "source": "range-feature-required"}],
        },
        {
            "host_id": "host-hmac",
            "path": "tls.enabled",
            "source": "policy-tls-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 50,
            "ranges": [{"allowed_values": [True], "source": "range-tls-required"}],
        },
        {
            "host_id": "host-endpoint",
            "path": "quota.limit",
            "source": "policy-quota-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 10,
            "ranges": [{"interval": "(100,200]", "source": "range-quota-strict"}],
        },
        {
            "host_id": "host-broken",
            "path": "network.mtu",
            "source": "policy-network-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 20,
            "ranges": [{"interval": "[1200,1500]"}],
        },
        {
            "host_id": "host-cycle",
            "path": "security.level",
            "source": "policy-security-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 20,
            "ranges": [{"allowed_values": ["standard", "strict"]}],
        },
        {
            "host_id": "host-tamper",
            "path": "logging.level",
            "source": "policy-logging-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 20,
            "ranges": [{"allowed_values": ["info", "warn"]}],
        },
        {
            "host_id": "host-width",
            "path": "clock.mode",
            "source": "policy-clock-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 20,
            "ranges": [{"allowed_values": ["utc"]}],
        },
        {
            "host_id": "host-mixed",
            "path": "mixed.value",
            "source": "policy-mixed-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 20,
            "ranges": [{"interval": "[0,2]"}],
        },
        {
            "host_id": "host-conflict",
            "path": "duplicate.value",
            "source": "policy-duplicate-v1",
            "effective_from_seq": 1,
            "effective_to_seq": 20,
            "ranges": [{"interval": "[0,10]"}],
        },
    ]


def generate_samples(root: Path | str) -> dict[str, Any]:
    root = Path(root)
    evidence_dir = root / "evidence"
    baseline_dir = root / "baseline"
    if evidence_dir.exists():
        shutil.rmtree(evidence_dir)
    if baseline_dir.exists():
        shutil.rmtree(baseline_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    baseline_dir.mkdir(parents=True, exist_ok=True)

    baseline = {"schema_version": 1, "baselines": _baseline_entries()}
    baseline_path = baseline_dir / "baseline.json"
    _write_json(baseline_path, baseline)

    alpha_1 = _record(
        evidence_id="alpha-1",
        host_id="host-alpha",
        sequence=1,
        path="service.port",
        value=8080,
        previous=None,
        sync_marker="sync:host-alpha:1",
    )
    alpha_2 = _record(
        evidence_id="alpha-2",
        host_id="host-alpha",
        sequence=2,
        path="service.port",
        value=8090,
        previous="alpha-1",
        sync_marker="sync:host-alpha:2",
    )
    alpha_3 = _record(
        evidence_id="alpha-3",
        host_id="host-alpha",
        sequence=3,
        path="service.port",
        value=9000,
        previous="alpha-2",
        sync_marker="sync:host-alpha:3",
    )
    beta_1 = _record(
        evidence_id="beta-1",
        host_id="host-beta",
        sequence=1,
        path="service.port",
        value=8081,
        previous=None,
        sync_marker="sync:host-beta:1",
    )
    beta_2 = _record(
        evidence_id="beta-2",
        host_id="host-beta",
        sequence=2,
        path="service.port",
        value=9000,
        previous="beta-1",
        sync_marker="sync:host-beta:2",
    )
    gamma_1 = _record(
        evidence_id="gamma-1",
        host_id="host-gamma",
        sequence=1,
        path="features[0].enabled",
        value=True,
        previous=None,
        sync_marker="sync:host-gamma:1",
    )
    gamma_2 = _record(
        evidence_id="gamma-2",
        host_id="host-gamma",
        sequence=2,
        path="features[0].enabled",
        value=True,
        previous="gamma-1",
        sync_marker="sync:host-gamma:2",
    )
    gamma_3 = _record(
        evidence_id="gamma-3",
        host_id="host-gamma",
        sequence=3,
        path="features[0].enabled",
        value=False,
        previous="gamma-2",
        sync_marker="sync:host-gamma:3",
    )
    hmac_1 = _record(
        evidence_id="hmac-1",
        host_id="host-hmac",
        sequence=1,
        path="tls.enabled",
        value=False,
        previous=None,
        sync_marker="sync:host-hmac:1",
        algorithm="hmac-sha256",
    )
    endpoint_1 = _record(
        evidence_id="endpoint-1",
        host_id="host-endpoint",
        sequence=1,
        path="quota.limit",
        value=100,
        previous=None,
        sync_marker="sync:host-endpoint:1",
    )
    endpoint_2 = _record(
        evidence_id="endpoint-2",
        host_id="host-endpoint",
        sequence=2,
        path="quota.limit",
        value=150,
        previous="endpoint-1",
        sync_marker="sync:host-endpoint:2",
    )
    broken_1 = _record(
        evidence_id="broken-1",
        host_id="host-broken",
        sequence=1,
        path="network.mtu",
        value=1500,
        previous=None,
        sync_marker="sync:host-broken:1",
    )
    broken_2 = _record(
        evidence_id="broken-2",
        host_id="host-broken",
        sequence=2,
        path="network.mtu",
        value=1500,
        previous="missing-ghost",
        sync_marker="sync:host-broken:2",
    )
    cycle_1 = _record(
        evidence_id="cycle-1",
        host_id="host-cycle",
        sequence=1,
        path="security.level",
        value="standard",
        previous="cycle-2",
        sync_marker="sync:host-cycle:1",
    )
    cycle_2 = _record(
        evidence_id="cycle-2",
        host_id="host-cycle",
        sequence=2,
        path="security.level",
        value="strict",
        previous="cycle-1",
        sync_marker="sync:host-cycle:2",
    )

    tampered_value = "debug"
    tamper_original = "info"
    tamper_1 = _record(
        evidence_id="tamper-1",
        host_id="host-tamper",
        sequence=1,
        path="logging.level",
        value=tampered_value,
        previous=None,
        sync_marker="sync:host-tamper:1",
        sender_value=tamper_original,
        digest_override=_digest("logging.level", tamper_original),
    )
    width_1 = _record(
        evidence_id="width-1",
        host_id="host-width",
        sequence=1,
        path="clock.mode",
        value="utc",
        previous=None,
        sync_marker="sync:host-width:1",
        digest_bits=512,
    )
    mixed_valid = _record(
        evidence_id="mixed-1",
        host_id="host-mixed",
        sequence=1,
        path="mixed.value",
        value=1,
        previous=None,
        sync_marker="sync:host-mixed:1",
    )
    conflict_a = _record(
        evidence_id="conflict-a",
        host_id="host-conflict",
        sequence=1,
        path="duplicate.value",
        value=3,
        previous=None,
        sync_marker="sync:host-conflict:a",
    )
    conflict_b = _record(
        evidence_id="conflict-b",
        host_id="host-conflict",
        sequence=1,
        path="duplicate.value",
        value=4,
        previous=None,
        sync_marker="sync:host-conflict:b",
    )

    files: dict[str, dict[str, Any]] = {
        "f01_alpha_seq3_out_of_order.json": {
            "schema_version": 1,
            "file_id": "evidence-alpha-3",
            "records": [alpha_3],
        },
        "f02_alpha_seq1_out_of_order.json": {
            "schema_version": 1,
            "file_id": "evidence-alpha-1",
            "records": [alpha_1],
        },
        "f03_alpha_seq2_out_of_order.json": {
            "schema_version": 1,
            "file_id": "evidence-alpha-2",
            "records": [alpha_2],
        },
        "f04_beta_seq2.json": {
            "schema_version": 1,
            "file_id": "evidence-beta-2",
            "records": [beta_2],
        },
        "f05_beta_seq1.json": {
            "schema_version": 1,
            "file_id": "evidence-beta-1",
            "records": [beta_1],
        },
        "f06_gamma_seq3.json": {
            "schema_version": 1,
            "file_id": "evidence-gamma-3",
            "records": [gamma_3],
        },
        "f07_gamma_seq2.json": {
            "schema_version": 1,
            "file_id": "evidence-gamma-2",
            "records": [gamma_2],
        },
        "f08_gamma_seq1.json": {
            "schema_version": 1,
            "file_id": "evidence-gamma-1",
            "records": [gamma_1],
        },
        "f09_hmac.json": {
            "schema_version": 1,
            "file_id": "evidence-hmac",
            "hmac_keys": {HMAC_KEY_ID: HMAC_KEY},
            "records": [hmac_1],
        },
        "f10_endpoint_seq2.json": {
            "schema_version": 1,
            "file_id": "evidence-endpoint-2",
            "records": [endpoint_2],
        },
        "f11_endpoint_seq1.json": {
            "schema_version": 1,
            "file_id": "evidence-endpoint-1",
            "records": [endpoint_1],
        },
        "f12_broken_seq1.json": {
            "schema_version": 1,
            "file_id": "evidence-broken-1",
            "records": [broken_1],
        },
        "f13_broken_seq2.json": {
            "schema_version": 1,
            "file_id": "evidence-broken-2",
            "records": [broken_2],
        },
        "f14_cycle_seq2.json": {
            "schema_version": 1,
            "file_id": "evidence-cycle-2",
            "records": [cycle_2],
        },
        "f15_cycle_seq1.json": {
            "schema_version": 1,
            "file_id": "evidence-cycle-1",
            "records": [cycle_1],
        },
        "f16_tampered.json": {
            "schema_version": 1,
            "file_id": "evidence-tampered",
            "records": [tamper_1],
        },
        "f17_width_mismatch.json": {
            "schema_version": 1,
            "file_id": "evidence-width",
            "records": [width_1],
        },
        "f18_mixed_invalid.json": {
            "schema_version": 1,
            "file_id": "evidence-mixed",
            "records": [
                {
                    **mixed_valid,
                    "record_id": mixed_valid["evidence_id"],
                },
                {
                    "host_id": "host-mixed",
                    "sequence": 2,
                    "path": "bad..path",
                    "value": 1,
                },
            ],
        },
        "f19_conflict_a.json": {
            "schema_version": 1,
            "file_id": "evidence-conflict-a",
            "records": [conflict_a],
        },
        "f20_conflict_b.json": {
            "schema_version": 1,
            "file_id": "evidence-conflict-b",
            "records": [conflict_b],
        },
    }
    for name, payload in files.items():
        _write_json(evidence_dir / name, payload)

    truth_records = [
        {
            "file_id": "evidence-alpha-1",
            "host_id": "host-alpha",
            "sequence": 1,
            "sender_observed_value": 8080,
            "sync_marker": "sync:host-alpha:1",
            "expected": "allowed",
        },
        {
            "file_id": "evidence-alpha-2",
            "host_id": "host-alpha",
            "sequence": 2,
            "sender_observed_value": 8090,
            "sync_marker": "sync:host-alpha:2",
            "expected": "allowed",
        },
        {
            "file_id": "evidence-alpha-3",
            "host_id": "host-alpha",
            "sequence": 3,
            "sender_observed_value": 9000,
            "sync_marker": "sync:host-alpha:3",
            "expected": "drift",
        },
        {
            "file_id": "evidence-beta-2",
            "host_id": "host-beta",
            "sequence": 2,
            "sender_observed_value": 9000,
            "sync_marker": "sync:host-beta:2",
            "expected": "drift",
        },
        {
            "file_id": "evidence-gamma-3",
            "host_id": "host-gamma",
            "sequence": 3,
            "sender_observed_value": False,
            "sync_marker": "sync:host-gamma:3",
            "expected": "drift",
        },
        {
            "file_id": "evidence-endpoint-1",
            "host_id": "host-endpoint",
            "sequence": 1,
            "sender_observed_value": 100,
            "sync_marker": "sync:host-endpoint:1",
            "expected": "drift_exclusive_endpoint",
        },
        {
            "file_id": "evidence-broken-2",
            "host_id": "host-broken",
            "sequence": 2,
            "sender_observed_value": 1500,
            "sync_marker": "sync:host-broken:2",
            "expected": "chain_break",
        },
        {
            "file_id": "evidence-cycle-1",
            "host_id": "host-cycle",
            "sequence": 1,
            "sender_observed_value": "standard",
            "sync_marker": "sync:host-cycle:1",
            "expected": "cycle",
        },
        {
            "file_id": "evidence-tampered",
            "host_id": "host-tamper",
            "sequence": 1,
            "sender_observed_value": "info",
            "sync_marker": "sync:host-tamper:1",
            "expected": "tampered",
        },
        {
            "file_id": "evidence-width",
            "host_id": "host-width",
            "sequence": 1,
            "sender_observed_value": "utc",
            "sync_marker": "sync:host-width:1",
            "expected": "digest_width_mismatch",
        },
        {
            "file_id": "evidence-conflict-a",
            "host_id": "host-conflict",
            "sequence": 1,
            "sender_observed_value": 3,
            "sync_marker": "sync:host-conflict:a",
            "expected": "conflict",
        },
        {
            "file_id": "evidence-conflict-b",
            "host_id": "host-conflict",
            "sequence": 1,
            "sender_observed_value": 4,
            "sync_marker": "sync:host-conflict:b",
            "expected": "conflict",
        },
    ]
    manifest = {
        "schema_version": 1,
        "sample_id": "offline-acceptance-v1",
        "clock_dependency": "none",
        "hmac_key_id": HMAC_KEY_ID,
        "truth": {
            "records": truth_records,
            "drift_points": [
                {
                    "host_id": "host-alpha",
                    "path": "service.port",
                    "sequence": 3,
                },
                {
                    "host_id": "host-beta",
                    "path": "service.port",
                    "sequence": 2,
                },
                {
                    "host_id": "host-gamma",
                    "path": "features[0].enabled",
                    "sequence": 3,
                },
                {
                    "host_id": "host-hmac",
                    "path": "tls.enabled",
                    "sequence": 1,
                },
                {
                    "host_id": "host-endpoint",
                    "path": "quota.limit",
                    "sequence": 1,
                },
            ],
            "chain_break_codes": {
                "host-broken": ["E_LINK_TARGET_MISSING", "E_CHAIN_BROKEN"],
                "host-cycle": ["E_LINK_CYCLE", "E_LINK_SEQUENCE_ORDER", "E_CHAIN_BROKEN"],
            },
            "tampered": [
                {
                    "host_id": "host-tamper",
                    "sequence": 1,
                    "code": "E_TAMPERED",
                }
            ],
            "algorithm_errors": [
                {
                    "host_id": "host-width",
                    "sequence": 1,
                    "code": "E_DIGEST_WIDTH_MISMATCH",
                }
            ],
            "conflicts": [
                {
                    "host_id": "host-conflict",
                    "sequence": 1,
                    "code": "E_EVIDENCE_CONFLICT",
                }
            ],
            "event_clusters": {
                "service.port": ["host-alpha", "host-beta"],
            },
        },
    }
    _write_json(root / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic offline samples")
    parser.add_argument("--out", required=True, help="sample output directory")
    args = parser.parse_args(argv)
    generate_samples(Path(args.out))
    print(f"samples written to {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
