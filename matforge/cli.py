from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from .cache import default_cache_root, probe_cache_writable
from .errors import MatForgeError, fail
from .hashing import atomic_write_json, digest_obj
from .params import MaterialParams
from .pipeline import generate_full
from .plan import BlockPlan
from .png import encode_rgba_png
from .presets import get_preset, preset_names, preset_summaries
from .snapshot import Snapshot, SnapshotStore
from .studio import MaterialStudio, ResourceLimits


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _rgba_from_channels(channels: np.ndarray) -> np.ndarray:
    rgba = np.empty((channels.shape[0], channels.shape[1], 4), dtype=np.uint8)
    rgba[..., :3] = channels[..., :3]
    return rgba


def _write_png(path: Path, channels: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_rgba_png(_rgba_from_channels(channels)))


class StudioSession:
    def __init__(self, args: argparse.Namespace):
        self.cache_root = Path(args.cache_dir) if args.cache_dir else default_cache_root()
        self.snapshot_path = Path(args.snapshot) if args.snapshot else self.cache_root / "snapshot.json"
        limits = ResourceLimits(
            max_material_pixels=args.max_pixels,
            max_block_resident_bytes=args.max_block_bytes,
            max_parallel_slices=args.max_parallel,
            preview_latency_budget_ms=args.preview_budget,
        )
        self.studio = MaterialStudio(
            cache_root=self.cache_root,
            snapshot_path=self.snapshot_path,
            params=MaterialParams(),
            plan=BlockPlan(),
            limits=limits,
        )


def _short_state(studio: MaterialStudio) -> dict[str, Any]:
    state = studio.state()
    return {
        "stage": state["stage"],
        "revision": state["snapshot"]["params_revision"],
        "params_version": state["snapshot"]["params_version"],
        "plan_version": state["snapshot"]["plan_version"],
        "ready_version": state["snapshot"]["ready_version"],
        "progress": state["progress"],
        "plan": {
            "width": state["plan"]["width"],
            "height": state["plan"]["height"],
            "tile_width": state["plan"]["tile_width"],
            "tile_height": state["plan"]["tile_height"],
            "rows": state["plan"]["rows"],
            "cols": state["plan"]["cols"],
        },
        "last_error": state["last_error"],
        "last_observation": state["last_observation"],
        "snapshot": state["snapshot"],
        "cache": state["cache"],
    }


