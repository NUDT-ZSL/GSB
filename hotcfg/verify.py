"""结果一致性校验: 按结果摘要中的版本号重取生效值重算并逐任务比较。"""
from __future__ import annotations

from .errors import BizError, E_VERIFY


def verify_results(center, tasks: dict, results: list[dict]) -> dict:
    """逐任务重算结果摘要并比较。

    不一致时以 E_VERIFY 报告首个偏差任务、期望版本与实际版本。
    """
    checked = 0
    for result in results:
        task = tasks.get(result["task_id"])
        if task is None:
            raise BizError(E_VERIFY, f"result references unknown task: {result['task_id']}",
                           {"task_id": result["task_id"]})
        effective = center.get_effective(result["version"], keys=task.spec.keys)
        recomputed = task.compute_digest(effective, result["version"])
        if recomputed["value_fp"] != result["value_fp"]:
            raise BizError(
                E_VERIFY,
                f"result mismatch for task {result['task_id']}",
                {
                    "task_id": result["task_id"],
                    "expected_version": result["version"],
                    "actual_version": center.current_version,
                    "expected_value_fp": recomputed["value_fp"],
                    "actual_value_fp": result["value_fp"],
                },
            )
        checked += 1
    return {"checked": checked, "ok": True}
