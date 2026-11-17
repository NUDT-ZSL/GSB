"""Deterministic offline sample data generator."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .models import CERT_SCHEMA, CHAIN_SCHEMA, SNAPSHOT_SCHEMA


OBSERVED_AT_MS = 1_700_000_000_000
_NORMAL_FROM = OBSERVED_AT_MS - 10_000
_NORMAL_TO = OBSERVED_AT_MS + 10_000_000


def _fingerprint(identifier: str) -> str:
    return "sha256:" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def _certificate(
    cert_id: str,
    issuer_id: str,
    *,
    valid_from_ms: int = _NORMAL_FROM,
    valid_to_ms: int = _NORMAL_TO,
    revoked: bool = False,
) -> dict[str, Any]:
    return {
        "id": cert_id,
        "issuer_id": issuer_id,
        "public_key_fingerprint": _fingerprint(cert_id),
        "valid_from_ms": valid_from_ms,
        "valid_to_ms": valid_to_ms,
        "revoked": revoked,
    }


def _revocation(
    cert_id: str, revoked_at_ms: int, reason: str
) -> dict[str, Any]:
    return {
        "cert_id": cert_id,
        "revoked_at_ms": revoked_at_ms,
        "reason": reason,
    }


def certificates_document() -> dict[str, Any]:
    return {
        "schema": CERT_SCHEMA,
        "certificates": [
            _certificate("root-a", "root-a"),
            _certificate("root-b", "root-b"),
            _certificate("int-a", "root-a"),
            _certificate("int-b", "root-b"),
            _certificate("int-revoked", "root-a"),
            _certificate("int-missing", "root-a"),
            _certificate("leaf-ok", "int-a"),
            _certificate("leaf-revoked", "int-a"),
            _certificate("leaf-int-revoked", "int-revoked"),
            _certificate(
                "leaf-expired",
                "int-a",
                valid_to_ms=OBSERVED_AT_MS - 1,
            ),
            _certificate("leaf-b", "int-b"),
            _certificate("leaf-missing", "int-missing"),
            _certificate("leaf-broken", "int-a"),
            _certificate("leaf-late", "int-a"),
        ],
    }


def snapshot_documents() -> list[dict[str, Any]]:
    return [
        {
            "schema": SNAPSHOT_SCHEMA,
            "snapshots": [
                {
                    "version": 1,
                    "policy_version": "1.0.0",
                    "trusted_roots": ["root-a", "root-b"],
                    "revoked_entries": [],
                    "observed_at_ms": OBSERVED_AT_MS,
                }
            ],
        },
        {
            "schema": SNAPSHOT_SCHEMA,
            "snapshots": [
                {
                    "version": 2,
                    "policy_version": "1.1.0",
                    "trusted_roots": ["root-a", "root-b"],
                    "revoked_entries": [
                        _revocation(
                            "int-revoked",
                            OBSERVED_AT_MS - 5_000,
                            "key_compromise",
                        )
                    ],
                    "observed_at_ms": OBSERVED_AT_MS,
                }
            ],
        },
        {
            "schema": SNAPSHOT_SCHEMA,
            "snapshots": [
                {
                    "version": 3,
                    "policy_version": "1.2.0",
                    "trusted_roots": ["root-a"],
                    "revoked_entries": [
                        _revocation(
                            "int-revoked",
                            OBSERVED_AT_MS - 5_000,
                            "key_compromise",
                        ),
                        _revocation(
                            "leaf-revoked",
                            OBSERVED_AT_MS - 3_000,
                            "key_compromise",
                        ),
                        _revocation(
                            "leaf-late",
                            OBSERVED_AT_MS - 1_000,
                            "late_revocation",
                        ),
                    ],
                    "observed_at_ms": OBSERVED_AT_MS,
                }
            ],
        },
    ]


def _summary(conclusion: str) -> str:
    return f"{conclusion}:snapshot=3:policy=1.2.0"


def chains_document() -> dict[str, Any]:
    chains = [
        ("chain-broken", ["leaf-broken", "int-b", "root-b"], "untrusted"),
        ("chain-empty", [], "untrusted"),
        ("chain-expired", ["leaf-expired", "int-a", "root-a"], "partially_trusted"),
        (
            "chain-int-revoked",
            ["leaf-int-revoked", "int-revoked", "root-a"],
            "partially_trusted",
        ),
        ("chain-late", ["leaf-late", "int-a", "root-a"], "partially_trusted"),
        (
            "chain-missing-intermediate",
            ["leaf-missing", "root-a"],
            "untrusted",
        ),
        ("chain-ok", ["leaf-ok", "int-a", "root-a"], "trusted"),
        (
            "chain-revoked-leaf",
            ["leaf-revoked", "int-a", "root-a"],
            "partially_trusted",
        ),
        (
            "chain-root-untrusted",
            ["leaf-b", "int-b", "root-b"],
            "untrusted",
        ),
    ]
    return {
        "schema": CHAIN_SCHEMA,
        "chains": [
            {
                "chain_id": chain_id,
                "certificate_ids": certificate_ids,
                "deadline_ms": 1_000,
                "expected_summary": _summary(conclusion),
            }
            for chain_id, certificate_ids, conclusion in chains
        ],
    }


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def generate_samples(output_directory: str | Path) -> dict[str, Any]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    snapshot_directory = output / "snapshots"
    snapshot_directory.mkdir(parents=True, exist_ok=True)

    documents: dict[Path, dict[str, Any]] = {
        output / "certificates.json": certificates_document(),
        output / "chains.json": chains_document(),
    }
    for index, document in enumerate(snapshot_documents(), start=1):
        documents[snapshot_directory / f"v{index}.json"] = document
    for path, document in sorted(
        documents.items(), key=lambda item: str(item[0])
    ):
        atomic_write_json(path, document, pretty=True)

    manifest = {
        "schema": "trustman.sample-manifest/v1",
        "dataset": "offline-deterministic-demo",
        "observed_at_ms": OBSERVED_AT_MS,
        "effective_snapshot_version": 3,
        "effective_policy_version": "1.2.0",
        "files": {
            str(path.relative_to(output)).replace("\\", "/"): _file_sha256(path)
            for path in sorted(
                documents, key=lambda item: str(item.relative_to(output))
            )
        },
        "expected_conclusions": {
            chain["chain_id"]: chain["expected_summary"]
            for chain in documents[output / "chains.json"]["chains"]
        },
    }
    atomic_write_json(output / "manifest.json", manifest, pretty=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic offline certificate-chain samples"
    )
    parser.add_argument(
        "--out",
        default="samples",
        help="output directory (default: samples)",
    )
    args = parser.parse_args(argv)
    manifest = generate_samples(args.out)
    print(json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
