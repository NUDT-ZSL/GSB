"""本地诊断工作台 CLI：生成、运行、查看、裁决、注入、自检一体化入口。"""

import argparse
import json
import os
import sys

from .adjudicate import adjudicate, replay_report, save_inputs, save_report
from .canon import canonical_json
from .dag import EDGES, NODES, Pipeline
from .errors import Err, WorkbenchError
from .formats import read as read_format
from .formats import write_v1, write_v2, write_v3
from .samples import gen_manifest, read_manifest, write_manifest
from .selfcheck import run_selfcheck
from .snapshots import SnapshotStore
from .vocab import VocabRegistry, VocabSnapshot

DEFAULT_CONFIG = {
    "seed": 20261122,
    "num_samples": 12,
    "num_classes": 6,
    "iou_threshold": 0.5,
    "fallback_class_id": None,
    "vocab_version": 1,
    "limits": {"max_images": 1000, "max_iou_comparisons": 10000000,
               "max_resident_candidates": 100000, "degrade_allowed": False},
}

VOCAB_V1 = {0: "person", 1: "car", 2: "dog", 3: "bicycle", 4: "sign", 5: "bag"}


def _paths(data):
    return {
        "manifest": os.path.join(data, "manifest.jsonl"),
        "config": os.path.join(data, "config.json"),
        "vocab1": os.path.join(data, "vocab_v1.json"),
        "vocab2": os.path.join(data, "vocab_v2.json"),
        "out": os.path.join(data, "out"),
        "inject": os.path.join(data, "inject"),
    }


def _out_paths(data):
    out = _paths(data)["out"]
    return {
        "v1": os.path.join(out, "matches_v1.txt"),
        "v2": os.path.join(out, "matches_v2.txt"),
        "v3": os.path.join(out, "matches_v3.json"),
        "run": os.path.join(out, "run_result.json"),
        "store": os.path.join(out, "store.json"),
        "meta": os.path.join(out, "last_run_meta.json"),
        "errors": os.path.join(out, "errors.jsonl"),
        "adjudications": os.path.join(out, "adjudications"),
    }


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _load_config(data):
    cfg = dict(DEFAULT_CONFIG)
    p = _paths(data)["config"]
    if os.path.exists(p):
        cfg.update(json.loads(_read(p)))
    cfg["limits"] = {**DEFAULT_CONFIG["limits"], **cfg.get("limits", {})}
    return cfg


def _load_samples(data):
    return [s for _, s in read_manifest(_paths(data)["manifest"])]


def _load_vocab(data, version):
    p = _paths(data)[f"vocab{version}"]
    return VocabSnapshot.load_file(p)


def _load_store(data):
    p = _out_paths(data)["store"]
    if os.path.exists(p):
        return SnapshotStore.from_dict(json.loads(_read(p)))
    return SnapshotStore()


def _record_error(data, err):
    p = _out_paths(data)["errors"]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(canonical_json(err.to_dict()) + "\n")


def _make_vocabs(data):
    v1 = VocabSnapshot(1, {cid: {"name": n, "retired": False}
                           for cid, n in VOCAB_V1.items()})
    cats2 = {cid: {"name": n, "retired": False} for cid, n in VOCAB_V1.items()}
    cats2[3]["retired"] = True
    cats2[5]["name"] = "handbag"
    cats2[6] = {"name": "robot", "retired": False}
    v2 = VocabSnapshot(2, cats2)
    v1.save_file(_paths(data)["vocab1"])
    v2.save_file(_paths(data)["vocab2"])
    return v1, v2


# ---------------------------------------------------------------- gen

