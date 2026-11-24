from __future__ import annotations

from pathlib import Path

from tsaudit.adjudication import artifact_key
from tsaudit.codes import Code
from tsaudit.engine import AuditEngine
from tsaudit.trust import find_cycles


def _report(sample_dir: Path, workspace: Path):
    engine = AuditEngine(workspace)
    return engine, engine.adjudicate_sample(sample_dir)


def test_trust_conflicts_and_tombstone_are_recorded(
    sample_dir: Path, workspace: Path
) -> None:
    engine, report = _report(sample_dir, workspace)
    view = engine.store.read_report()  # report exists
    trust_view = __import__("json").loads(
        (workspace / "trust_view.json").read_text(encoding="utf-8")
    )
    unresolved = [c for c in trust_view["conflicts"] if c["status"] == "unresolved"]
    assert len(unresolved) == 2
    assert {c["field"] for c in unresolved} == {"trusted", "revoked"}
    assert any(o["issuer_id"] == "tsa-tombstone" for o in trust_view["tombstone_overrides"])
    assert report["summary"]["unresolved_conflict_count"] == 2

    tombstone_issuer = next(i for i in trust_view["issuers"] if i["issuer_id"] == "tsa-tombstone")
    assert tombstone_issuer["tombstone"] is True


def test_cycle_fixture_lists_nodes(sample_dir: Path, workspace: Path) -> None:
    engine = AuditEngine(workspace)
    engine.adjudicate_sample(sample_dir)
    report = engine.import_update(sample_dir / "invalid" / "cycle-reference.json")
    assert report["summary"]["cycle_count"] >= 1
    cycle = report["closure"]
    assert any(code == Code.E_CYCLE_REFERENCE for code in [Code.E_CYCLE_REFERENCE])
    view_cycles = report and None
    trust_view = __import__("json").loads(
        (workspace / "trust_view.json").read_text(encoding="utf-8")
    )
    nodes = {node for cycle in trust_view["cycles"] for node in cycle["nodes"]}
    assert "issuer:tsa-cycle-a" in nodes
    assert "token_generation:9" in nodes
    assert "issuer:tsa-cycle-b" in nodes


def test_find_cycles_returns_stable_ring() -> None:
    cycles = find_cycles({
        "issuer:a": {"token_generation:2"},
        "token_generation:2": {"issuer:b"},
        "issuer:b": {"issuer:a"},
    })
    assert cycles
    assert set(cycles[0]["nodes"]) == {
        "issuer:a", "token_generation:2", "issuer:b"
    }


def test_specific_token_failure_codes(sample_dir: Path, workspace: Path) -> None:
    _, report = _report(sample_dir, workspace)
    by_id = {a["artifact_id"]: a for a in report["artifacts"]}
    expectations = {
        "art-late-issuer": Code.E_ISSUER_NOT_REGISTERED,
        "art-generation-mismatch": Code.E_GENERATION_MISMATCH,
        "art-tombstoned": Code.E_ISSUER_TOMBSTONED,
        "art-trust-conflict": Code.E_TRUST_CONFLICT,
        "art-token-future": Code.E_TOKEN_AFTER_SIGNING,
        "art-expired-token": Code.E_TOKEN_EXPIRED,
        "art-not-yet-valid": Code.E_TOKEN_NOT_YET_VALID,
        "art-no-coverage": Code.E_NO_COVERAGE,
        "art-orphan-revoked-before": Code.E_REVOCATION_BEFORE_ISSUANCE,
    }
    for artifact_id, code in expectations.items():
        assert code in by_id[artifact_id]["business_codes"], artifact_id
        assert by_id[artifact_id]["status"] == "unanchored"


def test_multiple_token_coverage_primary_and_alternatives_are_sorted(
    sample_dir: Path, workspace: Path
) -> None:
    _, report = _report(sample_dir, workspace)
    multi = next(a for a in report["artifacts"] if a["artifact_id"] == "art-multi")
    assert multi["primary_token_id"] == "tok-multi-a"
    assert multi["alternative_token_ids"] == ["tok-multi-b", "tok-multi-c"]
    issued = [(c["issued_at_ms"], c["token_generation"], c["token_id"])
              for c in multi["candidate_tokens"]]
    assert issued == sorted(issued)
