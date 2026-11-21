"""跨模块一致性自检：任一不一致返回 E_INCONSISTENT 并指出类别。"""

from .canon import canonical_json, sha256_text
from .dag import EDGES, NODES, topo_order
from .errors import Err, WorkbenchError


def check_tp_fp_disjoint(run_result):
    """任一候选不得同时计入 TP 与 FP。"""
    for sid, st in sorted(run_result["stats"].items()):
        both = set(st["tp"]) & set(st["fp"])
        if both:
            raise WorkbenchError(
                Err.INCONSISTENT,
                f"样本 {sid} 候选 {sorted(both)} 同时计入 TP 与 FP",
                category="cand_tp_fp_overlap")


def check_gt_tp_fn_disjoint(run_result, samples_gt):
    """任一真值框不得同时计入 TP 与 FN。"""
    matched = {}
    for r in run_result["records"]:
        if r["gt_idx"] >= 0:
            matched.setdefault(r["sample_id"], set()).add(r["gt_idx"])
    for sid, st in sorted(run_result["stats"].items()):
        both = matched.get(sid, set()) & set(st["fn"])
        if both:
            raise WorkbenchError(
                Err.INCONSISTENT,
                f"样本 {sid} 真值框 {sorted(both)} 同时计入 TP 与 FN",
                category="gt_tp_fn_overlap")


def check_acyclic():
    """计算图顶点无环。"""
    topo_order(NODES, EDGES)  # 有环时抛 E_INCONSISTENT(category=cycle)


def check_cache_artifacts(store):
    """缓存键与产物哈希互相可核对：逐条重算校验和。"""
    for key in store.keys():
        snap = store._entries[key]
        actual = sha256_text(canonical_json(snap.payload))
        if actual != snap.checksum:
            raise WorkbenchError(
                Err.INCONSISTENT,
                f"缓存键 {key} 的产物哈希与登记校验和不符",
                category="cache_hash")


def run_selfcheck(run_result, samples_gt, store):
    """执行全部自检，返回通过项清单。"""
    check_tp_fp_disjoint(run_result)
    check_gt_tp_fn_disjoint(run_result, samples_gt)
    check_acyclic()
    check_cache_artifacts(store)
    return ["cand_tp_fp_disjoint", "gt_tp_fn_disjoint",
            "dag_acyclic", "cache_hash_consistent"]
