"""Engine: facade tying dataset, cache, snapshots, commits and self-checks."""
import json
import os
import tempfile

from .cache import Cache
from .dataset import deterministic_annotation, make_dataset
from .errors import (
    BusinessError, E_INCONSISTENT, E_RESOURCE_LIMIT,
)
from .graph import check_acyclic
from .incremental import (
    CommitLock, check_limits, plan_annotation_fix, plan_labelset_change,
    plan_projection_change,
)
from .metrics import verify_metrics
from .pipeline import Runner
from .snapshots import SnapshotStore


def metrics_equal(a, b, tol):
    paths = []

    def cmp(path, x, y):
        if abs(x - y) > tol:
            paths.append(path)

    for k in ("precision", "recall", "f1"):
        cmp(f"micro.{k}", a["micro"][k], b["micro"][k])
    cmp("macro.f1", a["macro"]["f1"], b["macro"]["f1"])
    cmp("hamming_loss", a["hamming_loss"], b["hamming_loss"])
    for label in a["per_label"]:
        for k in ("precision", "recall", "f1"):
            cmp(f"per_label.{label}.{k}", a["per_label"][label][k],
                b["per_label"][label][k])
    return not paths, paths


class Engine:
    def __init__(self, root, cfg, seeds, labelsets, projection):
        self.root = root
        self.cfg = cfg
        self.labelsets = dict(labelsets)          # version -> LabelSet
        self.projection = projection
        self.samples = make_dataset(seeds, cfg.image_size)
        self.cache = Cache(os.path.join(root, "cache"))
        snap = os.path.join(root, "snapshots")
        self.labelset_store = SnapshotStore(os.path.join(snap, "labelsets.json"), "score")
        self.projection_store = SnapshotStore(os.path.join(snap, "projection.json"), "features")
        self.metrics_store = SnapshotStore(os.path.join(snap, "metrics.json"), "metrics")
        self.state_dir = os.path.join(root, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.lock_path = os.path.join(root, "staging", "commit.lock")
        self.errors = []
        self.last_report = None
        self.last_plan = None
        self._init_state()

    # ------------------------------------------------------------ state I/O

    def _path(self, name):
        return os.path.join(self.state_dir, name)

    def _load_json(self, name, default):
        p = self._path(name)
        if os.path.exists(p):
            with open(p) as fh:
                return json.load(fh)
        return default

    def _save_json(self, name, obj):
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(obj, fh, sort_keys=True)
        os.replace(tmp, self._path(name))

    def _init_state(self):
        default_version = min(self.labelsets)
        self.assignments = self._load_json("assignments.json", {
            s.image_id: default_version for s in self.samples})
        stored = self._load_json("annotations.json", None)
        if stored is None:
            self.annotations = {
                s.image_id: self._gen_annotation(s.seed, self.assignments[s.image_id])
                for s in self.samples}
            self._save_json("annotations.json", self.annotations)
            self._save_json("assignments.json", self.assignments)
        else:
            self.annotations = stored
        self.meta = self._load_json("meta.json",
                                    {"metrics_seq": 0, "labelset_seq": 0, "projection_seq": 0})
        if self.labelset_store.load() is None:
            self.meta["labelset_seq"] = 1
            self.labelset_store.install(1, self._registry_payload())
        if self.projection_store.load() is None:
            self.meta["projection_seq"] = 1
            self.projection_store.install(1, self.projection.to_dict())
        self._save_json("meta.json", self.meta)

    def _gen_annotation(self, seed, version):
        labels, uncertain = deterministic_annotation(seed, self.labelsets[version])
        return {"labels": sorted(labels), "uncertain": sorted(uncertain)}

    def _registry_payload(self):
        return {str(v): ls.to_dict() for v, ls in sorted(self.labelsets.items())}

    def _labelsets_map(self):
        return {img: self.labelsets[v] for img, v in self.assignments.items()}

    def _record_error(self, err):
        self.errors.append(err.to_dict())
        with open(self._path("errors.jsonl"), "a") as fh:
            fh.write(json.dumps(err.to_dict(), sort_keys=True) + "\n")

    # ------------------------------------------------------------ runs

    def run_full(self, plan=None, degraded=None, cache=None):
        runner = Runner(self.cfg, cache or self.cache)
        report = runner.run(self.samples, self.annotations, self._labelsets_map(),
                            self.projection, plan=plan, degraded=degraded)
        if cache is None:
            self.last_report = report
            self.last_plan = plan
        return report

    def full_recompute_fresh(self):
        """From-scratch recompute in a throwaway cache (ground truth for
        incremental-equivalence checks)."""
        with tempfile.TemporaryDirectory() as tmp:
            return self.run_full(cache=Cache(tmp))

    def _commit(self, plan, apply_state, persist_state, snapshot_fn=None):
        try:
            with CommitLock(self.lock_path):
                violations, degradations = check_limits(plan, self.projection, self.cfg)
                if violations and self.cfg.limits.on_exceed == "error":
                    raise BusinessError(
                        E_RESOURCE_LIMIT, "resource limit exceeded",
                        violations=violations)
                degraded = degradations if violations else []
                old = (dict(self.annotations), dict(self.assignments),
                       self.projection)
                apply_state()
                try:
                    report = self.run_full(plan=plan, degraded=degraded)
                except BusinessError:
                    self.annotations, self.assignments, self.projection = old
                    raise
                persist_state()
                if snapshot_fn is not None:
                    snapshot_fn()
                self.meta["metrics_seq"] += 1
                self.metrics_store.install(self.meta["metrics_seq"], report.metrics)
                self._save_json("meta.json", self.meta)
                return report
        except BusinessError as err:
            self._record_error(err)
            raise

    # ------------------------------------------------------------ commits

    def fix_annotation(self, image_id, labels, uncertain):
        old = self.annotations[image_id]
        new = {"labels": sorted(labels), "uncertain": sorted(uncertain)}
        plan = plan_annotation_fix(image_id, old, new)

        def apply():
            self.annotations[image_id] = new

        def persist():
            self._save_json("annotations.json", self.annotations)

        return self._commit(plan, apply, persist)

    def change_labelset(self, image_id, version):
        if version not in self.labelsets:
            raise BusinessError(E_INCONSISTENT, "unknown label set version",
                                version=version)
        old_ls = self.labelsets[self.assignments[image_id]]
        new_ls = self.labelsets[version]
        plan = plan_labelset_change(image_id, old_ls, new_ls)
        seed = next(s.seed for s in self.samples if s.image_id == image_id)

        def apply():
            self.assignments[image_id] = version
            self.annotations[image_id] = self._gen_annotation(seed, version)

        def persist():
            self._save_json("assignments.json", self.assignments)
            self._save_json("annotations.json", self.annotations)

        def snapshot():
            self.meta["labelset_seq"] += 1
            self.labelset_store.install(self.meta["labelset_seq"],
                                        self._registry_payload())

        return self._commit(plan, apply, persist, snapshot)

    def replace_projection(self, params):
        plan = plan_projection_change([s.image_id for s in self.samples])

        def apply():
            self.projection = params

        def persist():
            pass  # projection lives in the projection snapshot store

        def snapshot():
            self.meta["projection_seq"] += 1
            self.projection_store.install(params.version, params.to_dict())

        return self._commit(plan, apply, persist, snapshot)

    # ------------------------------------------------------------ queries

    def explain_key(self, key):
        return self.cache.explain(key)

    def recent_errors(self, n=10):
        return self.errors[-n:]

    def snapshot_versions(self):
        def info(store):
            raw = store.load()
            return {"version": raw["version"], "checksum": raw["checksum"][:16]} if raw else None
        return {
            "labelsets": info(self.labelset_store),
            "projection": info(self.projection_store),
            "metrics": info(self.metrics_store),
        }

    # ------------------------------------------------------------ self-check

    def self_check(self):
        results = []

        def record(name, fn):
            try:
                detail = fn()
                results.append({"check": name, "ok": True, "detail": detail})
            except BusinessError as err:
                results.append({"check": name, "ok": False,
                                "detail": err.to_dict()})

        record("topology_acyclic", lambda: check_acyclic(self.cfg.edges) and "ok")

        def check_cache():
            bad = []
            for key in self.cache.iter_keys():
                ok, why = self.cache.verify(key)
                if not ok:
                    bad.append({"key": key, "reason": why})
            if bad:
                raise BusinessError(E_INCONSISTENT, "cache cross-check failed", bad=bad)
            return f"{len(list(self.cache.iter_keys()))} keys verified"

        record("cache_key_hash_crosscheck", check_cache)

        def check_sets():
            if self.last_report is None or self.last_plan is None:
                return "no incremental commit yet; skipped"
            recompute = set(self.last_report.recompute)
            reuse = set(self.last_report.reuse)
            inter = recompute & reuse
            if inter:
                raise BusinessError(E_INCONSISTENT,
                                    "recompute/reuse sets overlap",
                                    overlap=sorted(inter))
            affected = {f"{n}:{img}" for n, img in self.last_plan.cells}
            if self.last_plan.metrics_affected:
                affected = affected | {"metrics"}
            missing = affected - (recompute | reuse)
            if missing:
                raise BusinessError(E_INCONSISTENT,
                                    "recompute+reuse does not cover affected set",
                                    missing=sorted(missing))
            return f"disjoint; union covers {len(affected)} affected cells"

        record("recompute_reuse_coverage", check_sets)

        def check_metrics():
            if self.last_report is None:
                return "no run yet; skipped"
            labels = sorted({l for ls in self._labelsets_map().values() for l in ls.labels})
            verify_metrics(self.last_report.metrics, self.last_report.details,
                           labels, self.cfg)
            return "metrics recompute from details within tolerance"

        record("metrics_recomputable", check_metrics)

        if not all(r["ok"] for r in results):
            err = BusinessError(E_INCONSISTENT, "self-check failed", results=results)
            self._record_error(err)
            raise err
        return results
