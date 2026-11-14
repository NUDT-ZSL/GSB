"""命令行入口。

用法：
    python -m subtitle_audit <清单.json> <输出目录>

退出码：
    0 成功；2 清单文件/JSON 错误（不生成任何输出）；3 输出目录不可写
"""
from __future__ import annotations

import json
import os
import sys
import time

from .checker import audit, build_report, write_report


def _load_manifest(manifest_path: str) -> dict:
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"清单文件不存在：{manifest_path}")
    try:
        # utf-8-sig 可同时兼容带/不带 BOM 的 UTF-8 清单
        with open(manifest_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"清单文件不是合法 JSON：{manifest_path}（{exc.msg}，第 {exc.lineno} 行）") from exc
    if not isinstance(data, dict):
        raise ValueError("清单文件顶层必须为 JSON 对象")
    return data


def _ensure_output_dir(output_dir: str) -> None:
    if os.path.exists(output_dir):
        if not os.path.isdir(output_dir):
            raise NotADirectoryError(f"输出路径不是目录：{output_dir}")
    else:
        # 需求 13：目录不存在时创建
        os.makedirs(output_dir, exist_ok=True)
    # 可写性探测：直接以独占创建方式打开一个探针文件。
    # 不使用 tempfile.mkstemp —— CPython 在 Windows 上会把 PermissionError
    # 当作文件名冲突重试 TMP_MAX 次，只读目录下会长时间挂起。
    probe = os.path.join(output_dir, f".write_probe_{os.getpid()}_{time.perf_counter_ns()}")
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except OSError as exc:
        raise PermissionError(f"输出目录不可写：{output_dir}（{exc}）") from exc
    else:
        os.close(fd)
        os.remove(probe)


def main(argv=None) -> int:
    # Windows 控制台默认 GBK 代码页时，强制 UTF-8 避免中文报错乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("用法：python -m subtitle_audit <清单路径> <输出目录>", file=sys.stderr)
        return 2
    manifest_path, output_dir = argv

    # 需求 11：清单问题先报错退出，不生成预览图和报告
    try:
        manifest = _load_manifest(manifest_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        _ensure_output_dir(output_dir)
    except (NotADirectoryError, PermissionError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 3

    results = audit(manifest, output_dir)
    report = build_report(results, manifest_path)
    report_path = os.path.join(output_dir, "audit_report.json")
    write_report(report, report_path)

    total = sum(len(r.texts) for r in results)
    skipped = sum(1 for r in results for t in r.texts if t.skipped) + sum(1 for r in results if r.skipped)
    print(f"核对完成：画布 {len(results)} 张，文字项 {total} 条，跳过 {skipped} 条")
    print(f"报告：{report_path}")
    for r in results:
        if r.skipped:
            print(f"  [画布#{r.index} {r.sheet_id}] 跳过：{r.error}")
        else:
            print(f"  [画布#{r.index} {r.sheet_id}] 预览：{r.preview_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
