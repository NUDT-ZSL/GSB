"""Parallel validation must produce exactly the serial result."""

import hashlib

from certtrust.engine import Engine
from certtrust.models import ChainClaim

from tests.conftest import load_basic


def _many_claims(n):
    claims = {}
    for i in range(n):
        claims[f"bulk-{i:04d}"] = ChainClaim(
            f"bulk-{i:04d}", ("leaf-1", "inter-1", "root-a"))
    return claims


def test_parallel_matches_serial(samples, tmp_path):
    certs, claims, snaps, _ = load_basic(samples)
    claims.update(_many_claims(200))
    results = []
    for jobs, tag in ((1, "serial"), (8, "parallel")):
        engine = Engine(str(tmp_path / tag))
        with engine.lock():
            engine.import_certs(certs)
            engine.import_claims(claims)
            engine.import_snapshots([snaps[2]])
            conclusions = engine.recompute(jobs=jobs)
            results.append([c.to_dict() for c in conclusions])
    assert results[0] == results[1]


def test_repeated_runs_are_deterministic(samples, tmp_path):
    certs, claims, snaps, _ = load_basic(samples)
    claims.update(_many_claims(50))
    digests = []
    for tag in ("x", "y"):
        engine = Engine(str(tmp_path / tag))
        with engine.lock():
            engine.import_certs(certs)
            engine.import_claims(claims)
            engine.import_snapshots([snaps[3]])
            conclusions = engine.recompute(jobs=4)
        blob = repr([c.to_dict() for c in conclusions]).encode()
        digests.append(hashlib.sha256(blob).hexdigest())
    assert digests[0] == digests[1]
