"""matgen 命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import EXIT_GRACEFUL_STOP, GenerationEngine, StopController
from .errors import ErrorCode
from .injection import apply_input_injections, corrupt_persisted_chunk, load_injections
from .model import GenConfig, load_config, load_materials
from .samplegen import generate_samples


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def cmd_make_samples(args) -> int:
    paths = generate_samples(args.sample_dir)
    print(json.dumps(paths, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args) -> int:
    config = load_config(args.config)
    specs, errors = load_materials(args.materials, config)
    payload = {
        "valid_materials": [spec.material_id for spec in specs],
        "errors": [error.to_dict() for error in errors],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def _load_payloads(materials_path: Path, config_path: Path, injection_path: Path | None):
    material_payload = json.loads(materials_path.read_text(encoding="utf-8"))
    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    applied: list[dict] = []
    if injection_path is not None:
        injections = load_injections(injection_path)
        applied = apply_input_injections(material_payload, config_payload, injections)
    return material_payload, config_payload, applied


def cmd_run(args) -> int:
    materials_path = Path(args.materials)
    config_path = Path(args.config)
    material_payload, config_payload, applied = _load_payloads(
        materials_path, config_path, Path(args.injections) if args.injections else None
    )

    config = GenConfig(
        grid_rows=config_payload.get("grid_rows", 2),
        grid_cols=config_payload.get("grid_cols", 2),
        concurrency=args.concurrency or config_payload.get("concurrency", 2),
        memory_budget_bytes=args.memory_budget or config_payload.get("memory_budget_bytes", 64 * 1024 * 1024),
        overlap=config_payload.get("overlap", 2),
        tolerance=config_payload.get("tolerance", 1e-6),
        output_dir=args.output_dir or config_payload.get("output_dir", "build/output"),
        state_dir=args.state_dir or config_payload.get("state_dir", "build/state"),
        archive_dir=args.archive_dir or config_payload.get("archive_dir", "build/archive"),
        dtype=config_payload.get("dtype", "float32"),
    )

    # 输入注入通过临时内存文件交给标准校验链路，源码与原始样本不被修改。
    temp_dir = Path(config.state_dir) / "_inbox"
    temp_dir.mkdir(parents=True, exist_ok=True)
    injected_materials_path = temp_dir / "materials.injected.json"
    _write_json(injected_materials_path, material_payload)

    specs, validation_errors = load_materials(injected_materials_path, config)
    stop = StopController(stop_flag_path=args.stop_flag)
    stop.install_signal_handlers()
    engine = GenerationEngine(config, stop_controller=stop, force_crash_after=args.force_crash_after)

    run_records = []
    exit_code = 0
    produced_tiles = 0
    for spec in specs:
        result = engine.run_material(spec, config.state_dir, config.output_dir, config.archive_dir)
        produced_tiles += result.tile_count
        run_records.append(result.to_dict())
        if result.status == "interrupted":
            exit_code = EXIT_GRACEFUL_STOP
            break

    report = {
        "injections": applied,
        "validation_errors": [error.to_dict() for error in validation_errors],
        "materials": run_records,
        "produced_tile_count": produced_tiles,
    }
    report_path = Path(config.output_dir) / "run_report.json"
    _write_json(report_path, report)
    print(json.dumps({"report": str(report_path), "produced_tile_count": produced_tiles}, ensure_ascii=False, indent=2))
    return exit_code


def cmd_recover(args) -> int:
    # 恢复等价于用相同输入再次 run；版本标识一致时自动复用有效块。
    args.force_crash_after = None
    return cmd_run(args)


def cmd_request_stop(args) -> int:
    Path(args.stop_flag).parent.mkdir(parents=True, exist_ok=True)
    Path(args.stop_flag).write_text("stop\n", encoding="utf-8")
    print(f"stop flag written: {args.stop_flag}")
    return 0


def cmd_corrupt_chunk(args) -> int:
    path = corrupt_persisted_chunk(args.state_dir, args.material_id, args.chunk_index)
    print(json.dumps({"corrupted": str(path)}, ensure_ascii=False, indent=2))
    return 0


def cmd_archive_list(args) -> int:
    archive_root = Path(args.archive_dir)
    items = [str(path) for path in sorted(archive_root.rglob("version.json"))] if archive_root.exists() else []
    print(json.dumps({"archived_outputs": items}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="matgen", description="确定性程序化材质纹理生成工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("make-samples", help="生成固定种子样本")
    p.add_argument("--sample-dir", default="samples/fixed")
    p.set_defaults(func=cmd_make_samples)

    p = sub.add_parser("validate", help="仅校验材质清单")
    p.add_argument("--materials", required=True)
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_validate)

    def add_run_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--materials", required=True)
        sp.add_argument("--config", required=True)
        sp.add_argument("--injections")
        sp.add_argument("--output-dir")
        sp.add_argument("--state-dir")
        sp.add_argument("--archive-dir")
        sp.add_argument("--concurrency", type=int)
        sp.add_argument("--memory-budget", type=int)
        sp.add_argument("--stop-flag")
        sp.add_argument("--force-crash-after", type=int, help=argparse.SUPPRESS)

    p = sub.add_parser("run", help="执行生成链路")
    add_run_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("recover", help="按状态目录恢复并继续生成")
    add_run_args(p)
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("request-stop", help="写入可处理终止标记文件")
    p.add_argument("--stop-flag", required=True)
    p.set_defaults(func=cmd_request_stop)

    p = sub.add_parser("corrupt-chunk", help="损坏指定已持久化块摘要（异常注入）")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--material-id", required=True)
    p.add_argument("--chunk-index", type=int, required=True)
    p.set_defaults(func=cmd_corrupt_chunk)

    p = sub.add_parser("archive-list", help="列出被归档的旧版本输出")
    p.add_argument("--archive-dir", required=True)
    p.set_defaults(func=cmd_archive_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"code": ErrorCode.E_CONFIG_INVALID, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
