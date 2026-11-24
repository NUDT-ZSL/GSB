from drift_engine import adjudication, codes
from drift_engine.parsers import parse_v1, parse_v2


def _all_evidence(workspace, spec):
    v1 = parse_v1(str(workspace / "v1" / "snapshot-v1.json"), spec)
    v2 = parse_v2(str(workspace / "v2" / "shards"),
                  str(workspace / "v2" / "metadata.db"), spec)
    return v1.accepted + v2.accepted


def test_cross_generation_conflict_is_surfaced(sample_workspace):
    from drift_engine.spec import BaselineSpec

    spec = BaselineSpec.load_file(str(sample_workspace / "spec.json"))
    evidence = _all_evidence(sample_workspace, spec)
    result = adjudication.adjudicate_all(evidence, spec, window_seconds=60,
                                         decision_clock_ms=samplegen_clock(sample_workspace))
    conflict_keys = {(item["host"], item["key"]) for item in result["conflicts"]}
    assert ("host-a", "net.keepalive") in conflict_keys
    conflict = next(item for item in result["conflicts"]
                    if item["host"] == "host-a" and item["key"] == "net.keepalive")
    assert conflict["code"] == codes.E_CONFLICT
    assert conflict["left"]["generation"] == 1
    assert conflict["right"]["generation"] == 2


def samplegen_clock(workspace):
    import json
    spec = json.loads((workspace / "spec.json").read_text(encoding="utf-8"))
    return spec["observation_clock_ms"] + 120_000


def test_window_quantization_ambiguous(sample_workspace):
    from drift_engine.spec import BaselineSpec

    spec = BaselineSpec.load_file(str(sample_workspace / "spec.json"))
    evidence = _all_evidence(sample_workspace, spec)
    # edge.buffer first v1 crossing at clock+8000 with a 1000ms quantum;
    # pick a decision clock so the +-999ms band straddles a 12s window.
    decision_clock = spec.observation_clock_ms + 20_500
    result = adjudication.adjudicate_all(evidence, spec, window_seconds=12,
                                         decision_clock_ms=decision_clock)
    target = next(item for item in result["decisions"]
                  if item["host"] == "host-c" and item["key"] == "edge.buffer")
    assert target["verdict"] == codes.D_AMBIGUOUS
    assert target["code"] == codes.E_WINDOW_AMBIGUOUS
    band = target["quantization_band_ms"]
    assert band["quantum_ms"] == 1000
    assert target["margin_over_ms"] is not None
    assert target["margin_inside_ms"] is not None


def test_frozen_never_immediate_and_tombstone_revoked(sample_workspace):
    from drift_engine.spec import BaselineSpec

    spec = BaselineSpec.load_file(str(sample_workspace / "spec.json"))
    evidence = _all_evidence(sample_workspace, spec)
    result = adjudication.adjudicate_all(
        evidence, spec, window_seconds=1,
        decision_clock_ms=spec.observation_clock_ms + 900_000,
    )
    frozen = next(item for item in result["decisions"]
                  if item["host"] == "host-a" and item["key"] == "auth.mode")
    assert frozen["verdict"] == codes.D_REVOKED  # tombstone covers earlier drift

    # freeze directly: craft a non-tombstoned frozen drift
    from drift_engine.parsers import Evidence
    frozen_evidence = [Evidence(
        snapshot_id="x", generation=2, record_index=1, host="host-a",
        canonical_key="auth.mode", observed_ms=spec.observation_clock_ms,
        algebra=9, normalized={"type": "enum", "value": "LEGACY"},
        digest="d", tombstone=False, drift=True, severity="HIGH", time_quantum_ms=0,
    )]
    result = adjudication.adjudicate_all(
        frozen_evidence, spec, window_seconds=1,
        decision_clock_ms=spec.observation_clock_ms + 900_000,
    )
    decision = result["decisions"][0]
    assert decision["verdict"] == codes.D_FROZEN


def test_merge_order_independence(sample_workspace):
    from drift_engine.spec import BaselineSpec
    from drift_engine.merge import merge_view

    spec = BaselineSpec.load_file(str(sample_workspace / "spec.json"))
    evidence = _all_evidence(sample_workspace, spec)
    first = merge_view(evidence, spec)
    second = merge_view(list(reversed(evidence)), spec)
    assert first["entries"] == second["entries"]
    assert first["conflicts"] == second["conflicts"]
