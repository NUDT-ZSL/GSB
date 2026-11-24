"""Fixed-seed deterministic certificate-path sample generator."""

from __future__ import annotations

import random
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .canon import canonical_bytes, canonical_text, make_sync_marker, sha256_file


DEFAULT_SEED = 42
DEFAULT_CLOCK_MS = 1_700_000_000_000


def _fp(nibble: str) -> str:
    return "sha256:" + nibble * 64


def _fp2(prefix: str) -> str:
    return "sha256:" + prefix * 32


ROOT_ALPHA = _fp("1")
ROOT_GAMMA = _fp("2")
ROOT_BETA = _fp("3")
INTER_SHARED = _fp("4")
ROOT_STRICT = _fp("5")
ROOT_LEGACY = _fp("6")
ROOT_PARTIAL = _fp("7")
INTER_REVOKED = _fp("8")
INTER_STRICT = _fp("9")
LEAF_MAIN = _fp("c")
LEAF_REVOKED = _fp("d")
LEAF_EXPIRED = _fp("e")
LEAF_THROUGH_REVOKED = _fp("f")
LEAF_BAD_SIG = _fp("a")
LEAF_STRICT = _fp("b")
LEAF_LEGACY = _fp("0")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value) + b"\n")


def _certificate(
    certificate_id: str,
    subject_id: str,
    issuer_id: str,
    fingerprint: str,
    signature_digest: str,
    clock: int,
    *,
    cross_signed: bool = False,
    revoked: bool = False,
    not_before: int | None = None,
    not_after: int | None = None,
) -> dict[str, Any]:
    return {
        "certificate_id": certificate_id,
        "subject_id": subject_id,
        "issuer_id": issuer_id,
        "public_key_fingerprint": fingerprint,
        "signature_digest": signature_digest,
        "not_before_ms": clock - 86_400_000 if not_before is None else not_before,
        "not_after_ms": clock + 86_400_000 if not_after is None else not_after,
        "cross_signed": cross_signed,
        "revoked": revoked,
    }


def _certificates(clock: int) -> list[dict[str, Any]]:
    leaf_partial = _fp2("c0")
    leaf_rootbeta = _fp2("c1")
    leaf_untrusted = _fp2("c2")
    return [
        _certificate(
            "cert-root-alpha", "root-alpha", "root-alpha", ROOT_ALPHA, ROOT_ALPHA, clock
        ),
        _certificate(
            "cert-root-gamma", "root-gamma", "root-gamma", ROOT_GAMMA, ROOT_GAMMA, clock
        ),
        _certificate(
            "cert-root-beta", "root-beta", "root-beta", ROOT_BETA, ROOT_BETA, clock
        ),
        _certificate(
            "cert-root-strict",
            "root-strict",
            "root-strict",
            ROOT_STRICT,
            ROOT_STRICT,
            clock,
        ),
        _certificate(
            "cert-root-legacy",
            "root-legacy",
            "root-legacy",
            ROOT_LEGACY,
            ROOT_LEGACY,
            clock,
        ),
        _certificate(
            "cert-root-partial",
            "root-partial",
            "root-partial",
            ROOT_PARTIAL,
            ROOT_PARTIAL,
            clock,
        ),
        _certificate(
            "cert-inter-cross-a",
            "inter-shared",
            "root-alpha",
            INTER_SHARED,
            ROOT_ALPHA,
            clock,
            cross_signed=True,
        ),
        _certificate(
            "cert-inter-cross-b",
            "inter-shared",
            "root-beta",
            INTER_SHARED,
            ROOT_BETA,
            clock,
            cross_signed=True,
        ),
        _certificate(
            "cert-inter-cross-c",
            "inter-shared",
            "root-gamma",
            INTER_SHARED,
            ROOT_GAMMA,
            clock,
            cross_signed=True,
        ),
        _certificate(
            "cert-inter-revoked",
            "inter-revoked",
            "root-beta",
            INTER_REVOKED,
            ROOT_BETA,
            clock,
            revoked=True,
        ),
        _certificate(
            "cert-inter-strict",
            "inter-strict",
            "root-strict",
            INTER_STRICT,
            ROOT_STRICT,
            clock,
        ),
        _certificate(
            "cert-leaf-main",
            "leaf-main",
            "inter-shared",
            LEAF_MAIN,
            INTER_SHARED,
            clock,
        ),
        _certificate(
            "cert-leaf-revoked",
            "leaf-revoked",
            "inter-shared",
            LEAF_REVOKED,
            INTER_SHARED,
            clock,
            revoked=True,
        ),
        _certificate(
            "cert-leaf-expired",
            "leaf-expired",
            "inter-shared",
            LEAF_EXPIRED,
            INTER_SHARED,
            clock,
            not_before=clock - 10_000_000,
            not_after=clock - 1_000_000,
        ),
        _certificate(
            "cert-leaf-through-revoked",
            "leaf-through-revoked",
            "inter-revoked",
            LEAF_THROUGH_REVOKED,
            INTER_REVOKED,
            clock,
        ),
        _certificate(
            "cert-leaf-bad-sig",
            "leaf-bad-sig",
            "inter-shared",
            LEAF_BAD_SIG,
            ROOT_ALPHA,
            clock,
        ),
        _certificate(
            "cert-leaf-strict",
            "leaf-strict",
            "inter-strict",
            LEAF_STRICT,
            INTER_STRICT,
            clock,
        ),
        _certificate(
            "cert-leaf-legacy",
            "leaf-legacy",
            "root-legacy",
            LEAF_LEGACY,
            ROOT_LEGACY,
            clock,
        ),
        _certificate(
            "cert-leaf-partial",
            "leaf-partial",
            "root-partial",
            leaf_partial,
            ROOT_PARTIAL,
            clock,
        ),
        _certificate(
            "cert-leaf-rootbeta",
            "leaf-rootbeta",
            "root-beta",
            leaf_rootbeta,
            ROOT_BETA,
            clock,
        ),
        _certificate(
            "cert-leaf-untrusted",
            "leaf-untrusted",
            "root-gamma",
            leaf_untrusted,
            ROOT_GAMMA,
            clock,
        ),
    ]


