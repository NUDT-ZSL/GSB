"""命令行入口：生成样本、批量运行流水线与核对、确定性比对。"""

import argparse
import json
import sys

from .pipeline import PipelineError, UnparsedAction, run_pipeline
from .sample_data import generate_sample_workspace


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)


def generate(args):
    paths = generate_sample_workspace(args.output)
    print("已生成样本文件：")
    for name, path in paths.items():
        print(f"  {name:7s} {path}")


def run(args):
    with open(args.request, "r", encoding="utf-8") as handle:
        request = json.load(handle)
    try:
        result = run_pipeline(request)
    except UnparsedAction as exc:
        _write_json(args.output, {
            "status": "ERROR", "code": exc.code,
            "entry_index": exc.index, "entry_text": exc.text, "reason": exc.reason,
        })
        print(f"E_UNPARSED 条目 {exc.index}: {exc.text} ({exc.reason})")
        return 2
    except PipelineError as exc:
        _write_json(args.output, {"status": "ERROR", "code": "E_INPUT", "reason": str(exc)})
        print(f"E_INPUT: {exc}")
        return 2
    _write_json(args.output, result)
    report = result["verification_report"]
    print(f"完成 {result['summary_report']['action_count']} 条动作，"
          f"核对全部通过: {report['all_passed']}")
    print(f"输出: {args.output}")
    return 0


def check_determinism(args):
    with open(args.request, "r", encoding="utf-8") as handle:
        request = json.load(handle)
    first = json.dumps(run_pipeline(request), sort_keys=True)
    second = json.dumps(run_pipeline(request), sort_keys=True)
    if first == second:
        print("确定性检查通过：两次生成逐帧数值一致")
        return 0
    print("确定性检查失败：两次生成结果不一致")
    return 1


def acceptance(args):
    from .acceptance import run_acceptance
    report = run_acceptance()
    _write_json(args.output, report)
    for item in report["label_checks"]:
        mark = "PASS" if item["label_passed"] else f"FAIL {item['mismatches']}"
        print(f"[{mark}] {item['text']}")
    print(f"标签全部通过: {report['all_labels_passed']}")
    print(f"轨迹核对全部通过: {report['all_trajectories_passed']}")
    print(f"相机全部不穿模: {report['all_cameras_clear']}")
    print(f"报告输出: {args.output}")
    return 0 if (report["all_labels_passed"] and report["all_trajectories_passed"]
                 and report["all_cameras_clear"]) else 1


def build_parser():
    parser = argparse.ArgumentParser(description="博物馆数字展陈动画工作台 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="生成样本数据")
    gen.add_argument("-o", "--output", default="sample_workspace")
    gen.set_defaults(func=generate)

    run_cmd = sub.add_parser("run", help="运行流水线并输出动画 JSON")
    run_cmd.add_argument("request", help="请求 JSON 文件路径")
    run_cmd.add_argument("-o", "--output", default="animation_output.json")
    run_cmd.set_defaults(func=run)

    det = sub.add_parser("check-determinism", help="重复生成一致性检查")
    det.add_argument("request")
    det.set_defaults(func=check_determinism)

    acc = sub.add_parser("acceptance", help="在人工标注脚本集上运行验收核对")
    acc.add_argument("-o", "--output", default="acceptance_report.json")
    acc.set_defaults(func=acceptance)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
