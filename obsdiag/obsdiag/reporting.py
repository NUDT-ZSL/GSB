"""诊断报告渲染：规范化 JSON + 可逐次比对的文本总结。"""
from __future__ import annotations

from .models import DiagnosticReport, canonical_dumps, canonical_pretty, dataclass_to_jsonable


def render_canonical(report: DiagnosticReport) -> str:
    return canonical_dumps(dataclass_to_jsonable(report))


def render_pretty(report: DiagnosticReport) -> str:
    return canonical_pretty(dataclass_to_jsonable(report))


def render_summary(report: DiagnosticReport) -> str:
    lines = [
        "=== diagnostic summary ===",
        f"session_id={report.session_id}",
        f"run_id={report.run_id}",
        f"topology_version=v{report.topology_version}",
        f"rule_version=v{report.rule_version}",
        f"window_ms={report.window_ms}",
        f"observations={report.observation_cursor}/{report.observation_total}",
        f"alert_events={report.event_count}",
        f"cluster_count={len(report.clusters)}",
        f"business_codes={','.join(report.business_codes) or '-'}",
        f"status={report.status}",
    ]
    for cluster in report.clusters:
        root = cluster.root_causes[0]
        lines.append(
            f"  {cluster.cluster_id} members={len(cluster.members)} "
            f"root={root.service}(in_degree={root.in_degree},severity={root.severity}) "
            f"folded={len(cluster.folded)}"
        )
        for candidate in cluster.root_causes:
            lines.append(
                f"    candidate rank={candidate.rank} service={candidate.service} "
                f"support={len(candidate.supporting_events)} reasons=[{'; '.join(candidate.reasons)}]"
            )
    for injection in report.injections:
        lines.append(
            f"  injection {injection.injection_id} code={injection.code} "
            f"verified={injection.verified} scope={injection.scope}"
        )
        lines.append(f"    expected={list(injection.expected_affected_cluster_ids)}")
        lines.append(f"    actual  ={list(injection.actual_affected_cluster_ids)}")
    for diff in report.version_diffs:
        lines.append(
            f"  version_diff topo v{diff.old_topology_version}->v{diff.new_topology_version} "
            f"invalidated={list(diff.invalidated_cluster_ids)} "
            f"retained={list(diff.retained_cluster_ids)} new={list(diff.new_cluster_ids)}"
        )
    return "\n".join(lines)
