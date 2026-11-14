"""命令行入口。

用法：
  subaudit audit   audio.wav manifest.json -o report.json [--tolerance-ms 200]
  subaudit samples --scenario all --out-dir ./samples
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import soundfile as sf

from .audit import DEFAULT_TOLERANCE_MS, audit_manifest
from .samples import SCENARIOS, make_all_samples, make_sample


def _load_manifest(path: Path) -> list:
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, list):
        raise ValueError("字幕清单必须是 JSON 数组（每条一个对象）")
    return data


def _cmd_audit(args: argparse.Namespace) -> int:
    audio_path = Path(args.audio)
    manifest_path = Path(args.manifest)

    samples, sample_rate = sf.read(str(audio_path), always_2d=False)
    manifest = _load_manifest(manifest_path)

    report = audit_manifest(
        samples,
        sample_rate,
        manifest,
        tolerance_ms=args.tolerance_ms,
    )
    report["audio_file"] = str(audio_path)
    report["manifest_file"] = str(manifest_path)

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"报告已写入: {out_path}（状态: {report['status']}）")
        print(
            f"共 {len(report['results'])} 句，"
            f"返工 {len(report['flagged'])} 句，"
            f"无效条目 {len(report['invalid_entries'])} 条，"
            f"失败 {len(report['failures'])} 条"
        )
    else:
        print(text)
    return 0 if report["status"] == "complete" else 2


def _cmd_samples(args: argparse.Namespace) -> int:
    if args.scenario == "all":
        created = make_all_samples(args.out_dir)
        for name, (wav_path, json_path) in created.items():
            print(f"[{name}] {wav_path} + {json_path}")
    else:
        wav_path, json_path = make_sample(args.scenario, args.out_dir)
        print(f"{wav_path} + {json_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subaudit",
        description="离线检测每句人声发声区间并核查字幕覆盖与偏差（确定性输出）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="执行逐句核查")
    p_audit.add_argument("audio", help="对白 WAV 文件路径")
    p_audit.add_argument("manifest", help="字幕清单 JSON 路径（数组）")
    p_audit.add_argument("-o", "--output", help="输出报告 JSON 路径；省略则打印到标准输出")
    p_audit.add_argument(
        "--tolerance-ms",
        type=float,
        default=DEFAULT_TOLERANCE_MS,
        help=f"偏差容忍毫秒数（默认 {DEFAULT_TOLERANCE_MS}）",
    )
    p_audit.set_defaults(func=_cmd_audit)

    p_samples = sub.add_parser("samples", help="生成确定性样本 WAV 与字幕清单")
    p_samples.add_argument("--scenario", choices=SCENARIOS, default="all")
    p_samples.add_argument("--out-dir", default="./samples")
    p_samples.set_defaults(func=_cmd_samples)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"错误: 文件不存在: {exc.filename}", file=sys.stderr)
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"错误: 输入无效: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