def cmd_cache_check(args: argparse.Namespace) -> int:
    root = Path(args.path) if args.path else default_cache_root()
    print(json.dumps(probe_cache_writable(root), ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    session = StudioSession(args)
    serve(session.studio, host=args.host, port=args.port)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    session = StudioSession(args)
    studio = session.studio
    params = get_preset(args.preset) if args.preset else studio.params
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    full = generate_full(params, studio.plan)
    _write_png(output / "full-texture.png", full.channels)
    np.save(output / "full-channels.npy", full.channels, allow_pickle=False)
    atomic_write_json(
        output / "full-texture.json",
        {
            "summary": full.summary(),
            "params": params.to_dict(),
            "plan": studio.plan.to_dict(),
        },
    )
    print(json.dumps({"ok": True, "output": str(output), **full.summary()}, ensure_ascii=False, indent=2))
    return 0


def cmd_presets(args: argparse.Namespace) -> int:
    print(json.dumps({"ok": True, "presets": preset_summaries()}, ensure_ascii=False, indent=2))
    return 0


def _snapshot_guard_checks(studio: MaterialStudio, output: Path) -> dict[str, Any]:
    snapshot = studio.snapshot_store.current
    if snapshot is None:
        fail("E_SNAPSHOT_MISSING", "没有可测试的当前快照")
    original = snapshot.to_dict()
    rollback_file = output / "snapshot-rollback-attempt.json"
    corrupt_file = output / "snapshot-corrupt.json"
    rollback_store = SnapshotStore(rollback_file)
    rollback_store.save(snapshot)
    fake = json.loads(json.dumps(original))
    fake["ready"]["version"] = max(1, snapshot.ready["version"] - 1)
    fake["checksum"] = digest_obj(
        {"params": fake["params"], "plan": fake["plan"], "ready": fake["ready"]}
    )
    fake_snapshot = Snapshot.from_dict(fake)
    rollback_result: dict[str, Any]
    try:
        rollback_store.save(fake_snapshot)
        rollback_result = {"ok": False, "reason": "rollback unexpectedly accepted"}
    except MatForgeError as error:
        rollback_result = {"ok": error.code == "E_SNAPSHOT_ROLLBACK", "code": error.code, "details": error.details}
    corrupt_file.write_text("{broken snapshot", encoding="utf-8")
    corrupt_result: dict[str, Any]
    try:
        SnapshotStore(corrupt_file).load()
        corrupt_result = {"ok": False, "reason": "corrupt snapshot unexpectedly accepted"}
    except MatForgeError as error:
        corrupt_result = {"ok": error.code == "E_SNAPSHOT_CORRUPT", "code": error.code}
    return {"rollback": rollback_result, "corruption": corrupt_result}


def cmd_demo(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    if args.fresh and output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    cache_root = Path(args.cache_dir).resolve() if args.cache_dir else output / "cache"
    snapshot_path = Path(args.snapshot).resolve() if args.snapshot else output / "snapshot.json"
    limits = ResourceLimits(
        max_material_pixels=args.max_pixels,
        max_block_resident_bytes=args.max_block_bytes,
        max_parallel_slices=args.max_parallel,
        preview_latency_budget_ms=args.preview_budget,
    )
    report: dict[str, Any] = {
        "schema": "matforge.demo-report.v1",
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "paths": {"output": str(output), "cache": str(cache_root), "snapshot": str(snapshot_path)},
        "steps": [],
    }
    cache_probe = probe_cache_writable(cache_root)
    report["cache_probe"] = cache_probe
    studio = MaterialStudio(cache_root=cache_root, snapshot_path=snapshot_path, limits=limits)
    try:
        first = studio.preview(0, budget_ms=args.preview_budget)
        report["steps"].append(
            {
                "step": "preview_first",
                "ok": True,
                "source": first["source"],
                "observed_ms": first["observed"]["elapsed_ms"],
                "estimate": first["observed"]["estimate"],
                "pixel_range": first["slice"]["pixel_range"],
                "content_hash": first["slice"]["content_hash"],
                "progress": first["progress"],
            }
        )
        second = studio.preview(0, budget_ms=args.preview_budget)
        report["steps"].append(
            {
                "step": "preview_reuse",
                "ok": True,
                "source": second["source"],
                "elapsed_ms": second["observed"]["elapsed_ms"],
            }
        )
        remaining = studio.request_remaining(parallelism=args.parallelism, allow_degrade=True)
        report["steps"].append(
            {
                "step": "generate_remaining",
                "ok": True,
                "reused": remaining["reused"],
                "recomputed": remaining["recomputed"],
                "parallelism": remaining["parallelism"],
                "degraded": remaining["degraded"],
            }
        )
        verify = studio.verify()
        report["steps"].append(
            {
                "step": "byte_adjudication",
                "ok": verify["equal"],
                "mismatch_pixels": verify["mismatch_pixels"],
                "digest": verify["stitched_digest"],
                "full_duration_ms": verify["full"]["duration_ms"],
                "basis": verify["basis"],
            }
        )
        metadata_revision = studio.params.with_revision(studio.params.revision + 1, name="Mineral Slate / named revision")
        metadata_report = studio.commit_plan(metadata_revision, studio.plan, generate_suffix=False)
        report["steps"].append(
            {
                "step": "metadata_revision_reuse",
                "ok": metadata_report["coverage"]["disjoint"] and metadata_report["coverage"]["covers_all"],
                "changed_paths": metadata_report["changed_paths"],
                "affected": metadata_report["affected"],
                "recomputed": metadata_report["recomputed"],
                "reused": metadata_report["reused"],
                "coverage": metadata_report["coverage"],
            }
        )
        parameter_revision = studio.params.with_revision(
            studio.params.revision + 1,
            base_color=(128, 104, 82),
            roughness=0.62,
            normal_strength=2.8,
            noise=replace(studio.params.noise, octaves=6),
        )
        revision_report = studio.commit_plan(
            parameter_revision,
            studio.plan,
            generate_suffix=True,
            parallelism=args.parallelism,
            allow_degrade=True,
        )
        stale_result: dict[str, Any]
        try:
            studio.inspect_stale(0)
            stale_result = {"ok": False, "reason": "stale slice unexpectedly accepted"}
        except MatForgeError as error:
            stale_result = {
                "ok": error.code == "E_STALE_SLICE",
                "code": error.code,
                "details": error.details,
            }
        revision_verify = studio.verify()
        report["steps"].append(
            {
                "step": "parameter_revision_suffix",
                "ok": revision_verify["equal"],
                "changed_paths": revision_report["changed_paths"],
                "affected": revision_report["affected"],
                "affected_suffix": revision_report["affected_suffix"],
                "recomputed": revision_report["recomputed"],
                "reused": revision_report["reused"],
                "coverage": revision_report["coverage"],
                "stale_check": stale_result,
                "verify_equal": revision_verify["equal"],
            }
        )
        plan = BlockPlan(width=320, height=192, tile_width=64, tile_height=48)
        plan_revision = studio.params.with_revision(studio.params.revision + 1)
        plan_report = studio.commit_plan(plan_revision, plan, generate_suffix=False)
        report["steps"].append(
            {
                "step": "plan_whole_replacement",
                "ok": plan_report["coverage"]["disjoint"] and plan_report["coverage"]["covers_all"],
                "affected": plan_report["affected"],
                "recomputed": plan_report["recomputed"],
                "reused": plan_report["reused"],
                "versions": plan_report["versions"],
            }
        )
        studio.request_remaining(parallelism=args.parallelism)
        plan_verify = studio.verify()
        report["steps"].append(
            {
                "step": "plan_verify",
                "ok": plan_verify["equal"],
                "mismatch_pixels": plan_verify["mismatch_pixels"],
                "digest": plan_verify["stitched_digest"],
            }
        )
        selfcheck = studio.selfcheck()
        report["steps"].append({"step": "selfcheck", "ok": selfcheck["ok"], "checks": selfcheck["checks"]})
        report["snapshot_guards"] = _snapshot_guard_checks(studio, output)
        report["final_state"] = _short_state(studio)
        full = generate_full(studio.params, studio.plan)
        _write_png(output / "full-texture.png", full.channels)
        np.save(output / "full-channels.npy", full.channels, allow_pickle=False)
        rgba, _ = studio.capture_rgba()
        (output / "studio-preview.png").write_bytes(encode_rgba_png(rgba))
        report["artifacts"] = {
            "full_texture_png": str(output / "full-texture.png"),
            "full_channels_npy": str(output / "full-channels.npy"),
            "studio_preview_png": str(output / "studio-preview.png"),
            "report_json": str(output / "demo-report.json"),
            "snapshot_json": str(snapshot_path),
        }
        report["ok"] = all(
            step.get("ok", False)
            for step in report["steps"]
        ) and all(value.get("ok", False) for value in report["snapshot_guards"].values())
        atomic_write_json(output / "demo-report.json", report)
    except MatForgeError as error:
        report["ok"] = False
        report["error"] = error.to_dict()
        atomic_write_json(output / "demo-report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "report": str(output / "demo-report.json"),
                "first_slice_observed_ms": next(
                    step["observed_ms"] for step in report["steps"] if step["step"] == "preview_first"
                ),
                "first_slice_estimated_ms": next(
                    step["estimate"]["estimated_ms"] for step in report["steps"] if step["step"] == "preview_first"
                ),
                "steps": [
                    {
                        "step": step["step"],
                        "ok": step.get("ok"),
                        **(
                            {
                                "recomputed": step.get("recomputed"),
                                "reused": step.get("reused"),
                            }
                            if "recomputed" in step
                            else {}
                        ),
                    }
                    for step in report["steps"]
                ],
                "snapshot_guards": report["snapshot_guards"],
                "final_versions": report["final_state"]["snapshot"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


def cmd_interactive(args: argparse.Namespace) -> int:
    session = StudioSession(args)
    studio = session.studio
    print("MatForge interactive studio. Type 'help' for commands, 'quit' to exit.")
    _print(_short_state(studio))
    while True:
        try:
            raw = input("matforge> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        parts = raw.split()
        command = parts[0].lower()
        try:
            if command in ("quit", "exit"):
                break
            if command == "help":
                print(
                    "state | preview [block] [budget_ms] | rest [parallel] | verify | selfcheck\n"
                    "revise <revision> <r> <g> <b> | plan <w> <h> <tw> <th> [revision]\n"
                    "replay <block> | stale <block> | probe | quit"
                )
            elif command == "state":
                _print(studio.state())
            elif command == "preview":
                block = int(parts[1]) if len(parts) > 1 else 0
                budget = float(parts[2]) if len(parts) > 2 else studio.limits.preview_latency_budget_ms
                _print(studio.preview(block, budget_ms=budget))
            elif command == "rest":
                parallel = int(parts[1]) if len(parts) > 1 else studio.limits.max_parallel_slices
                _print(studio.request_remaining(parallelism=parallel, allow_degrade=True))
            elif command == "verify":
                _print(studio.verify())
            elif command == "selfcheck":
                _print(studio.selfcheck())
            elif command == "revise":
                revision = int(parts[1])
                color = tuple(int(value) for value in parts[2:5])
                _print(studio.apply_revision(studio.params.with_revision(revision, base_color=color)))
            elif command == "plan":
                plan = BlockPlan(int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))
                params = studio.params
                if len(parts) > 5:
                    params = params.with_revision(int(parts[5]))
                _print(studio.commit_plan(params, plan, generate_suffix=False))
            elif command == "replay":
                _print(studio.replay_slice(int(parts[1])))
            elif command == "stale":
                _print(studio.inspect_stale(int(parts[1])))
            elif command == "probe":
                _print(probe_cache_writable(studio.cache.root))
            else:
                print("unknown command; type 'help'")
        except MatForgeError as error:
            print(json.dumps(error.to_dict(), ensure_ascii=False, indent=2))
        except (ValueError, IndexError) as error:
            print(f"input error: {error}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="matforge", description="Local deterministic texture and material studio")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cache-dir", help="cache directory (default: platform cache root)")
        p.add_argument("--snapshot", help="snapshot JSON file (default: <cache>/snapshot.json)")
        p.add_argument("--max-pixels", type=int, default=8_388_608)
        p.add_argument("--max-block-bytes", type=int, default=32_000_000)
        p.add_argument("--max-parallel", type=int, default=4)
        p.add_argument("--preview-budget", type=float, default=350.0)

    serve_parser = sub.add_parser("serve", help="start the local browser application")
    common(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.set_defaults(func=cmd_serve)

    demo_parser = sub.add_parser("demo", help="run the reproducible acceptance demonstration")
    common(demo_parser)
    demo_parser.add_argument("--output", default="artifacts/demo")
    demo_parser.add_argument("--parallelism", type=int, default=3)
    demo_parser.add_argument("--fresh", action="store_true", default=True)
    demo_parser.add_argument("--keep-cache", dest="fresh", action="store_false")
    demo_parser.set_defaults(func=cmd_demo)

    interactive_parser = sub.add_parser("interactive", help="line-oriented local interactive studio")
    common(interactive_parser)
    interactive_parser.set_defaults(func=cmd_interactive)

    render_parser = sub.add_parser("render", help="render the full deterministic texture to PNG/NPY")
    common(render_parser)
    render_parser.add_argument("--output", default="artifacts/render")
    render_parser.add_argument("--preset", choices=preset_names(), help="fixed-seed material preset")
    render_parser.set_defaults(func=cmd_render)

    presets_parser = sub.add_parser("presets", help="list fixed-seed material presets")
    presets_parser.set_defaults(func=cmd_presets)

    cache_parser = sub.add_parser("cache-check", help="probe cache directory writability")
    cache_parser.add_argument("--path", help="cache path to probe")
    cache_parser.set_defaults(func=cmd_cache_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MatForgeError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

