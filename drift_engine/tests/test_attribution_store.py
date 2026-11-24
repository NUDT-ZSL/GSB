import json
import os

import pytest
import sqlite3

from drift_engine import codes
from drift_engine.attribution import attribute
from drift_engine.samplegen import build_spec


def test_dependency_cycle_reports_keys(engine_factory):
    _, spec, _ = engine_factory()
    result = attribute(["queue.depth"], spec, "host-c")
    assert result["code"] == codes.E_DEP_CYCLE
    assert set(result["cycle"]) == {"queue.depth", "worker.count", "backlog.size"}


def test_primary_secondary_attribution_stable(engine_factory):
    _, spec, _ = engine_factory()
    first = attribute(["net.keepalive"], spec, "host-c")
    second = attribute(["net.keepalive"], spec, "host-c")
    assert first == second
    assert first["primary_causes"] == ["net.keepalive"]
    assert first["secondary_causes"] == [
        {"key": "edge.buffer", "propagated_via": ["net.keepalive"]}
    ]
    assert first["service_root_candidates"] == [
        "svc-edge:edge.buffer", "svc-edge:net.keepalive"
    ]


def _import(engine, workspace, batch_id, workers=1):
    manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    base = manifest["base_dir"]
    v1 = [os.path.join(base, "v1", "snapshot-v1.json")]
    v2 = [(os.path.join(base, "v2", "shards"), os.path.join(base, "v2", "metadata.db"))]
    return engine.import_batch(batch_id, v1, v2, window_seconds=60,
                               decision_clock_ms=1_700_000_120_000, workers=workers)


def test_incremental_matches_full_rebuild(sample_workspace, engine_factory):
    engine, spec, _ = engine_factory()
    report = _import(engine, sample_workspace, "batch-1")
    assert report["fatal_snapshots"] == []
    rebuild = engine.full_rebuild(60, 1_700_000_120_000)
    ledger = {(row["host"], row["config_key"]): row for row in engine.ledger_rows()}
    for decision in rebuild["decisions"]:
        row = ledger[(decision["host"], decision["key"])]
        assert row["verdict"] == decision["verdict"]
        assert row["code"] == decision["code"]
        assert row["first_drift_ms"] == decision["first_drift_ms"]
        assert row["authoritative_generation"] == decision["authoritative_generation"]
        assert row["authoritative_algebra"] == decision["authoritative_algebra"]


def test_concurrency_matches_serial(sample_workspace, engine_factory):
    serial_engine, _, _ = engine_factory()
    parallel_engine, _, _ = engine_factory()
    serial = _import(serial_engine, sample_workspace, "b", workers=1)
    parallel = _import(parallel_engine, sample_workspace, "b", workers=4)
    assert serial_engine.ledger_rows() == parallel_engine.ledger_rows()
    assert serial["conflicts"] == parallel["conflicts"]


def test_half_written_checkpoint_residue_detected(sample_workspace, engine_factory):
    engine, _, tmp_path = engine_factory()
    _import(engine, sample_workspace, "batch-1")
    healthy = engine.verify_checkpoint()
    assert healthy["code"] is None

    # Corrupt the checkpoint snapshot file after its atomic replace: the replay
    # must flag half-written residue instead of silently trusting the ledger.
    cp_path = healthy["checkpoint"]["checkpoint_path"]
    with open(cp_path, "r+b") as handle:
        handle.seek(0)
        handle.write(b"{'half-written': true")
    damaged = engine.verify_checkpoint()
    assert damaged["code"] == codes.E_CHECKPOINT_RESIDUE
    assert damaged["mismatches"]


def test_orphan_temp_file_does_not_corrupt_checkpoint(sample_workspace, engine_factory):
    engine, _, tmp_path = engine_factory()
    _import(engine, sample_workspace, "batch-1")
    healthy = engine.verify_checkpoint()
    assert healthy["code"] is None
    # A crashed atomic write leaves a sibling temp file; the committed checkpoint
    # must remain the sole recovery source and still replay cleanly.
    cp_dir = tmp_path / "cp-0"
    orphan = cp_dir / ".checkpoint-batch-1.json.tmp.999"
    orphan.write_text('{"half":', encoding="utf-8")
    replayed = engine.verify_checkpoint()
    assert replayed["code"] is None
    assert replayed["live_ledger_digest"] == healthy["live_ledger_digest"]