def _make_injections(data, sample_line):
    """生成注入用损坏样例与注入清单。"""
    inj = _paths(data)["inject"]
    os.makedirs(inj, exist_ok=True)
    v1_line = ("s0000|0|1.000000|2.000000|10.000000|12.000000|"
               "0.900000|1|0|0.800000|TP")
    v2_fields = ("sample_id=s0000|cand_idx=0|x1=1.000000|y1=2.000000|"
                 "x2=10.000000|y2=12.000000|score=0.900000|class_id=1|"
                 "class_name=car|gt_idx=0|iou=0.800000|decision=TP|"
                 "difficulty=easy|occluded=false|conf_lo=0.850000|"
                 "conf_hi=0.950000|vocab_version=1")
    files = {
        "bad_version.txt": "#fmt=v9|vocab=1\n" + v1_line + "\n",
        "bad_header.txt": "#fmt=v1\n" + v1_line + "\n",
        "bad_fieldcount_v1.txt": "#fmt=v1|vocab=1\n"
                                 + "|".join(v1_line.split("|")[:9]) + "\n",
        "bad_missing_v2.txt": "#fmt=v2|vocab=1\n"
                              + v2_fields.replace("|iou=0.800000", "") + "\n",
        "bad_number_v2.txt": "#fmt=v2|vocab=1\n"
                             + v2_fields.replace("score=0.900000",
                                                 "score=abc") + "\n",
    }
    for name, text in files.items():
        _write(os.path.join(inj, name), text)
    tight = {"max_images": 2, "max_iou_comparisons": 10000000,
             "max_resident_candidates": 100000, "degrade_allowed": False}
    _write(os.path.join(inj, "tight_limits.json"), json.dumps(tight))
    cases = [
        {"case": "unknown_version", "kind": "format",
         "file": "bad_version.txt", "expect": Err.FORMAT_VERSION_UNSUPPORTED},
        {"case": "truncated_header", "kind": "format",
         "file": "bad_header.txt", "expect": Err.FORMAT_CORRUPT},
        {"case": "field_count", "kind": "format",
         "file": "bad_fieldcount_v1.txt", "expect": Err.FORMAT_CORRUPT},
        {"case": "missing_field", "kind": "format",
         "file": "bad_missing_v2.txt", "expect": Err.FORMAT_CORRUPT},
        {"case": "bad_number", "kind": "format",
         "file": "bad_number_v2.txt", "expect": Err.FORMAT_CORRUPT},
        {"case": "retired_label", "kind": "run", "vocab": 2,
         "expect": Err.LABEL_RETIRED},
        {"case": "resource_limit", "kind": "run", "vocab": 1,
         "limits_file": "tight_limits.json", "expect": Err.RESOURCE_LIMIT},
    ]
    manifest_path = os.path.join(inj, "injection_manifest.jsonl")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    return cases


def cmd_gen(args):
    data = args.data
    os.makedirs(data, exist_ok=True)
    cfg = _load_config(data)
    cfg["seed"] = args.seed
    cfg["num_samples"] = args.num_samples
    cfg["num_classes"] = args.num_classes
    _write(_paths(data)["config"], canonical_json(cfg) + "\n")
    _make_vocabs(data)
    samples = gen_manifest(cfg["seed"], cfg["num_samples"], cfg["num_classes"],
                           retired_label=3, retired_index=1)
    write_manifest(samples, _paths(data)["manifest"])
    _make_injections(data, None)
    print(f"[gen] 样本 {len(samples)} 张 -> {_paths(data)['manifest']}")
    print(f"[gen] 词表 v1/v2 -> {_paths(data)['vocab1']}, "
          f"{_paths(data)['vocab2']}")
    print(f"[gen] 注入清单 -> "
          f"{os.path.join(_paths(data)['inject'], 'injection_manifest.jsonl')}")
    print("[gen] 提示：样本 s0001 的真值含已停用类别 3（v2 中 bicycle 停用）")
    return 0


# ---------------------------------------------------------------- run

def _run_pipeline(data, cfg, vocab_version):
    samples = _load_samples(data)
    registry = VocabRegistry()
    registry.load(_load_vocab(data, vocab_version))
    store = _load_store(data)
    pipe = Pipeline(cfg, registry, store=store)
    prev_cfg = prev_vocab = None
    meta_path = _out_paths(data)["meta"]
    if os.path.exists(meta_path):
        meta = json.loads(_read(meta_path))
        prev_cfg = meta["config"]
        prev_vocab = VocabSnapshot.from_dict(meta["vocab"])
    result = pipe.run(samples, prev_config=prev_cfg, prev_vocab=prev_vocab)
    return result, registry.current, store


