"""确定性样本生成器：固定种子产出三版本工程文档、反馈事件与边界样本。"""
import json
import random
from datetime import datetime, timezone
from pathlib import Path

SEED = 20261128
PLANNED_START = datetime(2026, 3, 1, tzinfo=timezone.utc)
DAY_MS = 86_400_000

STEPS = [("FND", "土方开挖", "土方工"),
         ("RBR", "钢筋绑扎", "钢筋工"),
         ("CNC", "混凝土浇筑", "混凝土工")]
CAPS = {"土方工": 14, "钢筋工": 10, "混凝土工": 9}
PROJECT_ID = "demo"
PROJECT_NAME = "示范施工进度工程"


def canonical_activities():
    """2 区 × 2 栋 × 2 流水段 × 3 工序，固定种子决定工期与人数。"""
    rng = random.Random(SEED)
    activities = []
    for z in (1, 2):
        for b in (1, 2):
            for s in (1, 2):
                for order, (code, name, trade) in enumerate(STEPS):
                    activities.append({
                        "id": f"Z{z}-B{b}-S{s}-{code}",
                        "zone": f"Z{z}", "building": f"B{b}",
                        "segment": f"S{s}",
                        "name": name, "order": order,
                        "duration_days": rng.choice([2, 3, 4]),
                        "resources": {trade: rng.randint(4, 9)},
                        "predecessors": [],
                    })
    by_id = {a["id"]: a for a in activities}
    for z in (1, 2):
        for b in (1, 2):
            # 段间流水：上一段混凝土完成后下一段才开挖
            by_id[f"Z{z}-B{b}-S2-FND"]["predecessors"].append(
                f"Z{z}-B{b}-S1-CNC")
        # 跨栋工种流水：钢筋班组由 B1-S2 转到 B2-S1
        by_id[f"Z{z}-B2-S1-RBR"]["predecessors"].append(
            f"Z{z}-B1-S2-RBR")
    return activities


def _record(a, duration_value):
    return {"id": a["id"], "name": a["name"], "order": a["order"],
            "duration": duration_value, "resources": a["resources"],
            "predecessors": list(a["predecessors"])}


def _flat_segments(activities, duration_of):
    segs = {}
    for a in activities:
        key = (a["zone"], a["building"], a["segment"])
        segs.setdefault(key, []).append(a)
    return [{"zone": z, "building": b, "segment": s,
             "activities": [_record(a, duration_of(a)) for a in acts]}
            for (z, b, s), acts in sorted(segs.items())]


def build_v1(activities):
    return {"meta": {"schema": "v1", "time_unit": "s", "duration_unit": "h"},
            "project": {"id": PROJECT_ID, "name": PROJECT_NAME,
                        "planned_start": int(PLANNED_START.timestamp())},
            "segments": _flat_segments(
                activities, lambda a: a["duration_days"] * 24),
            "resource_caps": CAPS}


def build_v2(activities):
    zones = {}
    for a in activities:
        zones.setdefault(a["zone"], {}).setdefault(
            a["building"], {}).setdefault(a["segment"], []).append(a)
    zone_list = []
    for zid in sorted(zones):
        buildings = []
        for bid in sorted(zones[zid]):
            segments = [{"id": sid,
                         "activities": [_record(a, a["duration_days"] * 24)
                                        for a in zones[zid][bid][sid]]}
                        for sid in sorted(zones[zid][bid])]
            buildings.append({"id": bid, "segments": segments})
        zone_list.append({"id": zid, "buildings": buildings})
    return {"meta": {"schema": "v2", "time_unit": "ms", "duration_unit": "h"},
            "project": {"id": PROJECT_ID, "name": PROJECT_NAME,
                        "planned_start": int(PLANNED_START.timestamp() * 1000)},
            "zones": zone_list,
            "resource_caps": CAPS}


def build_v3(activities):
    return {"meta": {"schema": "v3", "time_unit": "s", "duration_unit": "d"},
            "project": {"id": PROJECT_ID, "name": PROJECT_NAME,
                        "planned_start": int(PLANNED_START.timestamp())},
            "segments": _flat_segments(
                activities, lambda a: a["duration_days"]),
            "resource_caps": CAPS}


def build_events():
    """基于基线计算构造反馈序列：关键工序延后、非关键工序提前、供应下调。"""
    from .cpm import backward, build_edges, critical_paths, forward
    from .normalize import normalize
    project = normalize(build_v1(canonical_activities()))
    edges = build_edges(project)
    forward(project)
    backward(project, edges)
    paths = critical_paths(project, edges)
    delayed = paths[0][len(paths[0]) // 2]
    non_critical = next(a.activity_id for a in project.activities.values()
                        if not a.critical)
    acts = project.activities
    return [
        {"seq": 1, "type": "delayed_finish", "activity": delayed,
         "actual_finish_ms": acts[delayed].ef_ms + 2 * DAY_MS,
         "note": "关键工序延后 2 天完成"},
        {"seq": 2, "type": "early_finish", "activity": non_critical,
         "actual_finish_ms": acts[non_critical].ef_ms - DAY_MS,
         "note": "非关键工序提前 1 天完成"},
        {"seq": 3, "type": "resource_change", "trade": "钢筋工",
         "new_cap": 6, "note": "钢筋工日供应上限下调至 6（低于在途需求）"},
    ]


def build_boundary_cycle():
    acts = []
    for i, (aid, pred) in enumerate([("CYC-A", "CYC-C"), ("CYC-B", "CYC-A"),
                                     ("CYC-C", "CYC-B")]):
        acts.append({"zone": "Z9", "building": "B9", "segment": f"S{i + 1}",
                     "activities": [{"id": aid, "name": f"环路工序{i}",
                                     "order": 0, "duration": 24,
                                     "resources": {"土方工": 1},
                                     "predecessors": [pred]}]})
    return {"meta": {"schema": "v1", "time_unit": "s", "duration_unit": "h"},
            "project": {"id": "boundary-cycle", "name": "边界-依赖成环",
                        "planned_start": int(PLANNED_START.timestamp())},
            "segments": acts, "resource_caps": CAPS}


def build_boundary_missing_unit():
    doc = build_v1(canonical_activities())
    doc["project"] = {"id": "boundary-missing-unit", "name": "边界-单位缺失",
                      "planned_start": int(PLANNED_START.timestamp())}
    del doc["meta"]["duration_unit"]
    return doc


def build_boundary_unknown_schema():
    doc = build_v1(canonical_activities())
    doc["project"] = {"id": "boundary-unknown", "name": "边界-未知版本",
                      "planned_start": int(PLANNED_START.timestamp())}
    doc["meta"]["schema"] = "v9"
    return doc


def generate_samples(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    activities = canonical_activities()
    docs = {
        "project_v1.json": build_v1(activities),
        "project_v2.json": build_v2(activities),
        "project_v3.json": build_v3(activities),
        "events.json": build_events(),
        "boundary_cycle.json": build_boundary_cycle(),
        "boundary_missing_unit.json": build_boundary_missing_unit(),
        "boundary_unknown_schema.json": build_boundary_unknown_schema(),
    }
    paths = []
    for name, doc in docs.items():
        p = out / name
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        paths.append(p)
    return paths