def test_incremental_second_batch_equiv_full_rebuild(sample_workspace, engine_factory, tmp_path):
    import hashlib
    import sqlite3

    from drift_engine import atomic
    from drift_engine.parsers import Evidence

    engine, spec, _ = engine_factory()
    _import(engine, sample_workspace, "batch-1")
    clock = spec.observation_clock_ms

    b2_dir = tmp_path / "b2" / "shards"
    b2_dir.mkdir(parents=True)
    normalized = {"type": "mask", "labels": ["HTTP", "SSH"]}
    digest = hashlib.sha256(
        __import__("json").dumps(
            {"host": "host-b", "key": "firewall.mask", "algebra": 5,
             "observed_ms": clock + 70000, "tombstone": False, "normalized": normalized},
            sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    record = {"host": "host-b", "key": "firewall.mask", "algebra": 5,
              "observed_ms": clock + 70000, "normalized": normalized,
              "tombstone": False, "drift": False, "severity": "LOW", "digest": digest}
    atomic.atomic_write_json(
        str(b2_dir / "shard.json"),
        {"shard_id": "shard.json", "records": {"host-b|firewall.mask|5": record}},
    )
    meta = tmp_path / "b2" / "meta.db"
    conn = sqlite3.connect(meta)
    conn.execute(
        "CREATE TABLE evidence_index (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT, "
        "key TEXT, algebra INTEGER, observed_ms INTEGER, tombstone INTEGER, digest TEXT, shard_id TEXT)"
    )
    conn.execute(
        "INSERT INTO evidence_index VALUES (NULL,?,?,?,?,?,?,?)",
        ("host-b", "firewall.mask", 5, clock + 70000, 0, digest, "shard.json"),
    )
    conn.commit()
    conn.close()

    before = {(r["host"], r["config_key"]): dict(r) for r in engine.ledger_rows()}
    report = engine.import_batch(
        "batch-2", [], [(str(b2_dir), str(meta))], 60, clock + 120000
    )
    after = {(r["host"], r["config_key"]): dict(r) for r in engine.ledger_rows()}
    affected = {tuple(pair) for pair in report["affected_pairs"]}
    assert affected == {("host-b", "firewall.mask")}
    assert all(before[key] == after[key] for key in before if key not in affected)

    # Reference: fresh engine ingesting both batches in one stream.
    ref_engine, _, _ = engine_factory()
    manifest = json.loads((sample_workspace / "manifest.json").read_text(encoding="utf-8"))
    base = manifest["base_dir"]
    ref_engine.import_batch(
        "all",
        [os.path.join(base, "v1", "snapshot-v1.json")],
        [(os.path.join(base, "v2", "shards"), os.path.join(base, "v2", "metadata.db")),
         (str(b2_dir), str(meta))],
        60, clock + 120000,
    )
    ref_rows = {(r["host"], r["config_key"]): r for r in ref_engine.ledger_rows()}
    for key, row in after.items():
        for field_name in ("verdict", "code", "first_drift_ms", "frozen",
                           "authoritative_generation", "authoritative_algebra", "row_digest"):
            assert row[field_name] == ref_rows[key][field_name]


def test_partial_failure_isolation_keeps_prior_ledger(sample_workspace, engine_factory):
    engine, _, _ = engine_factory()
    _import(engine, sample_workspace, "batch-1")
    snapshot_before = engine.ledger_rows()

    # A fatal snapshot contributes no evidence; all prior ledger rows stay stable.
    report = engine.import_batch(
        "batch-bad", [], [("/no/such/shards", "/no/such/meta.db")],
        60, spec_clock(sample_workspace),
    )
    assert report["fatal_snapshots"]
    assert engine.ledger_rows() == snapshot_before


def test_transaction_rollback_on_commit_failure(sample_workspace, engine_factory, monkeypatch):
    engine, spec, _ = engine_factory()
    _import(engine, sample_workspace, "batch-1")
    before = engine.ledger_rows()
    evidence_before = engine.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

    real_conn = engine.conn

    class BrokenConn:
        def __getattr__(self, name):
            return getattr(real_conn, name)
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.commit()  # raises OperationalError on success path
                return False
            real_conn.rollback()
            return False
        def commit(self):
            raise sqlite3.OperationalError("simulated forced commit failure")

    monkeypatch.setattr(engine, "conn", BrokenConn())
    manifest = json.loads((sample_workspace / "manifest.json").read_text(encoding="utf-8"))
    base = manifest["base_dir"]
    v1 = [os.path.join(base, "v1", "snapshot-v1.json")]
    v2 = [(os.path.join(base, "v2", "shards"), os.path.join(base, "v2", "metadata.db"))]
    with pytest.raises(sqlite3.OperationalError):
        engine.import_batch("batch-rollback", v1, v2, 60,
                            spec.observation_clock_ms + 120000)

    monkeypatch.undo()
    assert engine.ledger_rows() == before
    assert engine.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == evidence_before


def spec_clock(workspace):
    return json.loads((workspace / "spec.json").read_text(encoding="utf-8"))["observation_clock_ms"] + 120000


def test_version_change_forces_full_rebuild(engine_factory):
    engine, spec, _ = engine_factory(build_spec())
    engine._set_meta("merge_version", "0.9.0")
    notes = engine._version_rebuild_if_needed()
    assert notes and notes[0]["code"] == codes.E_VERSION_REBUILD
    assert engine._meta("merge_version") == spec.merge_version