def cmd_run(args):
    data = args.data
    cfg = _load_config(data)
    if args.threshold is not None:
        cfg["iou_threshold"] = args.threshold
    if args.fallback_class is not None:
        cfg["fallback_class_id"] = args.fallback_class
    if args.degrade:
        cfg["limits"]["degrade_allowed"] = True
        cfg["limits"]["max_resident_candidates"] = args.degrade
    vocab_version = args.vocab if args.vocab is not None else cfg["vocab_version"]
    try:
        result, vocab_snap, store = _run_pipeline(data, cfg, vocab_version)
    except WorkbenchError as err:
        _record_error(data, err)
        print(f"[run] 业务错误: {err.code} {err.message}")
        return 2
    out = _out_paths(data)
    vv = vocab_snap.version
    _write(out["v1"], write_v1(result["records"], vv))
    _write(out["v2"], write_v2(result["records"], vv))
    _write(out["v3"], write_v3(result["records"], vv))
    _write(out["run"], canonical_json(result) + "\n")
    _write(out["store"], canonical_json(store.to_dict()) + "\n")
    _write(out["meta"], canonical_json({
        "config": {"iou_threshold": cfg["iou_threshold"],
                   "fallback_class_id": cfg["fallback_class_id"]},
        "vocab": vocab_snap.to_dict()}) + "\n")
    tp = sum(len(s["tp"]) for s in result["stats"].values())
    fp = sum(len(s["fp"]) for s in result["stats"].values())
    fn = sum(len(s["fn"]) for s in result["stats"].values())
    print(f"[run] 词表 v{vv} 阈值 {cfg['iou_threshold']} "
          f"记录 {len(result['records'])} 条 TP={tp} FP={fp} FN={fn}")
    print(f"[run] 节点状态: "
          + ", ".join(f"{n}(算{result['node_states'][n]['computed']}/"
                      f"复用{result['node_states'][n]['reused']})"
                      for n in NODES))
    if result["invalidation"]:
        inv = result["invalidation"]
        print(f"[run] 失效传播: 重算 {len(inv['recompute'])} 张, "
              f"复用 {len(inv['reuse'])} 张")
    if result["degradation"]:
        print(f"[run] 降级类别: {result['degradation']}")
    print(f"[run] 三格式导出 -> {out['v1']}, {out['v2']}, {out['v3']}")
    return 0


# ---------------------------------------------------------------- show/view

def _load_run(data):
    p = _out_paths(data)["run"]
    if not os.path.exists(p):
        raise WorkbenchError(Err.INCONSISTENT, "尚未运行：请先执行 run",
                             category="run_missing")
    return json.loads(_read(p))


def cmd_show(args):
    data = args.data
    what = args.what
    if what == "topology":
        print("[topology] 节点:", " -> ".join(NODES))
        for a, b in EDGES:
            print(f"[topology] 边: {a} -> {b}")
        return 0
    result = _load_run(data)
    if what == "nodes":
        for n in NODES:
            st = result["node_states"][n]
            print(f"[node] {n}: computed={st['computed']} reused={st['reused']}")
        print(f"[node] 缓存: {result['cache']}")
    elif what == "invalidation":
        inv = result["invalidation"]
        if not inv:
            print("[invalidation] 首次运行，无失效传播")
        else:
            print(f"[invalidation] 重算集合: {inv['recompute']}")
            print(f"[invalidation] 复用集合: {inv['reuse']}")
    elif what == "matches":
        sid = args.sample
        for r in result["records"]:
            if sid and r["sample_id"] != sid:
                continue
            print(f"[match] {r['sample_id']}#{r['cand_idx']} "
                  f"cls={r['class_id']}({r['class_name']}) "
                  f"score={r['score']:.4f} gt={r['gt_idx']} "
                  f"iou={r['iou']:.4f} {r['decision']} "
                  f"diff={r['difficulty']} occ={r['occluded']}")
    elif what == "stats":
        print("[stats] 按类别:")
        for cid, b in result["aggregates"]["by_class"].items():
            print(f"  class={cid}: {b}")
        print("[stats] 按类别+难度:")
        for k, b in result["aggregates"]["by_class_difficulty"].items():
            print(f"  {k}: {b}")
    elif what == "vocab":
        print(f"[vocab] version={result['vocab_version']} "
              f"checksum={result['vocab_checksum'][:16]}...")
    elif what == "errors":
        p = _out_paths(data)["errors"]
        if not os.path.exists(p):
            print("[errors] 无记录")
        else:
            for line in _read(p).splitlines()[-20:]:
                print(f"[error] {line}")
    return 0


