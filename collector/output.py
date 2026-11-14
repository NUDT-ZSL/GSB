"""产物写出：UTF-8 CSV 清单与 JSON 来源可用性报告。

清单固定表头与字段顺序，按发布日期降序、同日期按标题升序排列。
写出一律使用 ``\\n`` 换行与 UTF-8 无 BOM 编码，保证同样输入逐字节一致。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .crawl import REASON_LABELS, SOURCE_REASON_LABELS, SourceResult

CSV_COLUMNS = ("标题", "发布日期", "详情链接")


def _date_key(value: str) -> int:
    return int(value.replace("-", ""))


def sort_records(results: list[SourceResult]):
    records = [record for result in results for record in result.records]
    return sorted(
        records,
        key=lambda r: (-_date_key(r.publish_date), r.title, r.source_id, r.url),
    )


def render_csv(results: list[SourceResult]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for record in sort_records(results):
        writer.writerow((record.title, record.publish_date, record.url))
    return buffer.getvalue()


def report_entry(result: SourceResult) -> dict:
    return {
        "id": result.source.id,
        "name": result.source.name,
        "url": result.source.url,
        "pages_read": result.pages_read,
        "records": len(result.records),
        "excluded": [
            {
                "url": item.url,
                "entry_title": item.entry_title,
                "reason": item.reason,
                "reason_label": item.reason_label,
                "detail": item.detail,
            }
            for item in result.excluded
        ],
    }


def build_report(results: list[SourceResult]) -> dict:
    complete: list[dict] = []
    incomplete: list[dict] = []
    for result in results:
        entry = report_entry(result)
        entry["status"] = result.status
        if result.is_complete:
            entry["reason"] = None
            entry["reason_label"] = None
            entry["reason_detail"] = None
            complete.append(entry)
        else:
            entry["reason"] = result.effective_reason
            entry["reason_label"] = result.reason_label
            entry["reason_detail"] = result.reason_text
            incomplete.append(entry)

    status_counts: dict[str, int] = {}
    for result in results:
        status_counts[result.status] = status_counts.get(result.status, 0) + 1

    return {
        "complete_sources": complete,
        "incomplete_sources": incomplete,
        "reason_labels": {
            "sources": SOURCE_REASON_LABELS,
            "entries": REASON_LABELS,
        },
        "totals": {
            "sources": len(results),
            "complete": len(complete),
            "incomplete": len(incomplete),
            "partial": status_counts.get("partial", 0),
            "records": sum(len(r.records) for r in results),
        },
        "notes": [
            "complete_sources 仅包含目录页全部读取、且所有条目均成功采集的来源。",
            "incomplete_sources 包含 status=partial（目录读全但有条目被排除）"
            "与 status=incomplete（目录页未能读完或来源超时）两类；"
            "其 records 字段只是已成功采集的条数，不代表完整数据。",
            "excluded 列出未进入清单的条目的链接与原因类别。",
        ],
    }


def render_report(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def write_outputs(out_dir: Path, results: list[SourceResult]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "announcements.csv"
    report_path = out_dir / "source-report.json"
    csv_path.write_bytes(render_csv(results).encode("utf-8"))
    report_path.write_bytes(render_report(build_report(results)).encode("utf-8"))
    return csv_path, report_path
