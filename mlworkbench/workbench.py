"""Interactive local diagnostic workbench (CLI).

Commands: topology, run, status, scores, decide, metrics, details, fix,
labelset, projection, snapshots, errors, explain, selfcheck, report, quit.
"""
import cmd
import json
import shlex

from .config import ProjectionParams, WorkbenchConfig
from .engine import Engine
from .errors import BusinessError
from .graph import topo_order
from .registry import default_labelsets, default_projection


def build_engine(root="workspace", seeds=range(1, 13), config_dict=None):
    cfg = WorkbenchConfig.from_dict(config_dict or {})
    return Engine(root, cfg, list(seeds), default_labelsets(), default_projection())


class WorkbenchShell(cmd.Cmd):
    intro = "multi-label diagnostic workbench; type 'help' for commands"
    prompt = "wb> "

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def _guard(self, fn, *args):
        try:
            return fn(*args)
        except BusinessError as err:
            print(json.dumps(err.to_dict(), ensure_ascii=False, sort_keys=True))
            return None

    def do_topology(self, _):
        """Show DAG nodes and edges."""
        cfg = self.engine.cfg
        print("nodes:", " -> ".join(topo_order(cfg.edges)))
        for a, b in cfg.edges:
            print(f"  {a} -> {b}")

    def do_run(self, _):
        """Full run over all samples (cache-assisted)."""
        report = self._guard(self.engine.run_full)
        if report:
            print(f"recomputed {len(report.recompute)} cells, "
                  f"reused {len(report.reuse)} cells")

    def do_status(self, _):
        """Show per-node cache hit/recompute status of the last run."""
        report = self.engine.last_report
        if not report:
            print("no run yet")
            return
        for node, per_img in report.statuses.items():
            counts = {}
            for st in per_img.values():
                counts[st] = counts.get(st, 0) + 1
            print(f"  {node:11s} {counts}")
        print(f"  {'metrics':11s} {report.metrics_status}")
        if report.degraded:
            print("  degraded:", ", ".join(report.degraded))

    def do_scores(self, arg):
        """scores <image_id> -- per-label scores for one sample."""
        report = self.engine.last_report
        if not report:
            print("no run yet")
            return
        img = arg.strip()
        for label, score in sorted(report.scores.get(img, {}).items()):
            print(f"  {label:10s} {score:.6f}")

    def do_decide(self, arg):
        """decide <image_id> -- decision set for one sample (may be empty)."""
        report = self.engine.last_report
        if not report:
            print("no run yet")
            return
        print(report.decisions.get(arg.strip(), "unknown image"))

    def do_metrics(self, _):
        """Show the metrics report of the last run."""
        report = self.engine.last_report
        if not report:
            print("no run yet")
            return
        print(json.dumps(report.metrics, indent=2, sort_keys=True))

    def do_details(self, arg):
        """details <image_id> -- per-sample-per-label detail rows."""
        report = self.engine.last_report
        if not report:
            print("no run yet")
            return
        img = arg.strip()
        for row in report.details:
            if row["image"] == img:
                print(f"  {row['label']:10s} truth={row['truth']} "
                      f"unc={row['uncertain']} pred={row['pred']}")

    def do_fix(self, arg):
        """fix <image_id> <labels csv> <uncertain csv> -- annotation fix."""
        parts = shlex.split(arg)
        img, labels, uncertain = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
        to_set = lambda s: {x for x in s.split(",") if x}
        report = self._guard(self.engine.fix_annotation, img,
                             to_set(labels), to_set(uncertain))
        if report:
            print("recompute:", report.recompute)
            print(f"reuse: {len(report.reuse)} cells")

    def do_labelset(self, arg):
        """labelset <image_id> <version> -- switch label set version."""
        img, version = shlex.split(arg)
        report = self._guard(self.engine.change_labelset, img, int(version))
        if report:
            print("affected:", json.dumps(report.affected, sort_keys=True))
            print("recompute:", report.recompute)

    def do_projection(self, arg):
        """projection <version> <out_dim> <seed> -- replace projection params."""
        version, out_dim, seed = shlex.split(arg)
        params = ProjectionParams(int(version), int(out_dim), int(seed))
        report = self._guard(self.engine.replace_projection, params)
        if report:
            print(f"recomputed {len(report.recompute)} cells, "
                  f"reused {len(report.reuse)} cells")

    def do_snapshots(self, _):
        """Show snapshot versions and checksum prefixes."""
        print(json.dumps(self.engine.snapshot_versions(), indent=2, sort_keys=True))

    def do_errors(self, _):
        """Show recent business error codes."""
        for err in self.engine.recent_errors():
            print(f"  {err['code']}: {err['message']} {err['details']}")

    def do_explain(self, arg):
        """explain <cache_key> -- provenance and hit status of a cache key."""
        print(json.dumps(self.engine.explain_key(arg.strip()),
                         indent=2, sort_keys=True))

    def do_keys(self, _):
        """List cache keys of the last run (node:image -> key prefix)."""
        report = self.engine.last_report
        if not report:
            print("no run yet")
            return
        for cell, key in sorted(report.cache_keys.items()):
            print(f"  {cell:24s} {key[:16]}")

    def do_selfcheck(self, _):
        """Run cross-module consistency self-checks."""
        results = self._guard(self.engine.self_check)
        if results:
            for r in results:
                mark = "OK " if r["ok"] else "FAIL"
                print(f"  [{mark}] {r['check']}: {r['detail']}")

    def do_report(self, _):
        """Dump the full last run report as JSON."""
        report = self.engine.last_report
        if not report:
            print("no run yet")
            return
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))

    def do_quit(self, _):
        """Exit the workbench."""
        return True

    do_EOF = do_quit


def main():
    shell = WorkbenchShell(build_engine())
    shell.cmdloop()


if __name__ == "__main__":
    main()
