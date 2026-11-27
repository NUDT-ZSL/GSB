"""Command-line interface for the clock halftone laboratory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .api import run_pipeline
from .config import load_config, validate_config
from .errors import BusinessError
from .generator import append_sample_catalog, generate_clock_sample
from .io import write_json
from .recompute import recompute_part
from .validation import build_input_analysis, load_and_validate


def _targets(values: list[str] | None) -> dict[int, float]:
    result: dict[int, float] = {}
    for value in values or []:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if "=" not in token:
                raise BusinessError(
                    "E_DENSITY_BOUNDS",
                    f"目标密度格式必须为 label=value: {token}",
                )
            label_text, density_text = token.split("=", 1)
            try:
                label = int(label_text)
                density = float(density_text)
            except ValueError as exc:
                raise BusinessError(
                    "E_DENSITY_BOUNDS",
                    f"目标密度格式无效: {token}",
                ) from exc
            result[label] = density
    return result


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _append_catalog(path: str | Path | None, entry: dict[str, Any]) -> None:
    if path is None:
        return
    catalog_path = Path(path)
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    else:
        catalog = {"schema": "clock-halftone-snapshot-catalog/v1", "snapshots": []}
    catalog.setdefault("snapshots", [])
    catalog["snapshots"] = [
        item for item in catalog["snapshots"] if item.get("entry_id") != entry.get("entry_id")
    ]
    catalog["snapshots"].append(entry)
    catalog["snapshots"].sort(key=lambda item: item.get("entry_id", ""))
    write_json(catalog_path, catalog)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clock-halftone", description="确定性钟表机芯半色调教学展具内核")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="生成固定种子的机芯灰阶样本")
    generate.add_argument("--out", default=None, help="样本输出目录")
    generate.add_argument("--seed", type=int, default=20261128)
    generate.add_argument("--size", type=int, default=512)
    generate.add_argument("--catalog", default="samples/catalog.json")

    inspect = sub.add_parser("inspect", help="校验输入并输出解析清单")
    inspect.add_argument("--sample", required=True)
    inspect.add_argument("--config", default="configs/default.json")
    inspect.add_argument("--out", default=None)
    inspect.add_argument("--verify-hashes", action="store_true")

    run = sub.add_parser("run", help="生成半色调快照和全部报告")
    run.add_argument("--sample", required=True)
    run.add_argument("--config", default="configs/default.json")
    run.add_argument("--out", required=True)
    run.add_argument("--snapshot-id", default=None)
    run.add_argument("--target-density", action="append", default=None, help="label=value，可重复")
    run.add_argument("--catalog", default="snapshots/catalog.json")

    recompute = sub.add_parser("recompute", help="修改单零件目标密度并局部重算")
    recompute.add_argument("--sample", required=True)
    recompute.add_argument("--config", default="configs/default.json")
    recompute.add_argument("--snapshot", required=True)
    recompute.add_argument("--part", type=int, required=True)
    recompute.add_argument("--density", type=float, required=True)
    recompute.add_argument("--out", required=True)
    recompute.add_argument("--verify-full", dest="verify_full", action="store_true", default=True)
    recompute.add_argument("--no-verify-full", dest="verify_full", action="store_false")
    recompute.add_argument("--catalog", default="snapshots/catalog.json")

    demo = sub.add_parser("demo", help="生成样本、两种打印线数快照及一次局部重算")
    demo.add_argument("--sample-dir", default="samples/watch_movement_seed_20261128")
    demo.add_argument("--snapshot-root", default="snapshots")
    demo.add_argument("--seed", type=int, default=20261128)
    demo.add_argument("--size", type=int, default=512)
    return parser


def command_generate(args: argparse.Namespace) -> dict[str, Any]:
    out = args.out or f"samples/watch_movement_seed_{args.seed}"
    result = generate_clock_sample(out, seed=args.seed, size=args.size)
    append_sample_catalog(args.catalog, result)
    return {"ok": True, "command": "generate", **result, "catalog": args.catalog}


def command_inspect(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    validate_config(config)
    bundle = load_and_validate(args.sample, config, verify_hashes=args.verify_hashes)
    analysis = build_input_analysis(bundle)
    if args.verify_hashes:
        analysis["checks"]["hash_verification"] = "requested"
    if args.out:
        write_json(args.out, analysis)
    return {"ok": True, "command": "inspect", "analysis": analysis, "out": args.out}


def command_run(args: argparse.Namespace) -> dict[str, Any]:
    result = run_pipeline(
        args.sample,
        config_path=args.config,
        output_dir=args.out,
        snapshot_id=args.snapshot_id,
        target_overrides=_targets(args.target_density),
        catalog_path=args.catalog,
    )
    return {"command": "run", **result}


def command_recompute(args: argparse.Namespace) -> dict[str, Any]:
    result = recompute_part(
        args.sample,
        args.config,
        args.snapshot,
        args.out,
        part_label=args.part,
        new_target_density=args.density,
        verify_full=args.verify_full,
    )
    _append_catalog(
        args.catalog,
        {
            "entry_id": f"local_{args.snapshot}_part{args.part}_{args.density:.4f}",
            "kind": "local_recompute",
            "sample_id": result.get("sample_id"),
            "snapshot_dir": str(Path(args.out).resolve()),
            "part_label": args.part,
            "new_target_density": args.density,
            "ok": result.get("ok"),
            "diff_report": str((Path(args.out) / "recompute_diff.json").resolve()),
        },
    )
    return {"command": "recompute", **result}


def command_demo(args: argparse.Namespace) -> dict[str, Any]:
    sample_dir = Path(args.sample_dir)
    if not (sample_dir / "sample.json").exists():
        generated = generate_clock_sample(sample_dir, seed=args.seed, size=args.size)
        append_sample_catalog("samples/catalog.json", generated)
    else:
        generated = {"sample_dir": str(sample_dir.resolve()), "sample_id": sample_dir.name}
    snapshot_root = Path(args.snapshot_root)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    catalog = snapshot_root / "catalog.json"
    default_snapshot = run_pipeline(
        sample_dir,
        config_path="configs/default.json",
        output_dir=snapshot_root / "screen_50lpi_default_density",
        snapshot_id=f"{generated.get('sample_id', 'sample')}_50lpi_default",
        catalog_path=catalog,
    )
    fine_snapshot = run_pipeline(
        sample_dir,
        config_path="configs/fine_75lpi.json",
        output_dir=snapshot_root / "screen_75lpi_default_density",
        snapshot_id=f"{generated.get('sample_id', 'sample')}_75lpi_default",
        catalog_path=catalog,
    )
    local = recompute_part(
        sample_dir,
        "configs/default.json",
        snapshot_root / "screen_50lpi_default_density",
        snapshot_root / "local_recompute_escape_density_boost",
        part_label=1,
        new_target_density=0.615,
        verify_full=True,
    )
    _append_catalog(
        catalog,
        {
            "entry_id": f"local_{local.get('part_label')}_{local.get('new_target_density'):.4f}",
            "kind": "local_recompute",
            "sample_id": local.get("sample_id"),
            "snapshot_dir": str((snapshot_root / "local_recompute_escape_density_boost").resolve()),
            "part_label": local.get("part_label"),
            "new_target_density": local.get("new_target_density"),
            "ok": local.get("ok"),
            "diff_report": str((snapshot_root / "local_recompute_escape_density_boost" / "recompute_diff.json").resolve()),
        },
    )
    return {
        "ok": bool(default_snapshot["ok"] and fine_snapshot["ok"] and local["ok"]),
        "command": "demo",
        "sample": generated,
        "snapshots": [default_snapshot, fine_snapshot],
        "local_recompute": local,
        "catalog": str(catalog.resolve()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            payload = command_generate(args)
        elif args.command == "inspect":
            payload = command_inspect(args)
        elif args.command == "run":
            payload = command_run(args)
        elif args.command == "recompute":
            payload = command_recompute(args)
        elif args.command == "demo":
            payload = command_demo(args)
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
    except BusinessError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    _print(payload)
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
