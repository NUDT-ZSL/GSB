"""命令行入口：读取配置 -> 采集本地授权站点 -> 写出清单与报告。

用法::

    python -m collector --config config/sample.json --out output

退出码：
  0  采集结束且所有来源完整
  2  配置无效（不产出清单与报告）
  3  输出目录不可写（不产出清单与报告）
  4  采集结束但存在未完成来源（产物照常写出）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, Settings, load_settings
from .crawl import SourceResult, collect_source
from .fetch import SiteFetcher
from .output import build_report, write_outputs


def _check_writable(out_dir: Path) -> None:
    """在采集前确认输出目录可写，避免跑完后才失败。"""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".write-probe"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise OSError(f"输出目录不可写：{out_dir}（{exc.strerror or exc}）") from None


def run(settings: Settings, out_dir: Path) -> tuple[list[SourceResult], dict, tuple[Path, Path]]:
    if not settings.site_root.is_dir():
        raise ConfigError([f"站点根路径不存在或不是目录：{settings.site_root}"])

    with SiteFetcher(
        settings.site_root,
        timeout_seconds=settings.timeout_seconds,
        deadline_seconds=settings.deadline_seconds,
    ) as fetcher:
        results: list[SourceResult] = []
        for source in settings.sources:
            if fetcher.remaining() <= 0:
                # 总时限耗尽：其余来源明确记为未完成，而不是伪装成空结果
                results.append(
                    SourceResult(
                        source=source,
                        reason="deadline_exceeded",
                        reason_detail="总采集时限已耗尽，该来源未开始采集",
                    )
                )
                continue
            results.append(collect_source(source, settings, fetcher))

    paths = write_outputs(out_dir, results)
    report = build_report(results)
    return results, report, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="collector",
        description="采集本地授权镜像站点的公告清单（不访问公网）",
    )
    parser.add_argument("--config", required=True, help="JSON 配置文件路径")
    parser.add_argument("--out", default="output", help="输出目录（默认 ./output）")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(Path(args.config))
    except ConfigError as exc:
        print("配置无效，未生成清单与报告：", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    try:
        _check_writable(out_dir)
    except OSError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("未生成清单与报告。", file=sys.stderr)
        return 3

    try:
        _results, report, (csv_path, report_path) = run(settings, out_dir)
    except ConfigError as exc:
        print("配置无效，未生成清单与报告：", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    record_count = report["totals"]["records"]
    incomplete_count = report["totals"]["incomplete"]

    print(f"清单：{csv_path}")
    print(f"报告：{report_path}")
    for entry in report["incomplete_sources"]:
        status = entry["status"]
        print(f"  不完整来源：{entry['name']}（{status}，{entry['reason_label']}）"
              f" — {entry['reason_detail']}")
    print(f"清单条数：{record_count}")
    print(f"未完成来源数：{incomplete_count}")

    return 0 if incomplete_count == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
