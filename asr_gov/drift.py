"""跨格式裁决与语义漂移检测。

裁决：同一切片集的 v1/v2/v3 三种书写，加载并迁移到 v3 规范化后，
必须产出相同切片集合、相同毫秒级起止与相同文字内容。
漂移：经低表达能力格式（如 v1）往返后不可还原的字段被显式列出，
返回 E_SEMANTIC_DRIFT，不得以默认可接受掩盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import SemanticDrift
from .migrate import migrate

DRIFT_FIELDS = ("token_timings", "confidence_interval", "speaker",
                "segment_label", "revision", "sub_ms_timing")


@dataclass
class AdjudicationReport:
    equal: bool
    mismatches: list = field(default_factory=list)
    slice_ids: list = field(default_factory=list)

    def to_dict(self):
        return {"equal": self.equal, "mismatches": self.mismatches,
                "slice_ids": self.slice_ids}


def _projection(slice_set):
    rows = {}
    for s in slice_set.slices:
        rows[s.utterance_id] = (s.start_ms, s.end_ms, s.text)
    return rows


def adjudicate(sets_by_version: dict) -> AdjudicationReport:
    """输入 {1: v1集, 2: v2集, 3: v3集}，全部迁移到 v3 规范化后比对。"""
    projections = {}
    for version, slice_set in sorted(sets_by_version.items()):
        normalized = migrate(slice_set, 3).slice_set
        projections[version] = _projection(normalized)
    versions = sorted(projections)
    baseline = projections[versions[0]]
    mismatches = []
    for version in versions[1:]:
        current = projections[version]
        if set(current) != set(baseline):
            mismatches.append({
                "kind": "slice_set",
                "detail": f"v{versions[0]} 与 v{version} 切片集合不一致: "
                          f"{sorted(set(baseline) ^ set(current))}",
            })
            continue
        for utt in sorted(baseline):
            if baseline[utt] != current[utt]:
                mismatches.append({
                    "kind": "field",
                    "utterance_id": utt,
                    "detail": f"v{versions[0]}={baseline[utt]} vs v{version}={current[utt]}",
                })
    return AdjudicationReport(equal=not mismatches, mismatches=mismatches,
                              slice_ids=sorted(baseline))


def _restorable_state(slice_set):
    state = {}
    for s in slice_set.slices:
        state[s.utterance_id] = {
            "token_timings": [t.to_dict() for t in s.tokens],
            "confidence_interval": [s.conf_lo, s.conf_hi],
            "speaker": s.speaker,
            "segment_label": s.segment_label,
            "revision": s.revision,
            "sub_ms_timing": (s.start_us % 1000, s.end_us % 1000),
        }
    return state


def roundtrip_drift_check(slice_set, via_version: int):
    """将切片集降级到 via_version 再升回 v3，检查语义是否完全还原。"""
    downgraded = migrate(slice_set, via_version).slice_set
    restored = migrate(downgraded, 3).slice_set
    before = _restorable_state(migrate(slice_set, 3).slice_set)
    after = _restorable_state(restored)
    unrecoverable = []
    for utt in sorted(before):
        for field_name in DRIFT_FIELDS:
            if before[utt][field_name] != after[utt].get(field_name):
                unrecoverable.append({
                    "utterance_id": utt,
                    "field": field_name,
                    "before": before[utt][field_name],
                    "after": after[utt].get(field_name),
                })
    if unrecoverable:
        raise SemanticDrift(
            f"经 v{via_version} 往返后 {len(unrecoverable)} 项语义不可还原",
            clip_id=slice_set.clip_id,
            location=f"roundtrip via v{via_version}",
            details={
                "unrecoverable": unrecoverable,
                "affected_slices": sorted({u["utterance_id"] for u in unrecoverable}),
            },
        )
    return restored