def _chains() -> list[dict[str, Any]]:
    return [
        {
            "chain_id": "chain-cross",
            "certificate_ids": [
                "cert-leaf-main",
                "cert-inter-cross-a",
                "cert-root-alpha",
            ],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-limited",
            "certificate_ids": [
                "cert-leaf-main",
                "cert-inter-cross-a",
                "cert-root-alpha",
            ],
            "enumeration_limit": 2,
        },
        {
            "chain_id": "chain-direct-beta",
            "certificate_ids": ["cert-leaf-rootbeta", "cert-root-beta"],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-legacy",
            "certificate_ids": ["cert-leaf-legacy", "cert-root-legacy"],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-partial",
            "certificate_ids": ["cert-leaf-partial", "cert-root-partial"],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-strict",
            "certificate_ids": [
                "cert-leaf-strict",
                "cert-inter-strict",
                "cert-root-strict",
            ],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-revoked-leaf",
            "certificate_ids": [
                "cert-leaf-revoked",
                "cert-inter-cross-a",
                "cert-root-alpha",
            ],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-revoked-intermediate",
            "certificate_ids": [
                "cert-leaf-through-revoked",
                "cert-inter-revoked",
                "cert-root-beta",
            ],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-expired-leaf",
            "certificate_ids": [
                "cert-leaf-expired",
                "cert-inter-cross-a",
                "cert-root-alpha",
            ],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-bad-signature",
            "certificate_ids": [
                "cert-leaf-bad-sig",
                "cert-inter-cross-a",
                "cert-root-alpha",
            ],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-root-untrusted",
            "certificate_ids": ["cert-leaf-untrusted", "cert-root-gamma"],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-issuer-break",
            "certificate_ids": ["cert-leaf-main", "cert-root-beta"],
            "enumeration_limit": 8,
        },
        {
            "chain_id": "chain-missing-intermediate",
            "certificate_ids": [
                "cert-leaf-orphan",
                "cert-inter-missing",
                "cert-root-beta",
            ],
            "enumeration_limit": 8,
        },
    ]


