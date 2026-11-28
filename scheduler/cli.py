"""命令行入口：装载、计算、反馈、一致性核对与施工现场查询。"""
import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .cpm import (backward, build_edges, critical_causes, critical_paths,
                  forward)
from .errors import BusinessError
from .feedback import apply_event, consistency_report
from .normalize import normalize
from .resources import daily_curve

DAY_MS = 86_400_000


def fmt_ms(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


def fmt_float(ms):
    return f"{ms / 3_600_000:.1f}h"


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_business_error(err):
    print(f"业务错误 {err.code}: {err.message}")
    if err.details:
        print(json.dumps(err.details, ensure_ascii=False, indent=2))


def cmd_generate(args):
    from .generator import generate_samples
    for p in generate_samples(args.out):
        print(f"已生成 {p}")
    return 0


def cmd_load(args):
    doc = _read_json(args.file)
    project = normalize(doc)
    conn = store.connect(args.db)
    store.save_project(conn, project)
    segs = sorted({a.segment_id for a in project.activities.values()})
    print(f"工程 {project.project_id}（{project.name}）已装载，"
          f"schema={project.schema}")
    print(f"  计划开始: {fmt_ms(project.planned_start_ms)}")
    print(f"  施工段 {len(segs)} 个，工序 {len(project.activities)} 道")
    print(f"  工种供应上限: {json.dumps(project.caps, ensure_ascii=False)}")
    return 0


def _compute(project):
    edges = build_edges(project)
    forward(project)
    project_end = backward(project, edges)
    return edges, project_end


def cmd_compute(args):
    conn = store.connect(args.db)
    project = store.load_project(conn, args.project)
    edges, project_end = _compute(project)
    sid = store.save_snapshot(conn, project, label="baseline")
    paths = critical_paths(project, edges)
    causes = critical_causes(project, edges)
    print(f"基线快照 #{sid} 已保存，完工时刻 {fmt_ms(project_end)}")
    print("\n逐工序时间参数（ES/EF/浮动/关键/计算前驱）:")
    for aid in sorted(project.activities):
        a = project.activities[aid]
        mark = "关键" if a.critical else "    "
        print(f"  {aid} [{mark}] ES={fmt_ms(a.es_ms)} "
              f"EF={fmt_ms(a.ef_ms)} 浮动={fmt_float(a.float_ms)} "
              f"前驱={a.trace_predecessors}")
    print("\n关键路径:")
    for i, path in enumerate(paths, 1):
        print(f"  路径{i}: {' -> '.join(path)}")
    print("\n关键工序成因:")
    for aid in sorted(causes):
        print(f"  {aid}: {causes[aid]}")
    curve = daily_curve(project)
    overs = [(k, v) for k, v in sorted(curve.items()) if v["over"]
             ]
    print("\n资源超限:")
    if not overs:
        print("  无")
    for (day, trade), slot in overs:
        print(f"  {day} {trade}: 需求 {slot['demand']:.0f} > "
              f"上限 {slot['cap']:.0f}，涉及 {slot['activities']}")
    return 0


def cmd_feedback(args):
    conn = store.connect(args.db)
    sid = store.latest_snapshot_id(conn, args.project)
    project = store.load_project(conn, args.project, snapshot_id=sid)
    if sid is not None:
        print(f"从最近快照 #{sid} 续算")
    events = _read_json(args.file)
    for event in events:
        result = apply_event(project, event)
        sid = store.save_snapshot(
            conn, project,
            label=f"event-{event.get('seq', '?')}-{event['type']}")
        store.record_event(conn, args.project, sid, result)
        print(f"\n事件 {event.get('seq', '?')} [{event['type']}]"
              f" {event.get('note', '')} -> 快照 #{sid}")
        if event["type"] in ("early_finish", "delayed_finish"):
            print(f"  受影响工序({len(result['affected'])}): "
                  f"{result['affected']}")
            print("  传播经过的依赖边:")
            for e in result["edges"]:
                print(f"    {e['pred']} -> {e['succ']} ({e['kind']})")
            print("  增量重算结果:")
            if not result["changes"]:
                print("    无工序时间参数变化")
            for c in result["changes"]:
                print(f"    {c['activity']}: "
                      f"ES {fmt_ms(c['old_es_ms'])} -> "
                      f"{fmt_ms(c['new_es_ms'])}, "
                      f"EF {fmt_ms(c['old_ef_ms'])} -> "
                      f"{fmt_ms(c['new_ef_ms'])}")
        else:
            print(f"  工种 {event['trade']} 供应上限调整为 "
                  f"{event['new_cap']}")
        if result["over_capacity"]:
            print("  仍超出供应上限的日期:")
            for o in result["over_capacity"]:
                print(f"    {o['day']} {o['trade']}: 需求 {o['demand']:.0f}"
                      f" > 上限 {o['cap']:.0f}，涉及 {o['activities']}")
        else:
            print("  当前无资源超限")
    return 0


def cmd_check(args):
    conn = store.connect(args.db)
    base = store.load_project(conn, args.project)
    _compute(base)  # 先重建基线时间参数，再施加事件序列
    events = store.load_event_payloads(conn, args.project)
    incremental = copy.deepcopy(base)
    for event in events:
        apply_event(incremental, event)
    report = consistency_report(incremental, daily_curve(incremental),
                                base, events)
    print("逐工序一致性核对（增量 vs 全量）:")
    for line in report["activities"]:
        mark = "一致" if line["match"] else "不一致"
        inc = line["incremental"]
        print(f"  [{mark}] {line['activity']}: "
              f"ES={fmt_ms(inc['es_ms'])} EF={fmt_ms(inc['ef_ms'])} "
              f"关键={inc['critical']}")
    print("\n逐日逐工种资源超限标记核对:")
    for line in report["resources"]:
        mark = "一致" if line["match"] else "不一致"
        print(f"  [{mark}] {line['day']} {line['trade']}")
    print("\n关键路径核对:")
    print(f"  增量: {report['critical_paths_incremental']}")
    print(f"  全量: {report['critical_paths_full']}")
    print("  关键路径"
          f"{'一致' if report['critical_paths_match'] else '不一致'}")
    print(f"\n总体: {'全部逐项一致' if report['ok'] else '存在不一致项'}")
    return 0 if report["ok"] else 1


def _parse_day(text):
    dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def cmd_query(args):
    conn = store.connect(args.db)
    sid = store.latest_snapshot_id(conn, args.project)
    project = store.load_project(conn, args.project, snapshot_id=sid)
    from_ms = _parse_day(args.from_day)
    to_ms = _parse_day(args.to_day) + DAY_MS
    print(f"快照 #{sid} | 施工段 {args.segment} | "
          f"{args.from_day} ~ {args.to_day}")
    hits = [a for a in project.activities.values()
            if a.segment_id == args.segment
            and a.es_ms < to_ms and a.ef_ms > from_ms]
    if not hits:
        print("  该时段内无进行中的工序")
        return 0
    for a in sorted(hits, key=lambda x: x.es_ms):
        mark = "关键路径" if a.critical else "非关键"
        res = ", ".join(f"{t}:{q:.0f}人/日" for t, q in a.resources.items())
        print(f"  {a.activity_id} {a.name} [{mark}] "
              f"剩余浮动 {fmt_float(a.float_ms)}")
        print(f"    {fmt_ms(a.es_ms)} ~ {fmt_ms(a.ef_ms)} 需求: {res}")
    return 0


def cmd_show(args):
    conn = store.connect(args.db)
    project = store.load_project(conn, args.project)
    a = project.activities.get(args.activity)
    if a is None:
        from .errors import E_NOT_FOUND
        raise BusinessError(E_NOT_FOUND, f"工序不存在: {args.activity}")
    print(f"工序 {a.activity_id}（{a.name}）施工段 {a.segment_id} "
          f"工期 {a.duration_hours:.0f}h")
    print("字段来源（路径 / schema 版本）:")
    for field, prov in a.provenance.items():
        print(f"  {field}: {prov['path']} (schema {prov['schema']})")
    return 0


def cmd_history(args):
    conn = store.connect(args.db)
    history = store.list_history(conn, args.project)
    print("基线与增量快照:")
    for s in history["snapshots"]:
        print(f"  快照 #{s['snapshot_id']} [{s['label']}] {s['created_at']}")
    print("\n增量重排记录:")
    if not history["events"]:
        print("  无")
    for e in history["events"]:
        ev = e["event"]
        print(f"  事件 #{e['event_id']} [{ev['type']}] -> 快照 "
              f"#{e['snapshot_id']}，受影响 {len(e['affected'])} 道，"
              f"时间参数变化 {len(e['changes'])} 道")
    return 0


def cmd_demo(args):
    from .generator import generate_samples
    out = Path("samples")
    generate_samples(out)
    db = Path("demo.db")
    if db.exists():
        db.unlink()
    ns = argparse.Namespace
    print("== 1. 生成确定性样本 ==")
    cmd_generate(ns(out=str(out)))
    print("\n== 2. 装载 v1 工程 ==")
    cmd_load(ns(db=str(db), file=str(out / "project_v1.json")))
    print("\n== 3. 正向/反向计算与关键路径 ==")
    cmd_compute(ns(db=str(db), project="demo"))
    print("\n== 4. 应用反馈事件（增量重排） ==")
    cmd_feedback(ns(db=str(db), project="demo",
                    file=str(out / "events.json")))
    print("\n== 5. 增量 vs 全量一致性核对 ==")
    rc = cmd_check(ns(db=str(db), project="demo"))
    print("\n== 6. 现场查询示例 ==")
    cmd_query(ns(db=str(db), project="demo", segment="Z1/B1/S1",
                 from_day="2026-03-01", to_day="2026-03-20"))
    print("\n== 7. 历史快照与增量记录 ==")
    cmd_history(ns(db=str(db), project="demo"))
    return rc


def build_parser():
    parser = argparse.ArgumentParser(
        prog="scheduler", description="施工进度协同排布工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="生成确定性样本")
    p.add_argument("--out", default="samples")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("load", help="装载并归一工程文档")
    p.add_argument("--db", default="scheduler.db")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("compute", help="正反向计算、关键路径与资源曲线")
    p.add_argument("--db", default="scheduler.db")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_compute)

    p = sub.add_parser("feedback", help="应用反馈事件并增量重排")
    p.add_argument("--db", default="scheduler.db")
    p.add_argument("--project", required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("check", help="增量与全量重排逐工序一致性核对")
    p.add_argument("--db", default="scheduler.db")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("query", help="按时段查询施工段工序与资源")
    p.add_argument("--db", default="scheduler.db")
    p.add_argument("--project", required=True)
    p.add_argument("--segment", required=True)
    p.add_argument("--from", dest="from_day", required=True)
    p.add_argument("--to", dest="to_day", required=True)
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("show", help="查看工序字段来源与参数")
    p.add_argument("--db", default="scheduler.db")
    p.add_argument("--project", required=True)
    p.add_argument("--activity", required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("history", help="回看基线快照与增量重排记录")
    p.add_argument("--db", default="scheduler.db")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("demo", help="端到端演示")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BusinessError as err:
        _print_business_error(err)
        return 2


if __name__ == "__main__":
    sys.exit(main())
