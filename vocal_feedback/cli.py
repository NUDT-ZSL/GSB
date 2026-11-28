from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

from .config import Config
from .dataset import SampleFiles, discover_samples
from .evaluation import evaluate_sample
from .pipeline import analyze_sample
from .report import write_reports


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "sample"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vocal-feedback",
        description="本地单声道清唱音高、音符、歌词对齐与演唱评分工具",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="单个 WAV、单个样本目录，或包含多个样本子目录的批量目录",
    )
    parser.add_argument("--output", required=True, help="报告输出目录")
    parser.add_argument("--config", help="JSON 配置路径")
    parser.add_argument("--frame-length", type=float, help="覆盖帧长（秒）")
    parser.add_argument("--hop-length", type=float, help="覆盖帧移（秒）")
    parser.add_argument("--wav", help="单样本模式下覆盖 WAV 路径")
    parser.add_argument("--lyrics", help="单样本模式下覆盖歌词路径")
    parser.add_argument("--reference", help="单样本模式下覆盖参考音高路径")
    parser.add_argument("--annotations", help="单样本模式下覆盖手工标注路径")
    parser.add_argument(
        "--no-evaluation",
        action="store_true",
        help="即使存在手工标注也不生成验收指标",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出最终摘要")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    return parser


def _apply_path_overrides(
    samples: list[SampleFiles], args: argparse.Namespace
) -> list[SampleFiles]:
    overrides = {
        "wav": args.wav,
        "lyrics": args.lyrics,
        "reference": args.reference,
        "annotations": args.annotations,
    }
    if not any(overrides.values()):
        return samples
    if len(samples) != 1:
        raise ValueError("path overrides are only valid when exactly one sample is discovered")
    base = samples[0]
    sample = SampleFiles(
        sample_id=base.sample_id,
        directory=base.directory,
        wav=Path(args.wav) if args.wav else base.wav,
        lyrics=Path(args.lyrics) if args.lyrics else base.lyrics,
        reference=Path(args.reference) if args.reference else base.reference,
        annotations=Path(args.annotations) if args.annotations else base.annotations,
        manifest=base.manifest,
    )
    for label, path in [
        ("WAV", sample.wav),
        ("lyrics", sample.lyrics),
        ("reference", sample.reference),
    ]:
        if not path.is_file():
            raise FileNotFoundError(f"{label} file not found: {path}")
    if sample.annotations is not None and not sample.annotations.is_file():
        raise FileNotFoundError(f"annotation file not found: {sample.annotations}")
    return [sample]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = Config.from_json(args.config)
        if args.frame_length is not None:
            config.frame_length_s = args.frame_length
        if args.hop_length is not None:
            config.hop_length_s = args.hop_length
        config.validate()
        samples = discover_samples(args.input)
        samples = _apply_path_overrides(samples, args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    multiple = len(samples) > 1
    batch_results: list[dict[str, object]] = []
    for sample in samples:
        sample_output = output_root / _safe_name(sample.sample_id) if multiple else output_root
        result: dict[str, object] = {
            "sample_id": sample.sample_id,
            "input_audio": str(sample.wav),
            "output_dir": str(sample_output),
        }
        try:
            report = analyze_sample(sample, config)
            evaluation = None
            if sample.annotations is not None and not args.no_evaluation:
                evaluation = evaluate_sample(report, sample.annotations, config)
                if evaluation is not None:
                    report["evaluation"] = evaluation
            paths = write_reports(report, sample_output, evaluation)
            result.update(
                {
                    "ok": True,
                    "notes": report["summary"]["note_count"],
                    "syllables": report["summary"]["syllable_count"],
                    "pitch_hit_rate": report["summary"]["pitch_hit_rate"],
                    "warnings": report["warnings"],
                    "paths": paths,
                }
            )
            if evaluation is not None:
                result["evaluation_passed"] = evaluation["passed"]
            if not args.quiet:
                status = "OK"
                if evaluation is not None:
                    status += " / PASS" if evaluation["passed"] else " / FAIL"
                print(
                    f"[{status}] {sample.sample_id}: "
                    f"{report['summary']['note_count']} notes, "
                    f"{report['summary']['syllable_count']} syllables, "
                    f"pitch-hit {report['summary']['pitch_hit_rate'] * 100:.1f}% -> "
                    f"{sample_output}"
                )
        except Exception as exc:
            result.update({"ok": False, "error": str(exc)})
            if not args.quiet:
                print(f"[ERROR] {sample.sample_id}: {exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
        batch_results.append(result)

    if multiple:
        batch_payload = {
            "schema_version": "1.0",
            "sample_count": len(samples),
            "success_count": sum(bool(item.get("ok")) for item in batch_results),
            "failure_count": sum(not bool(item.get("ok")) for item in batch_results),
            "evaluated_count": sum("evaluation_passed" in item for item in batch_results),
            "passed_count": sum(bool(item.get("evaluation_passed")) for item in batch_results),
            "samples": batch_results,
        }
        with (output_root / "batch_summary.json").open("w", encoding="utf-8") as handle:
            json.dump(batch_payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if not args.quiet:
            print(
                f"batch: {batch_payload['success_count']}/{batch_payload['sample_count']} "
                f"succeeded, {batch_payload['passed_count']}/"
                f"{batch_payload['evaluated_count']} acceptance checks passed"
            )
    failures = sum(not bool(item.get("ok")) for item in batch_results)
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

