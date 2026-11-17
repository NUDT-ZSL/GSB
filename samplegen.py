"""Deterministic local sample generator for certtrust.

Everything is derived from a seed string; generating the same scenario
twice produces byte-identical output. No network, no wall clock: all
timestamps are constants anchored at T0.

Usage: python samplegen.py --out DIR [--seed SEED]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os

T0 = 1_700_000_000_000  # fixed observation anchor, ms since epoch
DEFAULT_SEED = "certtrust-seed-42"


def fingerprint(seed, name):
    return "sha256:" + hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()


def cert(seed, cid, issuer, not_before, not_after, revoked=False):
    return {
        "id": cid,
        "issuer": issuer,
        "fingerprint": fingerprint(seed, cid),
        "not_before_ms": not_before,
        "not_after_ms": not_after,
        "revoked": revoked,
    }


def build_certificates(seed):
    early, late = T0 - 1_000, T0 + 1_000_000
    return [
        cert(seed, "root-a", "root-a", early, late),
        cert(seed, "root-b", "root-b", early, late),
        cert(seed, "root-c", "root-c", early, late),
        cert(seed, "inter-1", "root-a", early, late),
        cert(seed, "inter-2", "root-b", early, late),
        cert(seed, "inter-rev", "root-a", early, late),
        cert(seed, "leaf-1", "inter-1", early, late),
        cert(seed, "leaf-2", "inter-2", early, late),
        cert(seed, "leaf-3", "inter-2", early, late),
        cert(seed, "leaf-r", "inter-rev", early, late),
        cert(seed, "leaf-exp", "inter-1", early, T0 + 50),
        cert(seed, "leaf-c", "root-c", early, late),
    ]


def build_chains():
    return [
        {"claim_id": "claim-good-a", "chain": ["leaf-1", "inter-1", "root-a"]},
        {"claim_id": "claim-good-b", "chain": ["leaf-2", "inter-2", "root-b"]},
        {"claim_id": "claim-rev-inter", "chain": ["leaf-r", "inter-rev", "root-a"]},
        {"claim_id": "claim-leaf-rev", "chain": ["leaf-3", "inter-2", "root-b"]},
        {"claim_id": "claim-exp", "chain": ["leaf-exp", "inter-1", "root-a"]},
        {"claim_id": "claim-untrusted-root", "chain": ["leaf-c", "root-c"]},
        {"claim_id": "claim-missing-inter", "chain": ["leaf-1", "root-a"]},
        {"claim_id": "claim-root-mismatch", "chain": ["leaf-2", "inter-2"]},
    ]


def snapshot(version, policy, observed_at, roots, revocations):
    return {
        "structure_version": 1,
        "snapshot_version": version,
        "policy_version": policy,
        "observed_at_ms": observed_at,
        "trusted_roots": roots,
        "revocations": [
            {"cert_id": cid, "revoked_at_ms": ts} for cid, ts in revocations
        ],
    }


def build_snapshots():
    return {
        "v1": snapshot(1, "1.0.0", T0, ["root-a"], []),
        "v2": snapshot(2, "1.0.0", T0 + 500, ["root-a", "root-b"],
                       [("inter-rev", T0 + 100)]),
        "v3": snapshot(3, "1.0.0", T0 + 900, ["root-a", "root-b"],
                       [("inter-rev", T0 + 100), ("leaf-3", T0 + 800)]),
        "v4": snapshot(4, "2.0.0", T0 + 950, ["root-b"],
                       [("inter-rev", T0 + 100), ("leaf-3", T0 + 800)]),
    }


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def generate(out_dir, seed=DEFAULT_SEED):
    """Generate all scenarios below out_dir; returns a manifest dict."""
    certs = build_certificates(seed)
    chains = build_chains()
    snaps = build_snapshots()

    basic = os.path.join(out_dir, "basic")
    write_json(os.path.join(basic, "certs.json"), certs)
    write_json(os.path.join(basic, "chains.json"), chains)
    for tag, snap in snaps.items():
        write_json(os.path.join(basic, f"snap_{tag}.json"), snap)

    ooo = os.path.join(out_dir, "out_of_order")
    write_json(os.path.join(ooo, "certs.json"), certs)
    write_json(os.path.join(ooo, "chains_a.json"), chains[:4])
    write_json(os.path.join(ooo, "chains_b.json"), chains[4:])
    write_json(os.path.join(ooo, "z_snap_first.json"), snaps["v1"])
    write_json(os.path.join(ooo, "m_snap_second.json"), snaps["v2"])
    write_json(os.path.join(ooo, "a_snap_third.json"), snaps["v3"])
    write_json(os.path.join(ooo, "events.json"), [
        {"step": 1, "snapshots": ["m_snap_second.json"],
         "chains": ["chains_a.json"]},
        {"step": 2, "snapshots": ["z_snap_first.json"],
         "chains": ["chains_b.json"]},
        {"step": 3, "snapshots": ["a_snap_third.json"], "chains": []},
    ])

    invalid = os.path.join(out_dir, "invalid")
    write_json(os.path.join(invalid, "certs_bad.json"), [
        cert(seed, "dup-cert", "dup-cert", T0, T0 + 10),
        cert(seed, "dup-cert", "dup-cert", T0, T0 + 10),
        dict(cert(seed, "bad-fp", "bad-fp", T0, T0 + 10), fingerprint="zzzz"),
        cert(seed, "bad-validity", "bad-validity", T0 + 10, T0),
        cert(seed, "orphan", "ghost-issuer", T0, T0 + 10),
        cert(seed, "ok-cert", "ok-cert", T0, T0 + 10),
        {"id": "no-issuer", "fingerprint": fingerprint(seed, "x"),
         "not_before_ms": T0, "not_after_ms": T0 + 10},
    ])
    write_json(os.path.join(invalid, "chains_bad.json"), [
        {"claim_id": "empty-chain", "chain": []},
        {"claim_id": "dup-chain", "chain": ["ok-cert", "ok-cert"]},
        {"claim_id": "missing-ref", "chain": ["ok-cert", "no-such-cert"]},
        {"claim_id": "not-a-list", "chain": "ok-cert"},
        {"claim_id": "ok-chain", "chain": ["ok-cert"]},
    ])

    unknown = os.path.join(out_dir, "unknown_policy")
    write_json(os.path.join(unknown, "snap_unknown_policy.json"),
               snapshot(1, "9.9.9", T0, ["root-a"], []))
    write_json(os.path.join(unknown, "snap_bad_policy_format.json"),
               snapshot(1, "1.0", T0, ["root-a"], []))
    write_json(os.path.join(unknown, "snap_bad_structure.json"),
               dict(snapshot(1, "1.0.0", T0, ["root-a"], []),
                    structure_version=99))
    write_json(os.path.join(unknown, "snap_no_obs_time.json"),
               dict(snapshot(1, "1.0.0", T0, ["root-a"], []),
                    observed_at_ms=None))

    interrupt = os.path.join(out_dir, "interrupt")
    _generate_interrupt_scenario(interrupt, certs, chains, snaps)

    return {
        "seed": seed,
        "t0": T0,
        "dirs": {
            "basic": basic,
            "out_of_order": ooo,
            "invalid": invalid,
            "unknown_policy": unknown,
            "interrupt": interrupt,
        },
    }


def _generate_interrupt_scenario(workdir, certs, chains, snaps):
    """A workdir as left behind by a killed process.

    Contains one complete checkpoint (anchor v2) plus half-written
    temporary files and a stale lock; only the complete checkpoint may
    ever be recovered.
    """
    from certtrust.checkpoint import write_checkpoint  # noqa: F401
    from certtrust.engine import Engine
    from certtrust.models import CertDesc, ChainClaim, Snapshot

    os.makedirs(workdir, exist_ok=True)
    engine = Engine(workdir)
    with engine.lock():
        engine.import_certs({c["id"]: CertDesc.from_dict(c) for c in certs})
        engine.import_claims({c["claim_id"]: ChainClaim.from_dict(c)
                              for c in chains})
        engine.import_snapshots([Snapshot.from_dict(snaps["v1"]),
                                 Snapshot.from_dict(snaps["v2"])])
        engine.recompute()
        engine.save()
    with open(engine.checkpoint_path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write('{"structure_version": 1, "payload_sha256": "incomplete')
    with open(os.path.join(workdir, "report.json.tmp"), "w",
              encoding="utf-8") as fh:
        fh.write('{"tool": "certtrust", "conclu')
    with open(os.path.join(engine.state_dir, "state.lock"), "w",
              encoding="ascii") as fh:
        fh.write("999999")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="deterministic sample generator")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--seed", default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    manifest = generate(args.out, args.seed)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
