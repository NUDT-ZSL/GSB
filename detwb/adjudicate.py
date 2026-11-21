"""跨格式裁决：同一匹配结果的 v1/v2/v3 书写必须还原出相同语义。"""

import json
import os

from .canon import canonical_json, sha256_text
from .errors import Err, WorkbenchError
from .formats import read as read_format
from .matching import aggregate
from .migrate import FIELD_DEFAULTS, migrate_v1_to_rich


def _assignment_map(records):
    return {f"{r['sample_id']}#{r['cand_idx']}": r["gt_idx"]
            for r in records}


def _tp_fp_fn(records, samples_gt):
    tp = sorted(f"{r['sample_id']}#{r['cand_idx']}" for r in records
                if r["decision"] == "TP")
    fp = sorted(f"{r['sample_id']}#{r['cand_idx']}" for r in records
                if r["decision"] == "FP")
    matched = {(r["sample_id"], r["gt_idx"]) for r in records if r["gt_idx"] >= 0}
    fn = sorted(f"{sid}#{g['gt_idx']}" for sid, gts in samples_gt.items()
                for g in gts if (sid, g["gt_idx"]) not in matched)
    return tp, fp, fn


def adjudicate(text_v1, text_v2, text_v3, vocab_snapshot, samples_gt):
    """对同一结果的三份书写做裁决，返回裁决报告 dict。"""
    rec_v1, vv1, _ = read_format(text_v1)
    rec_v2, vv2, _ = read_format(text_v2)
    rec_v3, vv3, _ = read_format(text_v3)
    if not (vv1 == vv2 == vv3 == vocab_snapshot.version):
        raise WorkbenchError(
            Err.INCONSISTENT,
            f"三份书写的词表版本不一致: v1={vv1}, v2={vv2}, v3={vv3}, "
            f"registry={vocab_snapshot.version}",
            category="vocab_version")

    rich_v1, provenance = migrate_v1_to_rich(rec_v1, vocab_snapshot)
    views = {"v1": rich_v1, "v2": rec_v2, "v3": rec_v3}

    ref = views["v3"]
    for name in ("v1", "v2"):
        cur = views[name]
        if _assignment_map(cur) != _assignment_map(ref):
            raise WorkbenchError(Err.INCONSISTENT,
                                 f"{name} 与 v3 的候选指派不一致",
                                 category="cross_format_assignment")
        for label, a, b in zip(("tp", "fp", "fn"),
                               _tp_fp_fn(cur, samples_gt),
                               _tp_fp_fn(ref, samples_gt)):
            if a != b:
                raise WorkbenchError(Err.INCONSISTENT,
                                     f"{name} 与 v3 的 {label.upper()} 集合不一致",
                                     category=f"cross_format_{label}")
        if aggregate(cur, samples_gt)["by_class"] != \
                aggregate(ref, samples_gt)["by_class"]:
            raise WorkbenchError(Err.INCONSISTENT,
                                 f"{name} 与 v3 的按类聚合数值不一致",
                                 category="cross_format_aggregate")

    drift_fields = set()
    drift_details = []
    v1_by_key = {f"{r['sample_id']}#{r['cand_idx']}": r for r in rich_v1}
    for r in ref:
        k = f"{r['sample_id']}#{r['cand_idx']}"
        restored = v1_by_key.get(k)
        for f, default in FIELD_DEFAULTS.items():
            if r[f] != default and (restored is None or restored[f] != r[f]):
                drift_fields.add(f)
                drift_details.append({"record": k, "field": f,
                                      "v3_value": r[f],
                                      "v1_restored": restored[f] if restored else None})
        if restored is not None and restored["class_name"] != r["class_name"]:
            drift_fields.add("class_name")
            drift_details.append({"record": k, "field": "class_name",
                                  "v3_value": r["class_name"],
                                  "v1_restored": restored["class_name"]})
    agg_diff = aggregate(views["v1"], samples_gt)["by_class_difficulty"] != \
        aggregate(ref, samples_gt)["by_class_difficulty"]
    if agg_diff:
        drift_fields.add("difficulty")
        drift_details.append({"record": "*", "field": "difficulty",
                              "v3_value": "per-difficulty aggregates",
                              "v1_restored": "unknown"})

    verdict = "EQUIVALENT"
    error = None
    if drift_fields:
        verdict = "DRIFT"
        error = {"code": Err.SEMANTIC_DRIFT,
                 "unrestorable_fields": sorted(drift_fields)}

    return {
        "verdict": verdict,
        "error": error,
        "vocab_version": vocab_snapshot.version,
        "vocab_checksum": vocab_snapshot.checksum,
        "record_count": len(ref),
        "shared_semantics": {
            "assignment": "identical",
            "tp_fp_fn": "identical",
            "aggregates": "identical",
        },
        "drift_details": sorted(drift_details,
                                key=lambda d: (d["record"], d["field"])),
        "v1_provenance_summary": _summarize_provenance(provenance),
    }


def _summarize_provenance(provenance):
    summary = {}
    for p in provenance:
        key = f"{p['field']}:{p['source']}"
        summary[key] = summary.get(key, 0) + 1
    return dict(sorted(summary.items()))


def save_report(report, directory):
    """持久化裁决报告，返回 (裁决标识, 路径)。标识为内容哈希。"""
    os.makedirs(directory, exist_ok=True)
    body = canonical_json(report)
    adjudication_id = sha256_text(body)[:16]
    path = os.path.join(directory, f"adjudication_{adjudication_id}.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    return adjudication_id, path


def save_inputs(adjudication_id, directory, text_v1, text_v2, text_v3,
                vocab_snapshot, samples_gt):
    """把裁决输入随报告落盘，使任意历史裁决可脱离当前状态复现。"""
    sidecar = {
        "v1": text_v1, "v2": text_v2, "v3": text_v3,
        "vocab": vocab_snapshot.to_dict(),
        "samples_gt": samples_gt,
    }
    path = os.path.join(directory, f"adjudication_{adjudication_id}_inputs.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(canonical_json(sidecar) + "\n")
    return path


def replay_report(adjudication_id, directory, reproduce_fn=None):
    """复现历史裁决：优先从随报告落盘的输入重算，与持久化报告逐字节比对。"""
    path = os.path.join(directory, f"adjudication_{adjudication_id}.json")
    if not os.path.exists(path):
        raise WorkbenchError(Err.INCONSISTENT,
                             f"裁决报告不存在: {adjudication_id}",
                             category="adjudication_missing")
    with open(path, "r", encoding="utf-8") as fh:
        stored = fh.read()
    if reproduce_fn is None:
        from .vocab import VocabSnapshot
        sidecar_path = os.path.join(
            directory, f"adjudication_{adjudication_id}_inputs.json")
        if not os.path.exists(sidecar_path):
            raise WorkbenchError(Err.INCONSISTENT,
                                 f"裁决 {adjudication_id} 缺少输入侧车文件",
                                 category="adjudication_missing")
        with open(sidecar_path, "r", encoding="utf-8") as fh:
            sidecar = json.load(fh)
        vocab_snapshot = VocabSnapshot.from_dict(sidecar["vocab"])
        reproduce_fn = lambda: adjudicate(
            sidecar["v1"], sidecar["v2"], sidecar["v3"],
            vocab_snapshot, sidecar["samples_gt"])
    reproduced = canonical_json(reproduce_fn()) + "\n"
    if reproduced != stored:
        raise WorkbenchError(Err.INCONSISTENT,
                             f"裁决 {adjudication_id} 复现结果与历史不一致",
                             category="adjudication_replay")
    return json.loads(stored)
