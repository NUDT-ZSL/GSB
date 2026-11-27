"""验收核对：在人工标注脚本集上运行工作台并比对解析标签与轨迹核对结果。"""

from .labeled_dataset import LABELED_SCRIPT_SET
from .pipeline import run_pipeline
from .sample_data import CAMERA_CONFIG, CASE_BOUNDS, build_vase_obj


def run_acceptance() -> dict:
    texts = [item["text"] for item in LABELED_SCRIPT_SET]
    request = {
        "fps": 25,
        "obj_text": build_vase_obj(),
        "script": texts,
        "case_bounds": CASE_BOUNDS,
        "camera": CAMERA_CONFIG,
    }
    result = run_pipeline(request)
    label_checks = []
    for item, parsed in zip(LABELED_SCRIPT_SET, result["parsed_actions"]):
        label = item["label"]
        mismatches = [
            key for key in ("action_type", "direction", "period", "easing")
            if parsed[key] != label[key]
        ]
        # 幅度允许符号差异（反向动作），比较绝对值
        if abs(abs(parsed["amplitude"]) - abs(label["amplitude"])) > 1e-9:
            mismatches.append("amplitude")
        label_checks.append({
            "text": item["text"],
            "label": label,
            "parsed": {key: parsed[key] for key in label},
            "mismatches": mismatches,
            "label_passed": not mismatches,
        })
    return {
        "label_checks": label_checks,
        "all_labels_passed": all(item["label_passed"] for item in label_checks),
        "all_trajectories_passed": result["verification_report"]["all_passed"],
        "all_cameras_clear": all(
            item["camera_all_frames_clear"]
            for item in result["verification_report"]["actions"]
        ),
    }
