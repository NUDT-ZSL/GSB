"""CSV 标记文件读取与严格校验。

格式约定：
- UTF-8 编码、逗号分隔、无表头、恰好三列：片段编号,起始秒,结束秒
- 兼容带 UTF-8 BOM 的文件
- 空行被跳过；任何数据行缺列或时间非数字均为致命错误（抛
  :class:`SplitterError`），由 CLI 转换为非 0 退出码
- 仅当第一行恰好 3 列且时间列无法解析为数字时，才将其识别为可选表头
  （防止把表头误判为“时间非数字”致命错误；除此之外不接受任何非数据行）
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from .errors import SplitterError
from .models import Marker


def _parse_time(token: str, row_number: int, column_name: str) -> Decimal:
    """把单个时间 token 解析为有限非负的 Decimal，否则报致命错误。"""
    text = token.strip()
    if text == "":
        raise SplitterError(
            f"CSV 格式错误：第 {row_number} 行{column_name}为空，无法解析为时间"
        )
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise SplitterError(
            f"CSV 格式错误：第 {row_number} 行{column_name}“{text}”不是合法数字"
        ) from None
    if not value.is_finite():
        raise SplitterError(
            f"CSV 格式错误：第 {row_number} 行{column_name}“{text}”不是有限数值"
        )
    return value


def load_markers(csv_path: str) -> list[Marker]:
    """读取并校验 CSV，返回按文件顺序排列的标记列表。

    缺失文件、编码错误、列数不足、时间非数字等均抛 :class:`SplitterError`。
    """
    try:
        # newline="" 让 csv 模块自行控制换行；utf-8-sig 兼容 BOM
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise SplitterError(f"CSV 文件不存在：{csv_path}") from None
    except OSError as exc:
        raise SplitterError(f"CSV 文件无法读取：{csv_path}（{exc}）") from None

    reader = csv.reader(io.StringIO(text))
    markers: list[Marker] = []
    header_skipped = False

    for row_number, row in enumerate(reader, start=1):
        # csv 模块对纯空行返回 []；空白行同理，跳过
        if not row or all(cell.strip() == "" for cell in row):
            continue

        if len(row) != 3:
            raise SplitterError(
                f"CSV 格式错误：第 {row_number} 行列数为 {len(row)}，"
                f"期望恰好 3 列（片段编号,起始秒,结束秒）"
            )

        segment_id = row[0].strip()
        if segment_id == "":
            raise SplitterError(f"CSV 格式错误：第 {row_number} 行片段编号为空")

        # 仅第一行允许是表头：三列齐全但两个时间列都不是数字
        if not header_skipped and not markers:
            try:
                start = _parse_time(row[1], row_number, "起始时间")
                end = _parse_time(row[2], row_number, "结束时间")
            except SplitterError:
                header_skipped = True
                continue
        else:
            start = _parse_time(row[1], row_number, "起始时间")
            end = _parse_time(row[2], row_number, "结束时间")

        markers.append(
            Marker(
                segment_id=segment_id,
                start=start,
                end=end,
                raw_start=row[1].strip(),
                raw_end=row[2].strip(),
                row_number=row_number,
            )
        )

    if not markers:
        raise SplitterError(f"CSV 格式错误：{csv_path} 中没有任何有效标记行")
    return markers
