"""Command-line entry point for the deterministic metering storage demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .database import ChunkStore
from .generator import SampleDataset, generate_dataset, write_dataset
from .verification import verify_store

DEFAULT_DB = Path("data/chunk_index.sqlite3")
DEFAULT_SAMPLES = Path("data/samples")
DEFAULT_REPORTS = Path("data/reports")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def _remove_database(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            candidate.unlink()


def _percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def _print_occupancy(label: str, occupancy: dict[str, Any]) -> None:
    print(
        f"{label}: 逻辑引用={occupancy['logical_active_bytes']} B, "
        f"物理存活={occupancy['physical_live_bytes']} B, "
        f"去重节省={occupancy['deduplicated_saved_bytes']} B, "
        f"可回收块={occupancy['reclaimable_blocks']} 个/"
        f"{occupancy['reclaimable_bytes']} B"
    )


def _print_write(result: dict[str, Any], event_kind: str) -> None:
    print(
        f"[{result['event_seq']:>3}] {event_kind:<14} {result['file_id']} "
        f"policy={result['policy_id']} bytes={result['content_size']} "
        f"records={result['record_count']} chunks={result['chunk_count']} "
        f"hit={result['dedup_hit_chunks']} new={result['new_chunks']} "
        f"logical={result['logical_bytes']} stored={result['newly_stored_bytes']}"
    )
    for chunk in result["chunks"]:
        print(
            f"      #{chunk['chunk_index']:02d} "
            f"[{chunk['offset']}:{chunk['offset'] + chunk['length']}) "
            f"len={chunk['length']} sha256={chunk['digest']} "
            f"{'HIT' if chunk['hit'] else 'NEW'} "
            f"ref {chunk['ref_before']}->{chunk['ref_after']}"
        )


def _print_delete(result: dict[str, Any]) -> None:
    print(
        f"[{result['event_seq']:>3}] delete         {result['file_id']} "
        f"chunks={result['chunk_count']} reclaimed={result['reclaimed_chunk_count']} "
        f"bytes={result['reclaimed_bytes']} "
        f"physical {result['occupancy_before']['physical_live_bytes']}"
        f"->{result['occupancy_after']['physical_live_bytes']}"
    )
    for chunk in result["chunks"]:
        suffix = " RECLAIMED" if chunk["reclaimed"] else ""
        print(
            f"      #{chunk['chunk_index']:02d} "
            f"ref {chunk['ref_before']}->{chunk['ref_after']}{suffix} "
            f"sha256={chunk['digest']}"
        )


def _print_metrics(metrics: list[dict[str, Any]]) -> None:
    for metric in metrics:
        active = "*" if metric["active"] else " "
        print(
            f"  {active} policy={metric['policy_id']} {metric['label']:<14} "
            f"target={metric['target_size']:<5} mask={metric['mask_bits']:<2} "
            f"writes={metric['writes']:<2} refs={metric['chunk_refs']:<3} "
            f"hits={metric['hit_refs']:<3} hit_rate={_percent(metric['hit_rate'])} "
            f"dedupe_rate={_percent(metric['dedupe_rate'])} "
            f"saved={metric['saved_bytes']} B"
        )


def _replay_demo(
    *,
    dataset: SampleDataset,
    db_path: Path,
    samples_dir: Path,
    reports_dir: Path,
    reset: bool,
) -> tuple[int, dict[str, Any]]:
    if reset:
        _remove_database(db_path)
    elif db_path.exists():
        return 2, {"error": f"数据库已存在，未指定 --reset：{db_path}"}

    manifest = write_dataset(dataset, samples_dir)
    initial_policy = dataset.policies[0]
    reports_dir.mkdir(parents=True, exist_ok=True)
    operations: list[dict[str, Any]] = []
    policy_ids_by_index: dict[int, int] = {}

    print(
        f"样本：seed={dataset.seed}，台区={len(dataset.areas)}，"
        f"文件={len(dataset.files)}，事件={len(dataset.events)}，"
        f"基础记录流={len(dataset.all_base_records)} B"
    )
    print(f"样本目录：{samples_dir}")
    print(f"块索引：{db_path}")

    with ChunkStore(
        db_path,
        initial_target_size=int(initial_policy["target_size"]),
        initial_mask_bits=int(initial_policy["mask_bits"]),
    ) as store:
        created_policy = store.active_policy()
        if created_policy is None:
            raise RuntimeError("initial policy was not created")
        policy_ids_by_index[0] = int(created_policy["policy_id"])

        for event in dataset.events:
            kind = event.kind
            if kind in {"write", "duplicate", "near_duplicate"}:
                if event.file_id is None:
                    raise RuntimeError("write event is missing file_id")
                source_file = dataset.files[event.file_id]
                result = store.write_file(
                    event.file_id,
                    source_file.data,
                    logical_time=event.logical_time,
                )
                _print_write(result, kind)
                operations.append(
                    {
                        "manifest_event": event.to_dict(),
                        "kind": kind,
                        "result": result,
                    }
                )
            elif kind == "switch_policy":
                if event.policy_index is None:
                    raise RuntimeError("switch policy event is missing policy_index")
                policy_spec = dataset.policies[event.policy_index]
                policy = store.switch_policy(
                    target_size=int(policy_spec["target_size"]),
                    mask_bits=int(policy_spec["mask_bits"]),
                    logical_time=event.logical_time,
                    label=str(policy_spec["label"]),
                )
                policy_ids_by_index[event.policy_index] = int(policy["policy_id"])
                print(
                    f"[{event.logical_time}] switch_policy -> "
                    f"policy={policy['policy_id']} target={policy['target_size']} "
                    f"mask={policy['mask_bits']}"
                )
                operations.append(
                    {
                        "manifest_event": event.to_dict(),
                        "kind": kind,
                        "result": policy,
                    }
                )
            elif kind == "delete":
                if event.file_id is None:
                    raise RuntimeError("delete event is missing file_id")
                result = store.delete_file(
                    event.file_id,
                    logical_time=event.logical_time,
                )
                _print_delete(result)
                operations.append(
                    {
                        "manifest_event": event.to_dict(),
                        "kind": kind,
                        "result": result,
                    }
                )
            else:
                raise RuntimeError(f"unsupported sample event kind: {kind}")

        metrics = store.strategy_metrics()
        occupancy = store.occupancy()
        consistency = store.consistency_report()
        ledger = store.reclaim_ledger()
        verification = verify_store(store)

        print("\n策略对比：")
        _print_metrics(metrics)
        print("\n最终占用：")
        _print_occupancy("  占用", occupancy)
        print(
            f"  文件={occupancy['active_files']}，逻辑映射="
            f"{occupancy['active_mappings']}，总引用="
            f"{occupancy['total_block_refs']}"
        )
        print(
            f"\n回收账目：递减引用={ledger['total_decremented_references']}，"
            f"归零块={ledger['total_reclaimed_chunks']}，"
            f"回收字节={ledger['total_reclaimed_bytes']}"
        )
        print(f"一致性：{'OK' if consistency['ok'] else 'FAILED'}")
        print(
            f"独立复算/重放：{'OK' if verification['ok'] else 'FAILED'} "
            f"(files={verification['checked_active_files']}, "
            f"chunks={verification['checked_active_chunks']}, "
            f"records={verification['checked_active_records']})"
        )

        report = {
            "format": "metering-store-demo-report-v1",
            "seed": dataset.seed,
            "sample_manifest": manifest,
            "database": str(db_path),
            "policy_ids_by_manifest_index": policy_ids_by_index,
            "operations": operations,
            "strategy_metrics": metrics,
            "occupancy": occupancy,
            "reclaim_ledger": ledger,
            "consistency": consistency,
            "verification": verification,
        }
        report_path = reports_dir / "demo_report.json"
        ledger_path = reports_dir / "reclaim_ledger.json"
        _write_json(report_path, report)
        _write_json(ledger_path, ledger)
        print(f"\n报告：{report_path}")
        print(f"回收清单：{ledger_path}")
        return (0 if consistency["ok"] and verification["ok"] else 1), report


def _cmd_generate(args: argparse.Namespace) -> int:
    dataset = generate_dataset(
        seed=args.seed,
        target_size=args.target_size,
        mask_bits=args.mask_bits,
        new_target_size=args.new_target_size,
        new_mask_bits=args.new_mask_bits,
    )
    manifest = write_dataset(dataset, args.output)
    print(
        f"已生成：{args.output}；seed={args.seed}，"
        f"台区={len(dataset.areas)}，文件={len(dataset.files)}，"
        f"事件={len(dataset.events)}"
    )
    print(f"基础记录流 SHA-256：{manifest['all_base_records_sha256']}")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    dataset = generate_dataset(
        seed=args.seed,
        target_size=args.target_size,
        mask_bits=args.mask_bits,
        new_target_size=args.new_target_size,
        new_mask_bits=args.new_mask_bits,
    )
    code, payload = _replay_demo(
        dataset=dataset,
        db_path=Path(args.db),
        samples_dir=Path(args.samples),
        reports_dir=Path(args.reports),
        reset=args.reset,
    )
    if code and "error" in payload:
        print(payload["error"], file=sys.stderr)
    return code


def _cmd_write(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    with ChunkStore(args.db) as store:
        result = store.write_file(
            args.file_id,
            path.read_bytes(),
            logical_time=args.logical_time,
        )
        _print_write(result, "write")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    with ChunkStore(args.db) as store:
        result = store.delete_file(args.file_id, logical_time=args.logical_time)
        _print_delete(result)
    return 0


def _cmd_switch_policy(args: argparse.Namespace) -> int:
    with ChunkStore(args.db) as store:
        policy = store.switch_policy(
            target_size=args.target_size,
            mask_bits=args.mask_bits,
            logical_time=args.logical_time,
            label=args.label,
        )
        print(
            f"已切换策略：policy={policy['policy_id']} "
            f"target={policy['target_size']} mask={policy['mask_bits']}"
        )
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        rows = store.search_records(args.area_prefix, args.start_ms, args.end_ms)
        if args.limit is not None:
            rows = rows[: args.limit]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(
                "file_id  record  area   meter       ts_ms          "
                "chunk  offset      digest"
            )
            for row in rows:
                print(
                    f"{row['file_id']:<8} {row['record_index']:>6}  "
                    f"{row['area_id']:<5} {row['meter_id']:<11} {row['ts_ms']:>13}  "
                    f"{row['chunk_index']:>5} {row['chunk_offset']:>7}  "
                    f"{row['digest']}"
                )
            print(f"命中 {len(rows)} 条逻辑记录")
    return 0


def _cmd_files(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        rows = store.list_files(include_deleted=args.include_deleted)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        for row in rows:
            print(
                f"{row['file_id']:<8} {row['status']:<7} "
                f"policy={row['policy_id']} target={row['target_size']} "
                f"mask={row['mask_bits']} records={row['record_count']:<3} "
                f"chunks={row['active_chunk_count']}/{row['chunk_count']} "
                f"bytes={row['content_size']}"
            )
    return 0


def _cmd_file(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        file_info = store.get_file(args.file_id)
        if file_info is None:
            print(f"未知文件：{args.file_id}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(file_info, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        print(
            f"文件 {file_info['file_id']} status={file_info['status']} "
            f"policy={file_info['policy_id']} target={file_info['target_size']} "
            f"mask={file_info['mask_bits']} bytes={file_info['content_size']}"
        )
        for chunk in file_info["chunks"]:
            print(
                f"  #{chunk['chunk_index']:02d} "
                f"[{chunk['offset']}:{chunk['offset'] + chunk['length']}) "
                f"len={chunk['length']} active={chunk['active']} "
                f"hit={chunk['hit']} ref={chunk['ref_count']} "
                f"stored={chunk['stored']} sha256={chunk['digest']}"
            )
    return 0


def _cmd_refs(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        rows = store.files_for_digest(
            args.digest,
            include_deleted=args.include_deleted,
        )
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            for row in rows:
                print(
                    f"{row['file_id']:<8} status={row['status']:<7} "
                    f"chunk={row['chunk_index']} offset={row['offset']} "
                    f"active={row['active']}"
                )
            print(f"引用文件映射 {len(rows)} 条")
    return 0


def _cmd_blocks(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        rows = store.list_blocks(include_reclaimable=not args.live_only)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        for row in rows:
            print(
                f"len={row['length']:<5} refs={row['ref_count']:<2} "
                f"reclaimable={int(row['reclaimable'])} "
                f"stored={int(row['stored'])} policy={row['created_policy_id']} "
                f"sha256={row['digest']}"
            )
        print(f"块索引共 {len(rows)} 条")
    return 0


def _cmd_metrics(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        metrics = store.strategy_metrics()
        if args.json:
            print(json.dumps(metrics, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _print_metrics(metrics)
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        events = store.list_events()
        if args.json:
            print(json.dumps(events, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            for event in events:
                detail = event["detail"]
                extra = ""
                if event["file_id"] is not None:
                    extra = f" file={event['file_id']}"
                if "new_chunks" in detail:
                    extra += (
                        f" hit={detail['dedup_hit_chunks']}"
                        f" new={detail['new_chunks']}"
                    )
                if "reclaimed_bytes" in detail:
                    extra += f" reclaimed={detail['reclaimed_bytes']}"
                print(
                    f"{event['seq']:>3} t={event['logical_time']} "
                    f"{event['kind']:<14}{extra}"
                )
    return 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        ledger = store.reclaim_ledger()
        _write_json(Path(args.output), ledger)
    print(
        f"回收清单已写入 {args.output}：归零块={ledger['total_reclaimed_chunks']}，"
        f"回收字节={ledger['total_reclaimed_bytes']}"
    )
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        report = store.consistency_report()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            for check in report["checks"]:
                print(f"{'OK' if check['ok'] else 'FAILED':<7} {check['name']}")
                if not check["ok"] and "mismatches" in check:
                    print(
                        json.dumps(
                            check["mismatches"],
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
            _print_occupancy("占用", report["occupancy"])
    return 0 if report["ok"] else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    with ChunkStore(args.db, initialize=False) as store:
        report = verify_store(store)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        for check in report["checks"]:
            print(f"{'OK' if check['ok'] else 'FAILED':<7} {check['name']}")
        print(
            f"复算文件={report['checked_active_files']}，"
            f"块={report['checked_active_chunks']}，"
            f"记录={report['checked_active_records']}"
        )
        _print_occupancy("占用", report["occupancy"])
    return 0 if report["ok"] else 1


def _cmd_gui(args: argparse.Namespace) -> int:
    from .gui import launch_diagnostic_view

    return launch_diagnostic_view(args.db)


def _add_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-size", type=int, default=384, help="初始目标块大小（字节）")
    parser.add_argument("--mask-bits", type=int, default=7, help="初始边界掩码位数")
    parser.add_argument(
        "--new-target-size",
        type=int,
        default=768,
        help="策略切换后的目标块大小（字节）",
    )
    parser.add_argument(
        "--new-mask-bits",
        type=int,
        default=9,
        help="策略切换后的边界掩码位数",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m metering_store",
        description="确定性计量记录 CDC 分块、去重、检索与回收演示",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="按固定种子生成确定性样本")
    generate.add_argument("--seed", type=int, default=42)
    generate.add_argument("--output", default=str(DEFAULT_SAMPLES))
    _add_policy_arguments(generate)
    generate.set_defaults(func=_cmd_generate)

    demo = subparsers.add_parser("demo", help="生成样本并回放完整交错事件流")
    demo.add_argument("--seed", type=int, default=42)
    demo.add_argument("--db", default=str(DEFAULT_DB))
    demo.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    demo.add_argument("--reports", default=str(DEFAULT_REPORTS))
    demo.add_argument(
        "--reset",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="回放前清空块索引数据库（默认启用）",
    )
    _add_policy_arguments(demo)
    demo.set_defaults(func=_cmd_demo)

    write = subparsers.add_parser("write", help="使用当前策略写入一个 JSONL 记录文件")
    write.add_argument("--db", default=str(DEFAULT_DB))
    write.add_argument("--file-id", required=True)
    write.add_argument("--path", required=True)
    write.add_argument("--logical-time", type=int, default=0)
    write.set_defaults(func=_cmd_write)

    delete = subparsers.add_parser("delete", help="归档删除文件并递减块引用")
    delete.add_argument("--db", default=str(DEFAULT_DB))
    delete.add_argument("--file-id", required=True)
    delete.add_argument("--logical-time", type=int, default=0)
    delete.set_defaults(func=_cmd_delete)

    switch = subparsers.add_parser("switch-policy", help="切换后续写入使用的分块策略")
    switch.add_argument("--db", default=str(DEFAULT_DB))
    switch.add_argument("--target-size", type=int, required=True)
    switch.add_argument("--mask-bits", type=int, required=True)
    switch.add_argument("--logical-time", type=int, default=0)
    switch.add_argument("--label", default="switched")
    switch.set_defaults(func=_cmd_switch_policy)

    search = subparsers.add_parser("search", help="按台区前缀与毫秒区间检索有效记录")
    search.add_argument("--db", default=str(DEFAULT_DB))
    search.add_argument("--area-prefix", default="")
    search.add_argument("--start-ms", type=int, required=True)
    search.add_argument("--end-ms", type=int, required=True)
    search.add_argument("--limit", type=int)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=_cmd_search)

    files = subparsers.add_parser("files", help="列出文件与策略")
    files.add_argument("--db", default=str(DEFAULT_DB))
    files.add_argument("--include-deleted", action="store_true")
    files.add_argument("--json", action="store_true")
    files.set_defaults(func=_cmd_files)

    file_parser = subparsers.add_parser("file", help="追溯一个文件的全部块")
    file_parser.add_argument("--db", default=str(DEFAULT_DB))
    file_parser.add_argument("--file-id", required=True)
    file_parser.add_argument("--json", action="store_true")
    file_parser.set_defaults(func=_cmd_file)

    refs = subparsers.add_parser("refs", help="按块摘要反查引用文件")
    refs.add_argument("--db", default=str(DEFAULT_DB))
    refs.add_argument("--digest", required=True)
    refs.add_argument("--include-deleted", action="store_true")
    refs.add_argument("--json", action="store_true")
    refs.set_defaults(func=_cmd_refs)

    blocks = subparsers.add_parser("blocks", help="列出块索引")
    blocks.add_argument("--db", default=str(DEFAULT_DB))
    blocks.add_argument("--live-only", action="store_true")
    blocks.add_argument("--json", action="store_true")
    blocks.set_defaults(func=_cmd_blocks)

    metrics = subparsers.add_parser("metrics", help="比较各分块策略的命中率与去重率")
    metrics.add_argument("--db", default=str(DEFAULT_DB))
    metrics.add_argument("--json", action="store_true")
    metrics.set_defaults(func=_cmd_metrics)

    events = subparsers.add_parser("events", help="列出块索引事件账本")
    events.add_argument("--db", default=str(DEFAULT_DB))
    events.add_argument("--json", action="store_true")
    events.set_defaults(func=_cmd_events)

    ledger = subparsers.add_parser("ledger", help="导出回收清单")
    ledger.add_argument("--db", default=str(DEFAULT_DB))
    ledger.add_argument("--output", default=str(DEFAULT_REPORTS / "reclaim_ledger.json"))
    ledger.set_defaults(func=_cmd_ledger)

    check = subparsers.add_parser("check", help="检查引用计数与逻辑映射不变量")
    check.add_argument("--db", default=str(DEFAULT_DB))
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=_cmd_check)

    verify = subparsers.add_parser("verify", help="独立复算块边界/摘要并重放检索")
    verify.add_argument("--db", default=str(DEFAULT_DB))
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=_cmd_verify)

    gui = subparsers.add_parser("gui", help="打开 tkinter 诊断视图")
    gui.add_argument("--db", default=str(DEFAULT_DB))
    gui.set_defaults(func=_cmd_gui)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
