"""切点清单 CSV 的读取与校验。

清单格式：首行表头 frame,ms；后续每行一个切点（帧号、毫秒）。
校验规则：
  - 帧号必须是非负整数，毫秒必须是非负整数；
  - 帧号全局唯一，重复帧号逐条记录异常；
  - 毫秒必须严格单调递增（相等也算逆序），逐条记录异常；
  - 任何坏行都不中断整体读取，全部异常在返回值 anomalies 中逐条说明。
"""

from __future__ import annotations

import csv

from .errors import ContactSheetError

REQUIRED_HEADER = ["frame", "ms"]


def _parse_nonneg_int(text: str, field: str, row_number: int) -> int:
    text = (text or "").strip()
    if text == "":
        raise ValueError(f"第 {row_number} 行 {field} 为空")
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(f"第 {row_number} 行 {field}={text!r} 不是整数") from exc
    if value < 0:
        raise ValueError(f"第 {row_number} 行 {field}={value} 不能为负数")
    return value


def load_timestamps(csv_path: str) -> tuple[list[dict], list[dict]]:
    """返回 (entries, anomalies)，保持清单原始行顺序。

    entry: {"row": 行号(含表头从1起), "frame": 帧号, "ms": 毫秒,
            "anomaly_reasons": [str, ...]}
    anomaly: {"row": 行号, "frame": 帧号|None, "ms": 毫秒|None,
              "type": "duplicate_frame"|"non_monotonic_ms"|"malformed_row",
              "message": 中文说明}
    """
    try:
        fh = open(csv_path, "r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise ContactSheetError(f"时间戳 CSV 不存在：{csv_path}") from exc
    except OSError as exc:
        raise ContactSheetError(f"时间戳 CSV 无法读取：{csv_path}（{exc}）") from exc

    entries: list[dict] = []
    anomalies: list[dict] = []
    seen_frames: set[int] = set()
    previous_ms: int | None = None

    with fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            # 完全空文件：按空清单处理（核对点 8）
            return entries, anomalies
        if [h.strip().lower() for h in header[:2]] != REQUIRED_HEADER:
            raise ContactSheetError(
                f"CSV 表头必须是 {','.join(REQUIRED_HEADER)}，实际为 {','.join(header)!r}")

        for row in reader:
            row_number = reader.line_num
            if not row or all((cell or "").strip() == "" for cell in row):
                continue  # 跳过纯空行，不计异常
            if len(row) < 2:
                anomalies.append({
                    "row": row_number, "frame": None, "ms": None,
                    "type": "malformed_row",
                    "message": f"第 {row_number} 行列数不足，应为 frame,ms 两列",
                })
                continue

            try:
                frame = _parse_nonneg_int(row[0], "frame", row_number)
                ms = _parse_nonneg_int(row[1], "ms", row_number)
            except ValueError as exc:
                anomalies.append({
                    "row": row_number,
                    "frame": row[0].strip() or None,
                    "ms": (row[1].strip() if len(row) > 1 else None) or None,
                    "type": "malformed_row",
                    "message": str(exc),
                })
                continue

            reasons: list[str] = []
            if frame in seen_frames:
                msg = f"第 {row_number} 行帧号 {frame} 重复（首次出现于第 {_first_row_of(entries, frame)} 行）"
                reasons.append(msg)
                anomalies.append({
                    "row": row_number, "frame": frame, "ms": ms,
                    "type": "duplicate_frame", "message": msg,
                })
            else:
                seen_frames.add(frame)

            if previous_ms is not None and ms <= previous_ms:
                msg = (f"第 {row_number} 行毫秒 {ms} 未严格递增"
                       f"（上一条为 {previous_ms}）")
                reasons.append(msg)
                anomalies.append({
                    "row": row_number, "frame": frame, "ms": ms,
                    "type": "non_monotonic_ms", "message": msg,
                })
            previous_ms = ms

            entries.append({
                "row": row_number, "frame": frame, "ms": ms,
                "anomaly_reasons": reasons,
            })

    return entries, anomalies


def _first_row_of(entries: list[dict], frame: int) -> int:
    for entry in entries:
        if entry["frame"] == frame:
            return entry["row"]
    return -1
