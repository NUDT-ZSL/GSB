"""Requirements 2-4: merge arbitration, TTL conversion ledger, ordered
min-fold, error-bound accumulation and precision-sensitive findings."""
import json

from conftest import T0, NOW_MS
from dnscache.merge import fold_min_expiry
from dnscache.service import op_status


def _entries(store):
    return store.load_entries(store.current_snapshot_version())


def _entry(store, domain, rtype, level):
    for entry in _entries(store):
        if (entry.domain, entry.rtype, entry.level) == (domain, rtype, level):
            return entry
    raise AssertionError(f"missing {domain} {rtype} {level}")


def test_merge_keeps_both_levels_and_sources(loaded_store):
    entries = _entries(loaded_store)
    # 6 v1 + 6 v2 entries, eta.test/A/local arbitrated down to one.
    assert len(entries) == 11
    local = _entry(loaded_store, "alpha.test", "A", "local")
    recursive = _entry(loaded_store, "alpha.test", "A", "recursive")
    assert local.source_version == "v1"
    assert recursive.source_version == "v2"


def test_arbitration_picks_newer_clock_and_records_rationale(loaded_store):
    eta = _entry(loaded_store, "eta.test", "A", "local")
    assert eta.source_version == "v2"  # v2 declared a newer logical clock
    assert eta.arbitration["rule"] == "logical_clock,generation,format_priority"
    assert eta.arbitration["winner_source"] == "v2"
    assert len(eta.arbitration["contenders"]) == 2
    # original representation preserved for tracing
    assert eta.original_repr["repr"] == "abs_ms"
    assert eta.original_repr["expiry_ms"] == NOW_MS + 95_000


def test_ttl_conversion_ledger_records(loaded_store):
    ledger = loaded_store.ledger_dump()
    conversions = [json.loads(r["detail_json"]) for r in ledger
                   if r["kind"] == "convert"]
    assert len(conversions) == 11
    v1_conv = next(c for c in conversions
                   if c["domain"] == "alpha.test" and c["level"] == "local")
    assert v1_conv["scale"] == 1000
    assert v1_conv["rounding"] == "down"
    assert v1_conv["discarded_sub_ms_us"] == 0
    assert v1_conv["error_bound_ms"] == 1000.0
    assert v1_conv["output_expiry_ms"] == NOW_MS + 60_000
    v2_conv = next(c for c in conversions
                   if c["domain"] == "alpha.test" and c["level"] == "recursive")
    assert v2_conv["scale"] == 1
    assert v2_conv["rounding"] == "exact"
    assert v2_conv["error_bound_ms"] == 0.0
    assert v2_conv["output_expiry_ms"] == NOW_MS + 60_500


def test_min_fold_order_and_comparison_records(loaded_store):
    ledger = loaded_store.ledger_dump()
    comparisons = [json.loads(r["detail_json"]) for r in ledger
                   if r["kind"] == "compare"]
    # alpha, beta (recursive vs local) and zeta (authoritative vs recursive)
    assert {(c["domain"], c["order_index"]) for c in comparisons} == {
        ("alpha.test", 0), ("beta.test", 0), ("zeta.test", 0)}
    alpha = next(c for c in comparisons if c["domain"] == "alpha.test")
    # fixed order: recursive folded before local; local wins (smaller expiry)
    assert alpha["left"]["level"] == "recursive"
    assert alpha["right"]["level"] == "local"
    assert alpha["winner_level"] == "local"
    assert alpha["diff_ms"] == 500
    assert alpha["combined_bound_ms"] == 1000.0
    assert alpha["reliable"] is False


def test_fold_min_expiry_is_ordered(loaded_store):
    zeta = [e for e in _entries(loaded_store) if e.domain == "zeta.test"]
    winner, records = fold_min_expiry(zeta)
    assert winner.level == "authoritative"  # first in fixed order, diff 0
    assert len(records) == 1
    assert records[0].diff_ms == 0
    assert records[0].reliable is False


def test_precision_sensitive_findings(loaded_store):
    findings = loaded_store.findings_all()
    by_domain = {f["domain"]: f for f in findings}
    assert set(by_domain) == {"alpha.test", "beta.test", "gamma.test",
                              "zeta.test"}
    alpha = by_domain["alpha.test"]
    assert alpha["decision_kind"] == "min_select"
    assert alpha["flip"]["would_flip"] is True
    assert alpha["flip"]["settled_by_ms_exact"] is True
    assert alpha["flip"]["conclusion"] == (
        "flip_possible_at_current_precision; "
        "ms_exact_representation_would_settle")
    # error source chain carries domain, conversions and comparison steps
    assert alpha["chain"]["domain"] == "alpha.test"
    assert {c["source_version"] for c in alpha["chain"]["conversions"]} == {
        "v1", "v2"}
    assert len(alpha["chain"]["comparisons"]) == 1
    gamma = by_domain["gamma.test"]
    assert gamma["decision_kind"] == "expiry"
    assert gamma["decision"] == "expired"
    assert gamma["remaining_ms"] == 0
    assert gamma["flip"]["conclusion"] == "flip_possible_even_at_ms_precision"
    zeta = by_domain["zeta.test"]
    assert zeta["flip"]["conclusion"] == "flip_possible_even_at_ms_precision"


def test_findings_reproducible_across_runs(tmp_path, loaded_store):
    # A second, identical load into a fresh store must produce the same
    # findings (deterministic given the same injected clock).
    from conftest import SAMPLES
    from dnscache.service import op_load
    from dnscache.store import Store
    fresh = Store(str(tmp_path / "fresh"))
    op_load(fresh, T0, 1000,
            v1_path=f"{SAMPLES}/v1_snapshot.json",
            v2_dir=f"{SAMPLES}/v2")
    assert fresh.findings_all() == loaded_store.findings_all()
    assert fresh.ledger_dump() == loaded_store.ledger_dump()


def test_status_after_load(loaded_store):
    status = op_status(loaded_store)
    assert status["current_snapshot_version"] == 2
    assert status["clock_us"] == T0
    assert status["generation_high_water"] == 2
    assert status["ledger_entries"] == 14