def cmd_view(args):
    data = args.data
    out = _out_paths(data)
    path = {"v1": out["v1"], "v2": out["v2"], "v3": out["v3"]}[args.format]
    if not os.path.exists(path):
        print(f"[view] 文件不存在，请先 run: {path}")
        return 2
    lines = _read(path).splitlines()
    print(f"[view] 格式 {args.format} 共 {len(lines)} 行: {path}")
    for line in lines[:args.lines]:
        print(line)
    return 0


# ---------------------------------------------------------------- adjudicate

def _adjudicate_now(data):
    out = _out_paths(data)
    result = _load_run(data)
    vocab_snap = _load_vocab(data, result["vocab_version"])
    samples_gt = {s["sample_id"]: s["gt"] for s in _load_samples(data)}
    return adjudicate(_read(out["v1"]), _read(out["v2"]), _read(out["v3"]),
                      vocab_snap, samples_gt)


def cmd_adjudicate(args):
    data = args.data
    out = _out_paths(data)
    try:
        report = _adjudicate_now(data)
    except WorkbenchError as err:
        _record_error(data, err)
        print(f"[adjudicate] 业务错误: {err.code} {err.message}")
        return 2
    adjudication_id, path = save_report(report, out["adjudications"])
    result = _load_run(data)
    vocab_snap = _load_vocab(data, result["vocab_version"])
    samples_gt = {s["sample_id"]: s["gt"] for s in _load_samples(data)}
    save_inputs(adjudication_id, out["adjudications"],
                _read(out["v1"]), _read(out["v2"]), _read(out["v3"]),
                vocab_snap, samples_gt)
    print(f"[adjudicate] 裁决 {adjudication_id}: verdict={report['verdict']}")
    if report["error"]:
        print(f"[adjudicate] {report['error']['code']} "
              f"不可还原字段: {report['error']['unrestorable_fields']}")
        print(f"[adjudicate] 漂移明细 {len(report['drift_details'])} 条, "
              f"报告 -> {path}")
    else:
        print(f"[adjudicate] 三格式共享语义完全一致, 报告 -> {path}")
    return 0


def cmd_replay(args):
    data = args.data
    try:
        report = replay_report(args.adjudication_id,
                               _out_paths(data)["adjudications"])
    except WorkbenchError as err:
        _record_error(data, err)
        print(f"[replay] 业务错误: {err.code} {err.message}")
        return 2
    print(f"[replay] 裁决 {args.adjudication_id} 复现一致: "
          f"verdict={report['verdict']}")
    return 0


# ---------------------------------------------------------------- inject/demo

def cmd_inject(args):
    data = args.data
    inj_dir = _paths(data)["inject"]
    cases = [json.loads(l) for l in
             _read(os.path.join(inj_dir, "injection_manifest.jsonl")).splitlines()
             if l.strip()]
    failures = 0
    for case in cases:
        actual = None
        attribution = ""
        try:
            if case["kind"] == "format":
                read_format(_read(os.path.join(inj_dir, case["file"])))
            else:
                cfg = _load_config(data)
                if "limits_file" in case:
                    cfg["limits"] = json.loads(
                        _read(os.path.join(inj_dir, case["limits_file"])))
                _run_pipeline(data, cfg, case.get("vocab", 1))
        except WorkbenchError as err:
            actual = err.code
            parts = []
            if err.line is not None:
                parts.append(f"行号={err.line}")
            if err.field is not None:
                parts.append(f"字段={err.field}")
            if err.category is not None:
                parts.append(f"类别={err.category}")
            attribution = " ".join(parts)
            _record_error(data, err)
        ok = actual == case["expect"]
        failures += 0 if ok else 1
        mark = "OK" if ok else "MISMATCH"
        print(f"[inject] {mark} {case['case']}: 期望 {case['expect']}, "
              f"实际 {actual} {attribution}")
    print(f"[inject] {len(cases) - failures}/{len(cases)} 用例归因符合预期")
    return 0 if failures == 0 else 2


def cmd_degrade_demo(args):
    data = args.data
    cfg = _load_config(data)
    cfg["limits"]["max_resident_candidates"] = 10
    cfg["limits"]["degrade_allowed"] = False
    try:
        _run_pipeline(data, cfg, cfg["vocab_version"])
        print("[degrade-demo] 意外：未触发资源上限")
        return 2
    except WorkbenchError as err:
        _record_error(data, err)
        print(f"[degrade-demo] 严格模式: {err.code} 类别={err.category}")
    cfg["limits"]["degrade_allowed"] = True
    result, vocab_snap, _ = _run_pipeline(data, cfg, cfg["vocab_version"])
    print(f"[degrade-demo] 降级模式: 降级类别={result['degradation']}, "
          f"记录 {len(result['records'])} 条照常产出")
    return 0


