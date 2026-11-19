from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

from flash_attn_cache import (
    FlashAttentionEngine,
    QKVArrays,
    RuntimeConfig,
    dense_attention_reference,
)
from flash_attn_cache.samples import load_samples


def _max_delta(left: np.ndarray, right: np.ndarray) -> float:
    return float(np.max(np.abs(left - right)))


def _case_summary(result) -> dict:
    return {
        "case_id": result.case_id,
        "ok": result.ok,
        "nodes": len(result.node_outcomes),
        "recomputed_nodes": len(result.recomputed_nodes),
        "reused_nodes": len(result.reused_nodes),
        "output_shape": list(result.output.shape) if result.output is not None else None,
        "output_checksum": result.output_checksum,
        "errors": [issue.to_dict() for issue in result.errors],
        "degraded_categories": list(result.degradation_categories),
        "resident_bytes_estimate": result.resident_bytes_estimate,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--cache", default="cache/demo")
    parser.add_argument(
        "--output",
        default="artifacts/demo_report.json",
    )
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="do not clear the demo cache before the baseline run",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache)
    if not args.keep_cache and cache_dir.exists():
        shutil.rmtree(cache_dir)

    engine = FlashAttentionEngine(
        cache_dir,
        runtime=RuntimeConfig(
            parallel_nodes=4,
            max_parallel_nodes=4,
            max_resident_bytes=64 * 1024 * 1024,
            max_cache_entries=10_000,
        ),
    )
    samples = load_samples(args.samples)
    baseline_results = {}
    report: dict = {
        "environment": {
            "numpy": np.__version__,
            "python": __import__("sys").version.splitlines()[0],
        },
        "cache_dir": str(cache_dir),
        "baseline": [],
        "incremental_replacement": None,
        "incremental_growth": None,
        "cache_diagnostics": None,
    }

    for case, arrays in samples:
        result = engine.compute(case, arrays)
        baseline_results[case.case_id] = result
        summary = _case_summary(result)
        if result.ok:
            reference = dense_attention_reference(case, arrays)
            summary["dense_reference_max_abs_delta"] = _max_delta(
                result.output,
                reference,
            )
            check = engine.self_check(case, arrays, result)
            summary["self_check_ok"] = check.ok
            summary["self_check_issues"] = [
                issue.to_dict() for issue in check.issues
            ]
        report["baseline"].append(summary)

    replacement_case, replacement_arrays = samples[0]
    updated_case = replacement_case.replace(kv_revision=(1, 0, 0))
    k = replacement_arrays.k.copy()
    v = replacement_arrays.v.copy()
    k[:, 0:replacement_case.block_size, :] += 0.2
    v[:, 0:replacement_case.block_size, :] -= 0.1
    updated_arrays = QKVArrays(replacement_arrays.q.copy(), k, v)
    incremental = engine.incremental_update(
        replacement_case,
        replacement_arrays,
        updated_case,
        updated_arrays,
        baseline_result=baseline_results[replacement_case.case_id],
    )
    full = engine.compute(
        updated_case,
        updated_arrays,
        force_recompute=True,
    )
    reference = dense_attention_reference(updated_case, updated_arrays)
    report["incremental_replacement"] = {
        "case_id": updated_case.case_id,
        "ok": incremental.ok,
        "changed_kv_blocks": list(incremental.changed_kv_blocks),
        "changed_q_blocks": list(incremental.changed_q_blocks),
        "affected_nodes": len(incremental.affected_nodes),
        "recomputed_nodes": list(incremental.recomputed_nodes),
        "reused_nodes": list(incremental.reused_nodes),
        "recomputed_count": len(incremental.recomputed_nodes),
        "reused_count": len(incremental.reused_nodes),
        "sets_disjoint": not (
            set(incremental.recomputed_nodes)
            & set(incremental.reused_nodes)
        ),
        "affected_covered": set(incremental.affected_nodes).issubset(
            set(incremental.recomputed_nodes) | set(incremental.reused_nodes)
        ),
        "incremental_vs_full_max_abs_delta": (
            _max_delta(incremental.attention.output, full.output)
            if incremental.attention.output is not None and full.output is not None
            else None
        ),
        "incremental_vs_dense_max_abs_delta": (
            _max_delta(incremental.attention.output, reference)
            if incremental.attention.output is not None
            else None
        ),
        "self_check": (
            incremental.self_check.to_dict()
            if incremental.self_check is not None
            else None
        ),
        "errors": [
            issue.to_dict() for issue in incremental.attention.errors
        ],
    }

    growth_case, growth_arrays = samples[1]
    rng = np.random.default_rng(991)
    extra = 4
    q2 = np.concatenate(
        [
            growth_arrays.q,
            rng.standard_normal(
                (growth_case.n_heads, extra, growth_case.head_dim),
                dtype=np.dtype(growth_case.dtype),
            ),
        ],
        axis=1,
    )
    k2 = np.concatenate(
        [
            growth_arrays.k,
            rng.standard_normal(
                (growth_case.n_heads, extra, growth_case.head_dim),
                dtype=np.dtype(growth_case.dtype),
            ),
        ],
        axis=1,
    )
    v2 = np.concatenate(
        [
            growth_arrays.v,
            rng.standard_normal(
                (growth_case.n_heads, extra, growth_case.head_dim),
                dtype=np.dtype(growth_case.dtype),
            ),
        ],
        axis=1,
    )
    growth_updated_case = growth_case.replace(
        seq_len=growth_case.seq_len + extra,
        kv_revision=(0, 0, 1),
    )
    growth_updated_arrays = QKVArrays(q2, k2, v2)
    growth_incremental = engine.incremental_update(
        growth_case,
        growth_arrays,
        growth_updated_case,
        growth_updated_arrays,
        baseline_result=baseline_results[growth_case.case_id],
    )
    growth_full = engine.compute(
        growth_updated_case,
        growth_updated_arrays,
        force_recompute=True,
    )
    growth_reference = dense_attention_reference(
        growth_updated_case,
        growth_updated_arrays,
    )
    report["incremental_growth"] = {
        "case_id": growth_updated_case.case_id,
        "ok": growth_incremental.ok,
        "changed_kv_blocks": list(growth_incremental.changed_kv_blocks),
        "changed_q_blocks": list(growth_incremental.changed_q_blocks),
        "affected_nodes": len(growth_incremental.affected_nodes),
        "recomputed_count": len(growth_incremental.recomputed_nodes),
        "reused_count": len(growth_incremental.reused_nodes),
        "recomputed_tail_nodes": [
            node_id
            for node_id in growth_incremental.recomputed_nodes
            if node_id.startswith(("score:", "softmax:", "accum:", "finalize:"))
        ][-8:],
        "reused_examples": [
            node_id
            for node_id in growth_incremental.reused_nodes
            if node_id.startswith(("score:", "softmax:", "accum:"))
        ][:8],
        "sets_disjoint": not (
            set(growth_incremental.recomputed_nodes)
            & set(growth_incremental.reused_nodes)
        ),
        "incremental_vs_full_max_abs_delta": (
            _max_delta(
                growth_incremental.attention.output,
                growth_full.output,
            )
            if growth_incremental.attention.output is not None
            and growth_full.output is not None
            else None
        ),
        "incremental_vs_dense_max_abs_delta": (
            _max_delta(growth_incremental.attention.output, growth_reference)
            if growth_incremental.attention.output is not None
            else None
        ),
        "self_check": (
            growth_incremental.self_check.to_dict()
            if growth_incremental.self_check is not None
            else None
        ),
    }
    report["cache_diagnostics"] = engine.diagnose_cache().to_dict()

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(destination),
                "baseline_cases": len(report["baseline"]),
                "baseline_all_ok": all(
                    item["ok"] for item in report["baseline"]
                ),
                "replacement_recomputed": report[
                    "incremental_replacement"
                ]["recomputed_count"],
                "replacement_reused": report[
                    "incremental_replacement"
                ]["reused_count"],
                "growth_recomputed": report["incremental_growth"][
                    "recomputed_count"
                ],
                "growth_reused": report["incremental_growth"][
                    "reused_count"
                ],
                "cache_entries": report["cache_diagnostics"]["entries"],
                "cache_ok": report["cache_diagnostics"]["ok"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