def _v1(clock: int) -> dict[str, Any]:
    return {
        "format": "certpath.v1",
        "relative_effective_seconds": 7,
        "trusted_root_ids": [
            "cert-root-alpha",
            "cert-root-legacy",
            "cert-root-partial",
        ],
        "revoked_root_ids": [],
        "no_path_length_constraint": True,
    }


def _v2_records(clock: int) -> list[list[dict[str, Any]]]:
    shards = [
        [
            {
                "anchor_id": "v2-anchor-alpha",
                "fingerprint": ROOT_ALPHA,
                "subject_id": "root-alpha",
                "effective_ms": clock + 7_100,
                "generation": 2,
                "path_length": 2,
                "trusted": False,
                "tombstone": True,
                "revoked": False,
            },
            {
                "anchor_id": "v2-anchor-beta",
                "fingerprint": ROOT_BETA,
                "subject_id": "root-beta",
                "effective_ms": clock + 7_200,
                "generation": 2,
                "path_length": 2,
                "trusted": True,
                "tombstone": False,
                "revoked": False,
            },
        ],
        [
            {
                "anchor_id": "v2-anchor-gamma",
                "fingerprint": ROOT_GAMMA,
                "subject_id": "root-gamma",
                "effective_ms": clock + 7_300,
                "generation": 2,
                "path_length": 2,
                "trusted": False,
                "tombstone": False,
                "revoked": True,
            },
            {
                "anchor_id": "v2-anchor-partial",
                "fingerprint": ROOT_PARTIAL,
                "subject_id": "root-partial",
                "effective_ms": clock + 7_400,
                "generation": 2,
                "path_length": 0,
                "trusted": True,
                "tombstone": False,
                "revoked": False,
            },
            {
                "anchor_id": "v2-anchor-strict",
                "fingerprint": ROOT_STRICT,
                "subject_id": "root-strict",
                "effective_ms": clock + 7_500,
                "generation": 2,
                "path_length": 0,
                "trusted": True,
                "tombstone": False,
                "revoked": False,
            },
        ],
    ]
    return shards


