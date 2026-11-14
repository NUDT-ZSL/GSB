"""端到端编排：所有计算与校验完成后才落盘，失败时不产生半成品文件。"""

from __future__ import annotations

import os

from .config import SheetConfig, load_config
from .csv_loader import load_timestamps
from .report import build_report, save_report
from .selector import DEFAULT_MERGE_GAP_MS, DEFAULT_SIMILARITY_MAE, locate_frames, merge_similar
from .sheet import build_sheet, save_png
from .video import decode_all_frames, probe_video


def run(video_path: str, csv_path: str, config_path: str | None,
        out_dir: str, sheet_name: str = "contact_sheet.png",
        report_name: str = "location_report.json",
        merge_gap_ms: int = DEFAULT_MERGE_GAP_MS,
        similarity_mae: float = DEFAULT_SIMILARITY_MAE) -> dict:
    # 1. 配置与清单（失败直接抛异常，此时尚未写任何输出）
    config: SheetConfig = load_config(config_path)
    entries, anomalies = load_timestamps(csv_path)

    # 2. 视频探测（非视频/损坏文件在此给出可读原因）
    info = probe_video(video_path)

    # 3. 按清单定位帧——只定位清单内的帧，不做时间点盲抽
    records: list[dict] = []
    if entries:
        frames = decode_all_frames(video_path, info)
        located = locate_frames(
            entries, frames, info, (config.thumb_width, config.thumb_height))
        records = merge_similar(located, merge_gap_ms, similarity_mae)

    # 4. 组装报告与联系表（纯内存计算）
    report = build_report(records, anomalies, config, video_path, csv_path,
                          config_path, info)
    note = report["summary"]["note"]
    if note is None and records and report["summary"]["located"] == 0:
        note = f"无可定位帧：{report['summary']['unlocatable']} 条切点全部越界"
    sheet_image = build_sheet(records, config, video_path, note)

    # 5. 全部成功后才创建输出目录并落盘
    os.makedirs(out_dir, exist_ok=True)
    sheet_path = os.path.join(out_dir, sheet_name)
    report_path = os.path.join(out_dir, report_name)
    save_report(report, report_path)
    save_png(sheet_image, sheet_path)

    return {
        "sheet_path": sheet_path,
        "report_path": report_path,
        "summary": report["summary"],
    }
