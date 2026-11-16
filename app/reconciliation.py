from __future__ import annotations

from typing import Any


def comparable_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Remove run-local evidence (ids, timestamps, URLs) before comparison."""
    businesses: list[dict[str, Any]] = []
    for raw_business in summary.get("businesses", []):
        records = []
        for record in raw_business.get("records", []):
            records.append(
                {
                    "schema_version": record["schema_version"],
                    "revision": int(record["revision"]),
                    "unresolved": bool(record["unresolved"]),
                    "fields": record["fields"],
                    "field_hash": record["field_hash"],
                }
            )
        records.sort(
            key=lambda item: (
                int(item["revision"]),
                str(item["schema_version"]),
                item["field_hash"],
            )
        )
        edges = []
        for edge in raw_business.get("edges", []):
            edges.append(
                {
                    "from_revision": int(edge["from_revision"]),
                    "to_revision": int(edge["to_revision"]),
                    "added_fields": sorted(edge["added_fields"]),
                    "missing_fields": sorted(edge["missing_fields"]),
                }
            )
        edges.sort(key=lambda item: (item["from_revision"], item["to_revision"]))
        businesses.append(
            {
                "business_id": raw_business["business_id"],
                "records": records,
                "edges": edges,
            }
        )
    businesses.sort(key=lambda item: item["business_id"])
    return {
        "businesses": businesses,
        "business_count": len(businesses),
        "record_count": sum(len(item["records"]) for item in businesses),
        "edge_count": sum(len(item["edges"]) for item in businesses),
    }


def _diff_values(left: Any, right: Any, path: str, differences: list[dict[str, Any]]) -> None:
    if type(left) is not type(right):
        differences.append({"path": path, "left": left, "right": right})
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                differences.append({"path": f"{path}.{key}", "left": None, "right": right[key]})
            elif key not in right:
                differences.append({"path": f"{path}.{key}", "left": left[key], "right": None})
            else:
                _diff_values(left[key], right[key], f"{path}.{key}", differences)
        return
    if isinstance(left, list):
        if len(left) != len(right):
            differences.append({"path": f"{path}.length", "left": len(left), "right": len(right)})
        for index, (a, b) in enumerate(zip(left, right)):
            _diff_values(a, b, f"{path}[{index}]", differences)
        return
    if left != right:
        differences.append({"path": path, "left": left, "right": right})


def compare_summaries(
    left_raw: dict[str, Any], right_raw: dict[str, Any]
) -> dict[str, Any]:
    left = comparable_summary(left_raw)
    right = comparable_summary(right_raw)
    left_map = {item["business_id"]: item for item in left["businesses"]}
    right_map = {item["business_id"]: item for item in right["businesses"]}
    business_ids = sorted(set(left_map) | set(right_map))
    comparisons: list[dict[str, Any]] = []
    for business_id in business_ids:
        differences: list[dict[str, Any]] = []
        if business_id not in left_map:
            differences.append({"path": "business", "left": None, "right": "present"})
        elif business_id not in right_map:
            differences.append({"path": "business", "left": "present", "right": None})
        else:
            _diff_values(left_map[business_id], right_map[business_id], business_id, differences)
        comparisons.append(
            {
                "business_id": business_id,
                "identical": not differences,
                "left_records": len(left_map.get(business_id, {}).get("records", [])),
                "right_records": len(right_map.get(business_id, {}).get("records", [])),
                "left_edges": len(left_map.get(business_id, {}).get("edges", [])),
                "right_edges": len(right_map.get(business_id, {}).get("edges", [])),
                "differences": differences,
            }
        )
    return {
        "identical": all(item["identical"] for item in comparisons),
        "business_count": len(business_ids),
        "record_count": {"left": left["record_count"], "right": right["record_count"]},
        "edge_count": {"left": left["edge_count"], "right": right["edge_count"]},
        "comparisons": comparisons,
        "left": left,
        "right": right,
    }


def build_markdown(report: dict[str, Any]) -> str:
    verdict = "PASS" if report["identical"] else "FAIL"
    lines = [
        "# 跨配置一致性对账",
        "",
        f"- 结论：**{verdict}**",
        f"- 运行 A：并发 {report['runs'][0]['concurrency']}，延迟 {report['runs'][0]['delay_profile']}，异常 {report['runs'][0]['fault_profile']}",
        f"- 运行 B：并发 {report['runs'][1]['concurrency']}，延迟 {report['runs'][1]['delay_profile']}，异常 {report['runs'][1]['fault_profile']}",
        f"- 业务标识数：{report['comparison']['business_count']}",
        f"- 规范化记录数：A={report['comparison']['record_count']['left']} / B={report['comparison']['record_count']['right']}",
        f"- 关联边数：A={report['comparison']['edge_count']['left']} / B={report['comparison']['edge_count']['right']}",
        "",
        "| 业务标识 | 记录 A/B | 边 A/B | 结论 | 差异路径 |",
        "|---|---:|---:|---|---|",
    ]
    for item in report["comparison"]["comparisons"]:
        paths = ", ".join(diff["path"] for diff in item["differences"][:5]) or "—"
        lines.append(
            f"| `{item['business_id']}` | {item['left_records']}/{item['right_records']} | "
            f"{item['left_edges']}/{item['right_edges']} | "
            f"{'一致' if item['identical'] else '差异'} | {paths} |"
        )
    lines.extend(
        [
            "",
            "对账字段：规范化字段集合及字段哈希、schemaVersion、修订号、unresolved 标记、"
            "每条关联边的 added_fields 与 missing_fields。运行局部 ID、时间戳、URL 与原始响应引用不参与等价比较。",
            "",
        ]
    )
    return "\n".join(lines)
