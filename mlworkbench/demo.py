"""Acceptance demo: deterministic samples, incremental commits, diagnostics.

Runs the full acceptance scenario and prints a diagnostic report:
1. full run on deterministic samples
2. incremental annotation fix; verify metrics == fresh full recompute
3. label-set version change for one image; verify equivalence
4. projection replacement; verify equivalence
5. snapshot rollback attempt -> E_SNAPSHOT_ROLLBACK
6. self-checks
"""
import json
import tempfile

from .config import ProjectionParams, WorkbenchConfig
from .engine import Engine, metrics_equal
from .errors import BusinessError
from .graph import topo_order
from .registry import default_labelsets, default_projection


def _check_equal(engine, report, label, tol):
    fresh = engine.full_recompute_fresh()
    ok, paths = metrics_equal(report.metrics, fresh.metrics, tol)
    print(f"  [{label}] incremental == fresh full recompute: "
          f"{'OK' if ok else 'MISMATCH ' + str(paths)}")
    return ok


def run_demo(root=None, seeds=range(1, 13)):
    root = root or tempfile.mkdtemp(prefix="mlwb-")
    cfg = WorkbenchConfig()
    engine = Engine(root, cfg, list(seeds), default_labelsets(), default_projection())
    ok = True

    print("== topology ==")
    print("  " + " -> ".join(topo_order(cfg.edges)))

    print("== 1. full run (cold cache) ==")
    base = engine.run_full()
    print(f"  recompute={len(base.recompute)} reuse={len(base.reuse)} "
          f"metrics_status={base.metrics_status}")
    print(f"  micro F1={base.metrics['micro']['f1']:.6f} "
          f"macro F1={base.metrics['macro']['f1']:.6f} "
          f"hamming={base.metrics['hamming_loss']:.6f}")

    print("== 2. incremental annotation fix on img-0003 ==")
    ann = engine.annotations["img-0003"]
    print(f"  old: {ann}")
    new_labels = set(ann["labels"]) ^ {"plant"}  # flip one label
    rep2 = engine.fix_annotation("img-0003", new_labels, set(ann["uncertain"]))
    print(f"  affected: {json.dumps(rep2.affected, sort_keys=True)}")
    print(f"  recompute: {rep2.recompute}")
    print(f"  reuse: {len(rep2.reuse)} cells")
    ok &= _check_equal(engine, rep2, "annotation fix", cfg.metric_tol)

    print("== 3. label set v1 -> v2 on img-0005 ==")
    rep3 = engine.change_labelset("img-0005", 2)
    print(f"  affected: {json.dumps(rep3.affected, sort_keys=True)}")
    print(f"  recompute: {rep3.recompute}")
    print(f"  reuse: {len(rep3.reuse)} cells")
    ok &= _check_equal(engine, rep3, "labelset change", cfg.metric_tol)

    print("== 4. projection replacement (v2, out_dim=32) ==")
    rep4 = engine.replace_projection(ProjectionParams(version=2, out_dim=32, seed=11))
    print(f"  recompute={len(rep4.recompute)} reuse={len(rep4.reuse)}")
    ok &= _check_equal(engine, rep4, "projection replace", cfg.metric_tol)

    print("== 5. snapshot rollback attempt ==")
    try:
        engine.replace_projection(ProjectionParams(version=1, out_dim=32, seed=11))
        print("  UNEXPECTED: rollback accepted")
        ok = False
    except BusinessError as err:
        print(f"  got expected {err.code}: {err.message}")

    print("== 6. self-checks ==")
    for r in engine.self_check():
        print(f"  [{'OK' if r['ok'] else 'FAIL'}] {r['check']}: {r['detail']}")

    print("== snapshots ==")
    print(json.dumps(engine.snapshot_versions(), indent=2, sort_keys=True))
    print("== recent business errors ==")
    for err in engine.recent_errors():
        print(f"  {err['code']}: {err['message']}")

    print(f"== final metrics == micro F1={rep4.metrics['micro']['f1']:.6f} "
          f"macro F1={rep4.metrics['macro']['f1']:.6f} "
          f"hamming={rep4.metrics['hamming_loss']:.6f}")
    print(f"RESULT: {'PASS' if ok else 'FAIL'}  (workspace: {root})")
    return ok, engine


def main():
    ok, _ = run_demo()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
