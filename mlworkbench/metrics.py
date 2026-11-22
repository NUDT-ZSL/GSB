"""Multi-label metrics with uncertain-label policy and recompute consistency."""
from fractions import Fraction

from .errors import BusinessError, E_LABEL_UNKNOWN, E_METRIC_MISMATCH


def build_details(decisions, annotations, labelsets):
    """Per-sample-per-label detail rows. Validates truth against label sets."""
    rows = []
    for image_id in sorted(annotations):
        ann = annotations[image_id]
        ls = labelsets[image_id]
        unknown = (set(ann["labels"]) | set(ann["uncertain"])) - set(ls.labels)
        if unknown:
            raise BusinessError(
                E_LABEL_UNKNOWN, "annotation references labels outside the controlled set",
                image=image_id, labels=sorted(unknown),
            )
        decided = set(decisions[image_id])
        for label in ls.labels:
            rows.append({
                "image": image_id,
                "label": label,
                "truth": 1 if label in ann["labels"] else 0,
                "uncertain": 1 if label in ann["uncertain"] else 0,
                "pred": 1 if label in decided else 0,
            })
    return rows


def _count_rows(rows, labels, uncertain_policy):
    """Returns (counts, pair_count, uncertain_stats) over counted rows only."""
    counts = {label: [0, 0, 0, 0] for label in labels}
    unc = {"total": 0, "pred_positive": 0, "truth_positive": 0}
    pairs = 0
    for r in rows:
        label = r["label"]
        truth, pred = r["truth"], r["pred"]
        if r["uncertain"]:
            unc["total"] += 1
            unc["pred_positive"] += pred
            unc["truth_positive"] += truth
            if uncertain_policy in ("ignore", "separate"):
                continue
            truth = 1  # as_positive
        pairs += 1
        if truth and pred:
            counts[label][0] += 1
        elif not truth and pred:
            counts[label][1] += 1
        elif truth and not pred:
            counts[label][2] += 1
        else:
            counts[label][3] += 1
    return counts, pairs, unc


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def _assemble(counts, pairs, unc, labels, cfg):
    per_label = {}
    macro_f1s = []
    tp_tot = fp_tot = fn_tot = 0
    no_positive = []
    for label in labels:
        tp, fp, fn, tn = counts[label]
        tp_tot += tp
        fp_tot += fp
        fn_tot += fn
        support = tp + fn
        p, r, f = _prf(tp, fp, fn)
        flagged = support == 0
        if flagged:
            no_positive.append(label)
        if not (flagged and cfg.no_positive_policy == "skip"):
            macro_f1s.append(f)
        per_label[label] = {
            "precision": p, "recall": r, "f1": f,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "support": support, "no_positive": flagged,
        }
    mp, mr, mf = _prf(tp_tot, fp_tot, fn_tot)
    return {
        "policy": {
            "uncertain": cfg.uncertain_policy,
            "no_positive": cfg.no_positive_policy,
            "metric_tol": cfg.metric_tol,
        },
        "micro": {"precision": mp, "recall": mr, "f1": mf},
        "macro": {"f1": sum(macro_f1s) / len(macro_f1s) if macro_f1s else 0.0},
        "per_label": per_label,
        "hamming_loss": (fp_tot + fn_tot) / pairs if pairs else 0.0,
        "no_positive_labels": sorted(no_positive),
        "uncertain_stats": unc,
        "counts": {"label_pairs": pairs, "tp": tp_tot, "fp": fp_tot, "fn": fn_tot},
    }


def aggregate(details, labels, cfg):
    counts, pairs, unc = _count_rows(details, labels, cfg.uncertain_policy)
    return _assemble(counts, pairs, unc, labels, cfg)


def reaggregate(details, labels, cfg):
    """Independent recompute from details using exact Fraction arithmetic."""
    counts, pairs, _ = _count_rows(details, labels, cfg.uncertain_policy)
    exact = {label: [Fraction(c) for c in counts[label]] for label in labels}

    def prf(tp, fp, fn):
        p = tp / (tp + fp) if tp + fp else Fraction(0)
        r = tp / (tp + fn) if tp + fn else Fraction(0)
        f = 2 * p * r / (p + r) if p + r else Fraction(0)
        return p, r, f

    per_label = {}
    macro_f1s = []
    tp_tot = fp_tot = fn_tot = Fraction(0)
    for label in labels:
        tp, fp, fn, _ = exact[label]
        tp_tot += tp
        fp_tot += fp
        fn_tot += fn
        p, r, f = prf(tp, fp, fn)
        support = tp + fn
        if not (support == 0 and cfg.no_positive_policy == "skip"):
            macro_f1s.append(f)
        per_label[label] = {"precision": float(p), "recall": float(r), "f1": float(f)}
    mp, mr, mf = prf(tp_tot, fp_tot, fn_tot)
    return {
        "micro": {"precision": float(mp), "recall": float(mr), "f1": float(mf)},
        "macro": {"f1": float(sum(macro_f1s) / len(macro_f1s)) if macro_f1s else 0.0},
        "per_label": per_label,
        "hamming_loss": float((fp_tot + fn_tot) / pairs) if pairs else 0.0,
    }


def verify_metrics(report, details, labels, cfg):
    """Raise E_METRIC_MISMATCH if the report deviates from an independent
    recompute over the detail rows beyond the configured tolerance."""
    if "policy" not in report:
        raise BusinessError(
            E_METRIC_MISMATCH, "metric policy not recorded; report not exportable")
    again = reaggregate(details, labels, cfg)
    tol = cfg.metric_tol
    diffs = []

    def cmp(path, a, b):
        if abs(a - b) > tol:
            diffs.append({"path": path, "report": a, "recomputed": b})

    for k in ("precision", "recall", "f1"):
        cmp(f"micro.{k}", report["micro"][k], again["micro"][k])
    cmp("macro.f1", report["macro"]["f1"], again["macro"]["f1"])
    cmp("hamming_loss", report["hamming_loss"], again["hamming_loss"])
    for label in labels:
        for k in ("precision", "recall", "f1"):
            cmp(f"per_label.{label}.{k}", report["per_label"][label][k],
                again["per_label"][label][k])
    if diffs:
        raise BusinessError(
            E_METRIC_MISMATCH, "metric recompute deviation beyond tolerance",
            tol=tol, diffs=diffs)
    return True
