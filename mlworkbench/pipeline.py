"""Deterministic node computations and the cache-orchestrated runner."""
import hashlib
import math
from dataclasses import dataclass, field

import numpy as np

from .cache import make_key
from .errors import BusinessError, E_DECISION_INVALID
from .hashing import digest_obj, hash_array
from .metrics import aggregate, build_details, verify_metrics


# ---------------------------------------------------------------- node math

def node_preprocess(pixels):
    return pixels.astype(np.float64) / 255.0


def projection_matrix(proj, in_dim):
    rng = np.random.default_rng(proj.seed)
    return rng.standard_normal((in_dim, proj.out_dim)) / math.sqrt(in_dim)


def node_features(pre, cfg, proj):
    size, bs = cfg.image_size, cfg.block_size
    nb = size // bs
    blocks = pre.reshape(nb, bs, nb, bs, 3)
    means = blocks.mean(axis=(1, 3))
    stds = blocks.std(axis=(1, 3))
    vec = np.concatenate([means.ravel(), stds.ravel()])
    return vec @ projection_matrix(proj, vec.size)


def _label_weights(label_set, label, dim):
    h = hashlib.sha256(
        f"w|{label_set.score_seed}|{label}|{label_set.version}".encode()).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
    w = rng.standard_normal(dim) / math.sqrt(dim)
    b = float(rng.standard_normal()) * 0.1
    return w, b


def node_score(features, label_set):
    scores = {}
    for label in label_set.labels:
        w, b = _label_weights(label_set, label, features.shape[0])
        z = float(features @ w) + b
        scores[label] = 1.0 / (1.0 + math.exp(-z))
    return scores


def node_decide(scores, label_set, image_id):
    unknown = set(scores) - set(label_set.labels)
    decided = sorted(
        l for l, s in scores.items()
        if l in label_set.thresholds and s >= label_set.thresholds[l])
    unknown |= set(decided) - set(label_set.labels)
    if unknown:
        raise BusinessError(
            E_DECISION_INVALID, "decision contains labels outside the controlled set",
            image=image_id, labels=sorted(unknown))
    return decided


# ------------------------------------------------------------------- report

@dataclass
class RunReport:
    statuses: dict = field(default_factory=dict)   # node -> {image: status}
    metrics_status: str = ""
    scores: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    details: list = field(default_factory=list)
    recompute: list = field(default_factory=list)  # "node:image" / "metrics"
    reuse: list = field(default_factory=list)
    degraded: list = field(default_factory=list)
    affected: dict = field(default_factory=dict)
    cache_keys: dict = field(default_factory=dict)  # "node:image" -> key

    def to_dict(self):
        return {
            "statuses": self.statuses,
            "metrics_status": self.metrics_status,
            "decisions": self.decisions,
            "metrics": self.metrics,
            "recompute": self.recompute,
            "reuse": self.reuse,
            "degraded": self.degraded,
            "affected": self.affected,
        }


# ------------------------------------------------------------------- runner

class Runner:
    """Executes the DAG per image with content-addressed caching.

    If a plan is given, cells in plan.cells are force-recomputed; all other
    cells must be served from cache (a miss is computed but flagged "miss").
    """

    def __init__(self, cfg, cache):
        self.cfg = cfg
        self.cache = cache

    def _cell(self, report, node, image_id, key, components, forced, compute):
        hit = None if forced else self.cache.get(key)
        if hit is not None:
            obj, content_hash = hit
            status = "reuse"
        else:
            obj = compute()
            content_hash = self.cache.put(key, components, obj)
            status = "recompute" if forced else "miss"
        cell = f"{node}:{image_id}" if image_id else node
        report.statuses.setdefault(node, {})[image_id or ""] = status
        report.cache_keys[cell] = key
        (report.reuse if status == "reuse" else report.recompute).append(cell)
        return obj, content_hash

    def run(self, samples, annotations, labelsets, projection, plan=None,
            degraded=None):
        cfg = self.cfg
        report = RunReport(degraded=list(degraded or []))
        if plan is not None:
            report.affected = plan.summary()
        decide_hashes = {}

        for sample in samples:
            img = sample.image_id
            ls = labelsets[img]

            def forced(node, img=img):
                return plan is not None and (node, img) in plan.cells

            key, comp = make_key(
                "preprocess", cfg.node_params("preprocess"),
                {"pixels": hash_array(sample.pixels), "seed": sample.seed},
                {}, None)
            pre, h_pre = self._cell(report, "preprocess", img, key, comp,
                                    forced("preprocess"),
                                    lambda: node_preprocess(sample.pixels))

            fparams = dict(cfg.node_params("features"))
            fparams["projection"] = projection.to_dict()
            key, comp = make_key("features", fparams, {}, {"preprocess": h_pre}, None)
            feat, h_feat = self._cell(report, "features", img, key, comp,
                                      forced("features"),
                                      lambda: node_features(pre, cfg, projection))

            key, comp = make_key(
                "score", {"score_seed": ls.score_seed}, {},
                {"features": h_feat}, ls.version)
            scores, h_score = self._cell(report, "score", img, key, comp,
                                         forced("score"),
                                         lambda: node_score(feat, ls))

            key, comp = make_key(
                "decide", {"thresholds": ls.thresholds}, {},
                {"score": h_score}, ls.version)
            decided, h_dec = self._cell(report, "decide", img, key, comp,
                                        forced("decide"),
                                        lambda: node_decide(scores, ls, img))

            report.scores[img] = scores
            report.decisions[img] = decided
            decide_hashes[img] = h_dec

        ann_digest = digest_obj({
            img: {"labels": sorted(a["labels"]), "uncertain": sorted(a["uncertain"])}
            for img, a in annotations.items()
        })
        key, comp = make_key(
            "metrics", cfg.node_params("metrics"),
            {"annotations": ann_digest},
            {f"decide:{img}": h for img, h in sorted(decide_hashes.items())},
            sorted({ls.version for ls in labelsets.values()}))
        metrics_forced = plan is not None and plan.metrics_affected

        def compute_metrics():
            details = build_details(report.decisions, annotations, labelsets)
            labels = sorted({l for ls in labelsets.values() for l in ls.labels})
            rep = aggregate(details, labels, cfg)
            verify_metrics(rep, details, labels, cfg)
            return {"report": rep, "details": details}

        hit = None if metrics_forced else self.cache.get(key)
        if hit is not None:
            bundle, _ = hit
            report.metrics_status = "reuse"
            report.reuse.append("metrics")
        else:
            bundle = compute_metrics()
            self.cache.put(key, comp, bundle)
            report.metrics_status = "recompute" if metrics_forced else "miss"
            report.recompute.append("metrics")
        report.cache_keys["metrics"] = key
        report.metrics = bundle["report"]
        report.details = bundle["details"]
        return report
