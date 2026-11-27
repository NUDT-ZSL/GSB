"""End-to-end orchestration across the three historical schema versions."""

from __future__ import annotations

from dataclasses import asdict

from .engine import GatewayEngine
from .generator import build_sample
from .reference_bloom import ReferenceAuditor
from .schemas import (
    CANONICAL_SEEDS,
    Event,
    ProjectSpec,
    SCHEMA_VERSIONS,
    cross_version_diff_table,
    encode_key,
    normalize_event,
    normalize_project,
    raw_project,
)
from .statistics import (
    generation_statistics,
    lab_acceleration,
    query_records_as_dicts,
    recompute_totals,
    residual_scan,
)


def canonical_spec(sample: dict) -> ProjectSpec:
    raw_spec = sample["spec"]
    return ProjectSpec(
        version="v1",
        project="campus-object-pool",
        bit_array_size=raw_spec["bit_array_size"],
        hash_count=raw_spec["hash_count"],
        max_inserts_per_generation=raw_spec["max_inserts_per_generation"],
        seeds=CANONICAL_SEEDS,
    )


def render_event(version: str, event: Event) -> dict:
    """Render a canonical event in one schema's native field vocabulary."""

    from .schemas import OP_VOCAB, PROFILES

    profile = PROFILES[version]
    inverse_ops = {canonical: raw for raw, canonical in OP_VOCAB[version].items()}
    return {
        profile.key_field: encode_key(version, event.key),
        profile.op_field: inverse_ops[event.op],
        profile.time_field: event.logical_time,
    }


def version_events(version: str, events: list[Event]) -> list[Event]:
    """Serialize to the version's native record, then normalize back."""

    normalized: list[Event] = []
    for event in events:
        raw = render_event(version, event)
        back = normalize_event(version, raw)
        if back.key != event.key or back.op != event.op or back.logical_time != event.logical_time:
            raise AssertionError(f"{version} event normalization round trip failed at tick {event.logical_time}")
        normalized.append(back)
    return normalized


def run_version(version: str, spec: ProjectSpec, events: list[Event]) -> dict:
    raw = raw_project(version, spec)
    normalized_spec = normalize_project(version, raw)
    normalized_events = version_events(version, events)
    auditor = ReferenceAuditor(normalized_spec)
    engine = GatewayEngine(normalized_spec, auditor=auditor)
    result = engine.run(normalized_events)
    return {
        "version": version,
        "spec": normalized_spec,
        "engine": engine,
        "result": result,
        "auditor": auditor,
    }


def _verdict_signature(result) -> list[tuple]:
    return [
        (
            q.query_seq,
            q.logical_time,
            q.generation_id,
            q.key,
            q.decision,
            q.filter_verdict,
            q.backend_exists,
            q.false_positive,
            q.via_tombstone,
        )
        for q in result.queries
    ]


def cross_version_check(runs: dict) -> dict:
    """Assert identical verdicts/stats across versions and collect diffs."""

    baseline = runs["v1"]["result"]
    baseline_sig = _verdict_signature(baseline)
    baseline_gen = generation_statistics(baseline)
    mismatches: list[dict] = []
    for version in SCHEMA_VERSIONS[1:]:
        target = runs[version]["result"]
        target_sig = _verdict_signature(target)
        if target_sig != baseline_sig:
            for index, (left, right) in enumerate(zip(baseline_sig, target_sig)):
                if left != right:
                    mismatches.append(
                        {
                            "version": version,
                            "first_divergent_query_seq": index + 1,
                            "v1": repr(left),
                            version: repr(right),
                        }
                    )
                    break
        if generation_statistics(target) != baseline_gen:
            mismatches.append({"version": version, "area": "generation_statistics"})
    return {
        "identical": not mismatches,
        "mismatches": mismatches,
        "diff_table": cross_version_diff_table(),
        "compared_query_count": len(baseline_sig),
    }


def build_report(use_reference: bool = True) -> dict:
    """Build the complete report payload for all schema versions."""

    sample = build_sample()
    spec = canonical_spec(sample)
    runs: dict[str, dict] = {}
    for version in SCHEMA_VERSIONS:
        # A ReferenceMismatch propagates to the CLI, which exits with code 2
        # and prints the first inconsistent query, key, positions and states.
        runs[version] = run_version(version, spec, sample["events"])

    primary = runs["v1"]
    engine = primary["engine"]
    result = primary["result"]
    backend_live = engine.backend.snapshot()

    # Residual scan pools the generated negative space plus deleted keys.
    residual_candidates = sample["negative_pool"] + sample["delete_targets"]
    residual = residual_scan(result, engine, residual_candidates, backend_live)

    report = {
        "project": spec.project,
        "generator_seed": sample["seed"],
        "canonical_spec": asdict(spec),
        "event_counts": {
            "total_events": len(sample["events"]),
            "inserts": sum(1 for e in sample["events"] if e.op == "insert"),
            "deletes": sum(1 for e in sample["events"] if e.op == "delete"),
            "queries": sum(1 for e in sample["events"] if e.op == "query"),
        },
        "generations": generation_statistics(result),
        "rotations": [asdict(r) for r in result.rotations],
        "deletes": [
            {
                "logical_time": d.logical_time,
                "generation_id": d.generation_id,
                "key": d.key.decode("ascii"),
                "positions": list(d.positions),
                "counters_after": d.counters_after,
            }
            for d in result.deletes
        ],
        "per_query": query_records_as_dicts(result),
        "totals": recompute_totals(result.queries),
        "lab_acceleration": lab_acceleration(result, tuple(sample["lab_prefixes"])),
        "residual_scan": residual,
        "cross_version": cross_version_check(runs),
        "reference_audit": {
            "implementation": "nqg.reference_bloom.ReferenceCountingBitArray",
            "shares_kernel_bit_path": False,
            "audited_generations": list(engine.auditor.generation_arrays.keys()),
            "mismatch": None,
        },
        "backend": {
            "final_live_object_count": result.backend_final_size,
            "total_backend_lookups": engine.backend.lookups,
        },
    }
    return report
