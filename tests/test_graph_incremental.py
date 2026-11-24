from __future__ import annotations

from supply_chain_audit.codes import E_DEP_CYCLE
from supply_chain_audit.engine import AuditEngine, compare_views
from supply_chain_audit.parsing import load_dependencies, load_snapshot
from supply_chain_audit.service import build_seed_engine


def test_self_loop_and_arbitrary_cycle_are_listed(sample_paths, clock):
    cycle_dependencies = load_dependencies(sample_paths["cycle_dependencies"])
    engine = AuditEngine(clock=clock, dependencies=cycle_dependencies)
    cycles = engine.view["cycles"]
    assert len(cycles) == 2
    self_cycle = next(item for item in cycles if item["artifact_key"] == "self/loop@1.0.0")
    three_cycle = next(item for item in cycles if item["length"] == 3)
    assert self_cycle["cycle"] == ["self/loop@1.0.0", "self/loop@1.0.0"]
    assert three_cycle["code"] == E_DEP_CYCLE
    assert three_cycle["cycle"][0] == three_cycle["cycle"][-1]
    assert set(three_cycle["cycle"][:-1]) == {"cycle/a@1.0.0", "cycle/b@1.0.0", "cycle/c@1.0.0"}


def test_reverse_closure_levels_and_chains(engine):
    crypto = engine.view["closure_sets"]["security/crypto@3.0.1"]
    assert {
        "security/crypto@3.0.1",
        "net/tls@4.2.0",
        "service/api@5.0.0",
        "app/portal@6.1.0",
        "cli/auditctl@2.4.0",
        "service/gateway@1.2.0",
        "batch/worker@1.0.0",
    }.issubset(set(crypto))

    artifacts = engine.view["artifacts"]
    tls_entry = next(e for e in artifacts["net/tls@4.2.0"]["invalidations"] if e["source_key"] == "security/crypto@3.0.1")
    portal_entry = next(e for e in artifacts["app/portal@6.1.0"]["invalidations"] if e["source_key"] == "security/crypto@3.0.1")
    worker_token = next(e for e in artifacts["batch/worker@1.0.0"]["invalidations"] if e["source_key"] == "legacy/token@0.9.3")
    assert tls_entry["level"] == 1
    assert portal_entry["level"] == 2
    assert portal_entry["chain"] == ["app/portal@6.1.0", "net/tls@4.2.0", "security/crypto@3.0.1"]
    assert worker_token["level"] == 2
    assert worker_token["chain"] == ["batch/worker@1.0.0", "service/gateway@1.2.0", "legacy/token@0.9.3"]


def test_incremental_update_matches_full_recompute(sample_paths, clock):
    engine = build_seed_engine(sample_paths["root"])
    update = load_snapshot(sample_paths["update_batch"], clock)
    update_outcome = engine.apply_parse_result(update)
    assert update_outcome["status"] == "applied"
    assert update_outcome["equivalent_to_full_recompute"] is True
    assert "platform/codec@2.1.0" in update_outcome["rejudged_keys"]
    assert "foundation/core@1.4.0" not in update_outcome["rejudged_keys"]
    assert engine.view["artifacts"]["platform/codec@2.1.0"]["status"] == "invalid"

    revocation = load_revocation(sample_paths)
    revoke_outcome = engine.apply_revocation_batch(revocation)
    assert revoke_outcome["equivalent_to_full_recompute"] is True
    assert "infra/log@1.8.2" in revoke_outcome["rejudged_keys"]
    assert "foundation/core@1.4.0" not in revoke_outcome["rejudged_keys"]

    full_engine = build_seed_engine(
        sample_paths["root"], include_update=True, include_revocation=True
    )
    assert compare_views(engine.view, full_engine.view) is None


def load_revocation(sample_paths):
    from supply_chain_audit.parsing import load_revocation_batch

    return load_revocation_batch(sample_paths["revocation_batch"])


def test_divergence_detector_reports_first_key_and_field(engine):
    divergent = engine.view
    divergent["artifacts"]["infra/log@1.8.2"]["status"] = "valid"
    full = engine.build_full_view()
    divergence = compare_views(divergent, full)
    assert divergence["code"] == "E_RECOMPUTE_DIVERGE"
    assert divergence["artifact_key"] == "infra/log@1.8.2"
    assert divergence["field_name"] == "status"


def test_revoked_tombstone_is_present(sample_paths):
    engine = build_seed_engine(sample_paths["root"])
    token = engine.view["artifacts"]["legacy/token@0.9.3"]
    assert token["status"] == "revoked"
    assert token["tombstoned"] is True
    assert any(item["reason"] == "v2_tombstone" for item in token["revocations"])
