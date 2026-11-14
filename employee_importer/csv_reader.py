# -*- coding: utf-8 -*-
"""CSV 读取：保留源文件物理行号，缺值不中断整批处理。"""

from __future__ import annotations

import csv
from dataclasses import dataclass

REQUIRED_COLUMNS = ("姓名", "邮箱", "部门")


@dataclass(frozen=True)
class EmployeeRow:
    """一条员工输入记录。

    line_no: 该数据行在源 CSV 中的物理行号（csv 模块的 line_num，从 1 起，含表头）
    name/email/department: None 表示该字段缺失或为空，由表单层决定留空
    """

    line_no: int
    name: str | None
    email: str | None
    department: str | None

    def non_empty_fields(self) -> dict[str, str]:
        """返回非空字段（键为表单输入框 name），供表单层只填写非空字段。"""
        result = {}
        if self.name is not None:
            result["name"] = self.name
        if self.email is not None:
            result["email"] = self.email
        if self.department is not None:
            result["department"] = self.department
        return result


def _normalize(value: object) -> str | None:
    """空白字符串视为缺值；非字符串值转字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def read_employee_csv(path: str) -> list[EmployeeRow]:
    """读取员工 CSV。

    - 使用 utf-8-sig 兼容带 BOM 的文件
    - 三列中缺少的列按空列处理（不中断），并在返回值外通过 warnings 体现
    - 完全空白的物理行跳过（不计入结果，也不产生报告条目）
    - 任何字段缺值都保留为 None，不影响其他行
    """
    # newline="" 是 csv 模块官方要求；utf-8-sig 自动剥离 BOM
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows: list[EmployeeRow] = []
        for raw in reader:
            # DictReader 在列数不齐时可能把多余内容放到 None 键，忽略之
            line_no = reader.line_num
            values = {
                "name": _normalize(raw.get("姓名")),
                "email": _normalize(raw.get("邮箱")),
                "department": _normalize(raw.get("部门")),
            }
            # 三字段全空的行视为空白数据行，跳过
            if all(v is None for v in values.values()):
                continue
            rows.append(EmployeeRow(line_no=line_no, **values))
    return rows