def cmd_vocab_demo(args):
    data = args.data
    registry = VocabRegistry()
    v1, v2 = _load_vocab(data, 1), _load_vocab(data, 2)
    print(f"[vocab] 加载 v1: replaced={registry.load(v1)} "
          f"checksum={v1.checksum[:12]}")
    print(f"[vocab] 加载 v2: replaced={registry.load(v2)} "
          f"checksum={v2.checksum[:12]} retired={v2.retired_ids}")
    try:
        registry.load(v1)
        print("[vocab] 意外：回退未被拒绝")
        return 2
    except WorkbenchError as err:
        _record_error(data, err)
        print(f"[vocab] 回退尝试被拒绝: {err.code} {err.message}")
    try:
        registry.resolve_class(3)
    except WorkbenchError as err:
        _record_error(data, err)
        print(f"[vocab] 停用类别命中: {err.code} {err.message}")
    cid, name, prov = registry.resolve_class(3, fallback_class_id=0)
    print(f"[vocab] 显式回退: class 3 -> {cid}({name}) 来源标记={prov}")
    return 0


def cmd_selfcheck(args):
    data = args.data
    try:
        result = _load_run(data)
        samples_gt = {s["sample_id"]: s["gt"] for s in _load_samples(data)}
        store = _load_store(data)
        passed = run_selfcheck(result, samples_gt, store)
    except WorkbenchError as err:
        _record_error(data, err)
        print(f"[selfcheck] {err.code} 类别={err.category}: {err.message}")
        return 2
    for item in passed:
        print(f"[selfcheck] 通过: {item}")
    return 0


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="detwb",
        description="本地确定性候选匹配诊断工作台")
    p.add_argument("--data", default="data", help="数据目录（默认 data/）")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="生成确定性样本、词表与注入清单")
    g.add_argument("--seed", type=int, default=DEFAULT_CONFIG["seed"])
    g.add_argument("--num-samples", type=int,
                   default=DEFAULT_CONFIG["num_samples"])
    g.add_argument("--num-classes", type=int,
                   default=DEFAULT_CONFIG["num_classes"])
    g.set_defaults(func=cmd_gen)

    r = sub.add_parser("run", help="运行匹配计算图并导出三格式")
    r.add_argument("--threshold", type=float, default=None)
    r.add_argument("--vocab", type=int, choices=(1, 2), default=None)
    r.add_argument("--fallback-class", type=int, default=None)
    r.add_argument("--degrade", type=int, default=None,
                   metavar="MAX_RESIDENT",
                   help="驻留候选上限并允许降级")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("show", help="展示拓扑/节点/失效/明细/统计/错误")
    s.add_argument("what", choices=("topology", "nodes", "invalidation",
                                    "matches", "stats", "vocab", "errors"))
    s.add_argument("--sample", default=None)
    s.set_defaults(func=cmd_show)

    v = sub.add_parser("view", help="切换 v1/v2/v3 视图")
    v.add_argument("--format", choices=("v1", "v2", "v3"), required=True)
    v.add_argument("--lines", type=int, default=5)
    v.set_defaults(func=cmd_view)

    a = sub.add_parser("adjudicate", help="跨格式裁决并持久化报告")
    a.set_defaults(func=cmd_adjudicate)

    rp = sub.add_parser("replay", help="复现历史裁决")
    rp.add_argument("adjudication_id")
    rp.set_defaults(func=cmd_replay)

    ij = sub.add_parser("inject", help="执行注入清单并归因")
    ij.set_defaults(func=cmd_inject)

    dg = sub.add_parser("degrade-demo", help="资源超限与降级模式演示")
    dg.set_defaults(func=cmd_degrade_demo)

    vd = sub.add_parser("vocab-demo", help="词表演进/回退/停用类别演示")
    vd.set_defaults(func=cmd_vocab_demo)

    sc = sub.add_parser("selfcheck", help="跨模块一致性自检")
    sc.set_defaults(func=cmd_selfcheck)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except WorkbenchError as err:
        _record_error(args.data, err)
        print(f"[error] {err.code}: {err.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