def _write_v2(v2_dir: Path, clock: int, shards: list[list[dict[str, Any]]]) -> None:
    shards_dir = v2_dir / "shards"
    if shards_dir.exists():
        shutil.rmtree(shards_dir)
    shards_dir.mkdir(parents=True, exist_ok=True)
    meta_path = v2_dir / "meta.db"
    if meta_path.exists():
        meta_path.unlink()
    shard_meta: list[tuple[str, str, int, int, int, str]] = []
    for index, records in enumerate(shards):
        shard_id = f"shard-{index}"
        rel_file = f"shards/shard_{index}.json"
        shard_path = v2_dir / rel_file
        payload = {
            "format": "certpath.v2.shard",
            "version": 2,
            "shard_id": shard_id,
            "generation": 2,
            "effective_ms": clock + 7_000 + index,
            "records": records,
        }
        _write_json(shard_path, payload)
        shard_meta.append(
            (
                shard_id,
                rel_file,
                2,
                clock + 7_000 + index,
                len(records),
                sha256_file(shard_path),
            )
        )
    connection = sqlite3.connect(meta_path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE snapshot_meta ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE shards ("
            " shard_id TEXT PRIMARY KEY,"
            " file TEXT NOT NULL,"
            " generation INTEGER NOT NULL,"
            " effective_ms INTEGER NOT NULL,"
            " record_count INTEGER NOT NULL,"
            " digest TEXT NOT NULL)"
        )
        meta = {
            "format": "certpath.v2.meta",
            "version": 2,
            "generation": 2,
            "effective_ms": clock + 7_000,
            "shard_count": len(shards),
            "path_length_metric": "intermediate_certificates",
        }
        for key, value in sorted(meta.items()):
            connection.execute(
                "INSERT INTO snapshot_meta(key, value) VALUES (?, ?)",
                (key, canonical_text(value)),
            )
        connection.executemany(
            "INSERT INTO shards"
            "(shard_id, file, generation, effective_ms, record_count, digest)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            shard_meta,
        )
        connection.commit()
    finally:
        connection.close()


def _truth(clock: int, seed: int) -> dict[str, Any]:
    p = {
        "leaf_main": LEAF_MAIN,
        "leaf_revoked": LEAF_REVOKED,
        "leaf_expired": LEAF_EXPIRED,
        "leaf_through_revoked": LEAF_THROUGH_REVOKED,
        "leaf_bad_sig": LEAF_BAD_SIG,
        "leaf_strict": LEAF_STRICT,
        "leaf_legacy": LEAF_LEGACY,
        "leaf_partial": _fp2("c0"),
        "leaf_rootbeta": _fp2("c1"),
        "leaf_untrusted": _fp2("c2"),
        "inter": INTER_SHARED,
        "inter_revoked": INTER_REVOKED,
        "inter_strict": INTER_STRICT,
        "alpha": ROOT_ALPHA,
        "gamma": ROOT_GAMMA,
        "beta": ROOT_BETA,
        "strict": ROOT_STRICT,
        "legacy": ROOT_LEGACY,
        "partial": ROOT_PARTIAL,
    }
    cross_paths = [
        [p["leaf_main"], p["inter"], p["alpha"]],
        [p["leaf_main"], p["inter"], p["gamma"]],
        [p["leaf_main"], p["inter"], p["beta"]],
    ]
    revoked_leaf_paths = [
        [p["leaf_revoked"], p["inter"], p["alpha"]],
        [p["leaf_revoked"], p["inter"], p["gamma"]],
        [p["leaf_revoked"], p["inter"], p["beta"]],
    ]
    expired_leaf_paths = [
        [p["leaf_expired"], p["inter"], p["alpha"]],
        [p["leaf_expired"], p["inter"], p["gamma"]],
        [p["leaf_expired"], p["inter"], p["beta"]],
    ]
    bad_sig_paths = [
        [p["leaf_bad_sig"], p["inter"], p["alpha"]],
        [p["leaf_bad_sig"], p["inter"], p["gamma"]],
        [p["leaf_bad_sig"], p["inter"], p["beta"]],
    ]
    return {
        "format": "certpath.truth",
        "version": 1,
        "seed": seed,
        "observation_clock_ms": clock,
        "path_length_metric": "intermediate_certificates",
        "chains": {
            "chain-cross": {
                "conclusion": "trusted",
                "candidate_paths": cross_paths,
                "selected": [p["leaf_main"], p["inter"], p["beta"]],
                "trust_generation": 2,
                "required_codes": ["E_ROOT_UNTRUSTED", "E_ROOT_REVOKED"],
                "truncated": False,
            },
            "chain-limited": {
                "conclusion": "trusted",
                "candidate_paths": cross_paths[:2],
                "candidates_total": 3,
                "selected": [p["leaf_main"], p["inter"], p["beta"]],
                "trust_generation": 2,
                "truncated": True,
                "truncated_count": 1,
                "truncated_has_valid": True,
            },
            "chain-direct-beta": {
                "conclusion": "trusted",
                "candidate_paths": [[p["leaf_rootbeta"], p["beta"]]],
                "selected": [p["leaf_rootbeta"], p["beta"]],
                "trust_generation": 2,
            },
            "chain-legacy": {
                "conclusion": "trusted",
                "candidate_paths": [[p["leaf_legacy"], p["legacy"]]],
                "selected": [p["leaf_legacy"], p["legacy"]],
                "trust_generation": 1,
            },
            "chain-partial": {
                "conclusion": "partial",
                "candidate_paths": [[p["leaf_partial"], p["partial"]]],
                "selected": [p["leaf_partial"], p["partial"]],
                "trust_generation": 2,
                "required_codes": ["E_TRUST_CONFLICT"],
                "required_view_conflicts": ["path_length"],
            },
            "chain-strict": {
                "conclusion": "untrusted",
                "candidate_paths": [[p["leaf_strict"], p["inter_strict"], p["strict"]]],
                "selected": None,
                "required_codes": ["E_PATH_LENGTH"],
            },
            "chain-revoked-leaf": {
                "conclusion": "untrusted",
                "candidate_paths": revoked_leaf_paths,
                "selected": None,
                "required_codes": ["E_LEAF_REVOKED"],
            },
            "chain-revoked-intermediate": {
                "conclusion": "untrusted",
                "candidate_paths": [
                    [p["leaf_through_revoked"], p["inter_revoked"], p["beta"]]
                ],
                "selected": None,
                "required_codes": ["E_INTERMEDIATE_REVOKED"],
            },
            "chain-expired-leaf": {
                "conclusion": "untrusted",
                "candidate_paths": expired_leaf_paths,
                "selected": None,
                "required_codes": ["E_CERT_EXPIRED"],
            },
            "chain-bad-signature": {
                "conclusion": "untrusted",
                "candidate_paths": bad_sig_paths,
                "selected": None,
                "required_codes": ["E_SIGNATURE_MISMATCH"],
            },
            "chain-root-untrusted": {
                "conclusion": "untrusted",
                "candidate_paths": [[p["leaf_untrusted"], p["gamma"]]],
                "selected": None,
                "required_codes": ["E_ROOT_REVOKED", "E_ROOT_UNTRUSTED"],
            },
            "chain-issuer-break": {
                "conclusion": "untrusted",
                "candidate_paths": cross_paths,
                "selected": None,
                "required_codes": ["E_ISSUER_BREAK"],
            },
            "chain-missing-intermediate": {
                "conclusion": "untrusted",
                "candidate_paths": [],
                "selected": None,
                "required_codes": ["E_MISSING_INTERMEDIATE"],
            },
        },
        "expected_conflicts": [
            {"fingerprint": ROOT_ALPHA, "field": "trusted"},
            {"fingerprint": ROOT_ALPHA, "field": "path_length"},
            {"fingerprint": ROOT_PARTIAL, "field": "path_length"},
        ],
    }


def generate_sample(
    out_dir: str | Path,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Generate all sample and truth files below out_dir."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    clock = DEFAULT_CLOCK_MS
    rng = random.Random(seed)

    certificates = _certificates(clock)
    chains = _chains()
    rng.shuffle(chains)
    v1 = _v1(clock)
    shards = _v2_records(clock)
    rng.shuffle(shards)
    for records in shards:
        rng.shuffle(records)

    manifest = {
        "format": "certpath.sample",
        "version": 1,
        "seed": seed,
        "observation_clock_ms": clock,
        "path_length_metric": "intermediate_certificates",
        "files": {
            "certificates": "certificates.json",
            "chains": "chains.json",
            "v1": "snapshots/v1/trust_v1.json",
            "v2": "snapshots/v2",
        },
    }
    _write_json(out / "manifest.json", manifest)
    _write_json(
        out / "certificates.json",
        {"format": "certpath.certificates", "version": 1, "certificates": certificates},
    )
    _write_json(
        out / "chains.json",
        {"format": "certpath.chains", "version": 1, "chains": chains},
    )
    _write_json(out / "snapshots/v1/trust_v1.json", v1)
    _write_v2(out / "snapshots/v2", clock, shards)

    marker_paths = {
        "manifest.json": out / "manifest.json",
        "certificates.json": out / "certificates.json",
        "chains.json": out / "chains.json",
        "snapshots/v1/trust_v1.json": out / "snapshots/v1/trust_v1.json",
        "snapshots/v2/meta.db": out / "snapshots/v2/meta.db",
    }
    for shard_path in sorted((out / "snapshots/v2/shards").glob("*.json")):
        marker_paths[
            str(shard_path.relative_to(out)).replace("\\", "/")
        ] = shard_path
    marker = make_sync_marker(marker_paths)
    marker["seed"] = seed
    _write_json(out / "sync.marker.json", marker)

    truth = _truth(clock, seed)
    truth["sync_marker"] = marker["sync_marker"]
    truth["sample_files"] = marker["files"]
    _write_json(out / "truth.json", truth)

    return {
        "seed": seed,
        "out_dir": str(out),
        "observation_clock_ms": clock,
        "certificates": len(certificates),
        "chains": len(chains),
        "shards": len(shards),
        "sync_marker": marker["sync_marker"],
    }
