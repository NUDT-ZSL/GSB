"""Map damage types and severity to one of three repair workflows."""

from __future__ import annotations

from .grading import GRADE_ORDER


WORKFLOW_LABELS = {
    "light_denoise": "轻度降噪",
    "declick_repair": "去脉冲修复",
    "remaster_retranscribe": "母带重新转录",
    "archive_only": "无需修复（归档）",
}


def map_workflow(type_grades: dict[str, str | None], overall_grade: str) -> dict:
    present = {
        key: value
        for key, value in type_grades.items()
        if value in ("light", "medium", "heavy")
    }
    ordered = sorted(present.items(), key=lambda item: (-GRADE_ORDER[item[1]], item[0]))
    basis = "；".join(f"{key}={value}" for key, value in ordered) if ordered else "无受损类型"

    if not present:
        return {
            "code": "archive_only",
            "label": WORKFLOW_LABELS["archive_only"],
            "rationale": "未检出削波、直流偏移、底噪抬升或脉冲爆音，建议直接归档，不做修复性处理。",
            "inputs": {"type_grades": type_grades, "overall_grade": "clean"},
        }

    heavy_types = [key for key, value in present.items() if value == "heavy"]
    medium_types = [key for key, value in present.items() if value == "medium"]
    impulse_present = "impulse" in present
    noise_present = "noise" in present

    if overall_grade == "heavy":
        reasons = []
        if heavy_types:
            reasons.append("存在重度类型: " + ", ".join(sorted(heavy_types)))
        if len(medium_types) >= 2:
            reasons.append("两类及以上中度并存: " + ", ".join(sorted(medium_types)))
        return {
            "code": "remaster_retranscribe",
            "label": WORKFLOW_LABELS["remaster_retranscribe"],
            "rationale": (
                "；".join(reasons)
                + "。重度或组合性劣化在数字修复中易造成不可逆损伤，建议优先回到母带/原始载体重新转录。"
            ),
            "inputs": {"type_grades": type_grades, "overall_grade": overall_grade},
        }

    if impulse_present:
        suffix = "；同时存在底噪抬升，建议去脉冲前先做轻度降噪。" if noise_present else ""
        return {
            "code": "declick_repair",
            "label": WORKFLOW_LABELS["declick_repair"],
            "rationale": f"以脉冲爆音为主要待修类型（{basis}），且未达到重转录条件{suffix}",
            "inputs": {"type_grades": type_grades, "overall_grade": overall_grade},
        }

    return {
        "code": "light_denoise",
        "label": WORKFLOW_LABELS["light_denoise"],
        "rationale": (
            f"仅见轻度/中度非脉冲劣化（{basis}），可先做轻度降噪，并视情况做直流基线校正与削波修复。"
        ),
        "inputs": {"type_grades": type_grades, "overall_grade": overall_grade},
    }
