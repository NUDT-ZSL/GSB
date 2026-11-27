"""Candidate index definitions and their deterministic range-access plans."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .models import PortRecord, QuerySpec


HIGH_STRING = "\U0010ffff"
INF_SEQUENCE = math.inf


@dataclass(frozen=True, slots=True)
class ScanBounds:
    lower_key: tuple[Any, ...] | None
    upper_key: tuple[Any, ...] | None
    access_description: str
    full_scan_required: bool = False


@dataclass(frozen=True, slots=True)
class IndexDefinition:
    name: str
    label: str
    fields: tuple[str, ...]
    rationale: str

    def key_for_record(self, record: PortRecord) -> tuple[Any, ...]:
        if self.name == "berth_time_composite":
            return (record.berth_id, record.berth_time, record.write_seq)
        if self.name == "berth_time_depart":
            return (record.berth_time, record.depart_time, record.write_seq)
        if self.name == "berth_time_only":
            return (record.berth_time, record.write_seq)
        raise ValueError(f"unknown index definition: {self.name}")

    def scan_bounds(self, query: QuerySpec) -> ScanBounds:
        if self.name == "berth_time_composite":
            if query.kind == "berth_overlap":
                return ScanBounds(
                    lower_key=(query.berth_id,),
                    upper_key=(query.berth_id, query.end, INF_SEQUENCE),
                    access_description=(
                        "按泊位前缀定位，扫描该泊位中靠泊时刻<=查询结束时刻的记录，"
                        "再用离泊时刻过滤重叠"
                    ),
                )
            return ScanBounds(
                lower_key=None,
                upper_key=None,
                access_description=(
                    "索引以泊位编号为前导列，全局靠泊时刻闭区间无法形成单一连续"
                    "键范围，因此做全索引叶链扫描后过滤"
                ),
                full_scan_required=True,
            )

        if self.name == "berth_time_depart":
            if query.kind == "berth_overlap":
                return ScanBounds(
                    lower_key=None,
                    upper_key=(query.end, HIGH_STRING, INF_SEQUENCE),
                    access_description=(
                        "从最左叶开始扫描靠泊时刻<=查询结束时刻的记录，"
                        "再用泊位编号和离泊时刻过滤"
                    ),
                )
            return ScanBounds(
                lower_key=(query.start,),
                upper_key=(query.end, HIGH_STRING, INF_SEQUENCE),
                access_description=(
                    "按靠泊时刻闭区间直接定位，离泊时刻作为键内次级列"
                ),
            )

        if self.name == "berth_time_only":
            if query.kind == "berth_overlap":
                return ScanBounds(
                    lower_key=None,
                    upper_key=(query.end, INF_SEQUENCE),
                    access_description=(
                        "从最左叶开始扫描靠泊时刻<=查询结束时刻的记录，"
                        "再过滤泊位编号和离泊时刻"
                    ),
                )
            return ScanBounds(
                lower_key=(query.start,),
                upper_key=(query.end, INF_SEQUENCE),
                access_description="按靠泊时刻闭区间直接定位",
            )

        raise ValueError(f"unknown index definition: {self.name}")

    def key_to_json(self, key: tuple[Any, ...]) -> list[Any]:
        return [_json_key_part(part) for part in key]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "fields": list(self.fields),
            "unique_tie_breaker": "write_seq",
            "rationale": self.rationale,
        }


def _json_key_part(value: Any) -> Any:
    if isinstance(value, float) and math.isinf(value):
        return "INF"
    return value


INDEX_DEFINITIONS: tuple[IndexDefinition, ...] = (
    IndexDefinition(
        name="berth_time_composite",
        label="泊位编号 + 靠泊时刻",
        fields=("berth_id", "berth_time", "write_seq"),
        rationale=(
            "前导列与泊位区间查询等值条件一致，可直接把扫描限定在一个泊位前缀内"
        ),
    ),
    IndexDefinition(
        name="berth_time_depart",
        label="靠泊时刻 + 离泊时刻",
        fields=("berth_time", "depart_time", "write_seq"),
        rationale=(
            "适合按全局靠泊时刻定位，离泊时刻可参与键排序，但不能先按泊位裁剪"
        ),
    ),
    IndexDefinition(
        name="berth_time_only",
        label="单一靠泊时刻",
        fields=("berth_time", "write_seq"),
        rationale="存储最简、纯靠泊时刻范围定位最快，但离泊重叠和泊位均需回表过滤",
    ),
)


def get_index_definition(name: str) -> IndexDefinition:
    for definition in INDEX_DEFINITIONS:
        if definition.name == name:
            return definition
    raise KeyError(f"unknown index: {name}")
