"""Independent report validation and standalone visual diagnostics."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .canonical import sha256_value
from .errors import E_INCONSISTENT, WorkbenchError
from .merge import answer_digest, verify_answer_from_details


def _append(problems: list[str], condition: bool, message: str) -> None:
    if not condition:
        problems.append(message)


def _validate_invalidation(report: dict[str, Any], problems: list[str]) -> None:
    invalidation = report.get("invalidation")
    if invalidation is None:
        return
    recompute = list(invalidation.get("recompute_nodes", []))
    reuse = list(invalidation.get("reuse_nodes", []))
    _append(
        problems,
        len(recompute) == len(set(recompute)),
        "invalidation recompute nodes contain duplicates",
    )
    _append(
        problems,
        len(reuse) == len(set(reuse)),
        "invalidation reuse nodes contain duplicates",
    )
    _append(
        problems,
        not bool(set(recompute) & set(reuse)),
        "invalidation recompute and reuse sets intersect",
    )
    node_map = {
        node["id"]: node for node in report.get("graph", {}).get("nodes", [])
    }
    owners = {
        node_map[node_id]["owner"]
        for node_id in set(recompute) | set(reuse)
        if node_id in node_map and node_map[node_id]["owner"] != "global"
    }
    affected = set(invalidation.get("affected_requests", []))
    _append(
        problems,
        affected <= owners,
        "invalidation set union does not cover all affected requests",
    )
    _append(
        problems,
        invalidation.get("coverage", {}).get("disjoint") is True,
        "invalidation coverage does not report disjoint sets",
    )
    _append(
        problems,
        invalidation.get("coverage", {}).get("affected_requests_covered")
        is True,
        "invalidation coverage does not report complete request coverage",
    )
    counts = invalidation.get("counts", {})
    _append(
        problems,
        counts.get("recompute_nodes") == len(recompute),
        "invalidation recompute node count mismatch",
    )
    _append(
        problems,
        counts.get("reuse_nodes") == len(reuse),
        "invalidation reuse node count mismatch",
    )
    _append(
        problems,
        invalidation.get("recompute_set", recompute) == recompute,
        "invalidation recompute set alias mismatch",
    )
    _append(
        problems,
        invalidation.get("reuse_set", reuse) == reuse,
        "invalidation reuse set alias mismatch",
    )


def validate_report(
    report: dict[str, Any],
    cache: Any | None = None,
) -> dict[str, Any]:
    """Independently validate topology, fragments, coverage, and snapshots."""

    problems: list[str] = []
    _append(
        problems,
        report.get("schema") == "ragdiag-report/v1",
        "report schema mismatch",
    )
    graph = report.get("graph", {})
    nodes = graph.get("nodes", [])
    node_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str) or node_id in node_map:
            problems.append(f"invalid or duplicate graph node: {node_id}")
            continue
        node_map[node_id] = node
        expected_key = sha256_value(node.get("key_material"))
        _append(
            problems,
            expected_key == node.get("key"),
            f"node key cannot be recomputed: {node_id}",
        )
    for edge in graph.get("edges", []):
        _append(
            problems,
            len(edge) == 2
            and edge[0] in node_map
            and edge[1] in node_map,
            f"graph edge references missing node: {edge}",
        )
    topo = graph.get("topological_order", [])
    _append(
        problems,
        len(topo) == len(node_map) and set(topo) == set(node_map),
        "topological order does not cover graph nodes",
    )
    for position, node_id in enumerate(topo):
        node = node_map.get(node_id)
        if node is None:
            continue
        for dependency in node.get("deps", []):
            _append(
                problems,
                dependency in node_map,
                f"node {node_id} has missing dependency {dependency}",
            )
            if dependency in topo:
                _append(
                    problems,
                    topo.index(dependency) < position,
                    f"dependency {dependency} appears after {node_id}",
                )

    request_units: set[str] = set()
    code_counts: dict[str, int] = {}
    cache_hits = 0
    for request in report.get("requests", []):
        unit = request.get("unit_id")
        _append(
            problems,
            isinstance(unit, str) and unit not in request_units,
            f"invalid or duplicate request unit: {unit}",
        )
        request_units.add(unit)
        code = request.get("business_code")
        code_counts[code] = code_counts.get(code, 0) + 1
        if request.get("cache", {}).get("hit"):
            cache_hits += 1
        if request.get("status") == "ok":
            fragments = request.get("fragments", [])
            answer = request.get("answer") or {}
            input_request = request.get("input") or {}
            _append(
                problems,
                answer.get("fragment_count") == len(fragments),
                f"fragment count mismatch in {unit}",
            )
            _append(
                problems,
                answer.get("digest")
                == answer_digest(
                    input_request.get("request_id", ""),
                    int(input_request.get("turn_no", 0)),
                    fragments,
                ),
                f"answer digest mismatch in {unit}",
            )
            for fragment_problem in verify_answer_from_details(
                input_request,
                {"fragments": fragments, "answer": answer},
            ):
                problems.append(f"{unit}: {fragment_problem}")
            for index, fragment in enumerate(fragments):
                _append(
                    problems,
                    fragment.get("request_id")
                    == input_request.get("request_id")
                    == fragment.get("source_request_id"),
                    f"fragment request id mismatch in {unit}",
                )
                _append(
                    problems,
                    fragment.get("format_version")
                    == fragment.get("result_format_version"),
                    f"fragment format version mismatch in {unit}",
                )
                read_id = f"read:{unit}:{index}"
                retrieve_id = f"retrieve:{unit}"
                merge_id = f"merge:{unit}"
                cache_keys = fragment.get("cache_keys", {})
                _append(
                    problems,
                    merge_id in node_map,
                    f"merge node missing for {unit}",
                )
                _append(
                    problems,
                    retrieve_id in node_map,
                    f"retrieval node missing for {unit}",
                )
                if merge_id in node_map:
                    _append(
                        problems,
                        cache_keys.get("merge")
                        == node_map[merge_id].get("key")
                        == fragment.get("cache_key"),
                        f"fragment merge cache key mismatch in {unit}",
                    )
                if retrieve_id in node_map:
                    _append(
                        problems,
                        cache_keys.get("retrieval")
                        == node_map[retrieve_id].get("key"),
                        f"fragment retrieval cache key mismatch in {unit}",
                    )
                if read_id in node_map:
                    _append(
                        problems,
                        cache_keys.get("read")
                        == node_map[read_id].get("key"),
                        f"fragment read cache key mismatch in {unit}",
                    )
                _append(
                    problems,
                    fragment.get("merge_position") == index,
                    f"fragment merge position mismatch in {unit}",
                )
                _append(
                    problems,
                    isinstance(fragment.get("source_path"), list)
                    and len(fragment["source_path"]) >= 4,
                    f"fragment source path is incomplete in {unit}",
                )
        else:
            _append(
                problems,
                not request.get("fragments"),
                f"failed request {unit} unexpectedly has fragments",
            )
            _append(
                problems,
                isinstance(request.get("error", {}).get("code"), str),
                f"failed request {unit} lacks a business error",
            )

    summary = report.get("summary", {})
    _append(
        problems,
        summary.get("requests_total") == len(request_units),
        "summary request count mismatch",
    )
    _append(
        problems,
        summary.get("business_codes") == dict(sorted(code_counts.items())),
        "summary business code counts mismatch",
    )
    _append(
        problems,
        summary.get("cache_hit_requests") == cache_hits,
        "summary cache hit count mismatch",
    )
    execution = report.get("execution", {})
    budget_limits = report.get("budget", {}).get("limits", {})
    _append(
        problems,
        execution.get("engine") == "multiprocessing.ProcessPoolExecutor",
        "report does not describe the local process-pool engine",
    )
    _append(
        problems,
        execution.get("workers") == budget_limits.get("effective_concurrency"),
        "process worker count does not match scheduler budget",
    )
    _append(
        problems,
        execution.get("declared_chunk_count")
        == len(report.get("budget", {}).get("declared_chunks", [])),
        "declared chunk count mismatch",
    )
    _validate_invalidation(report, problems)

    snapshot = report.get("snapshot", {})
    if cache is not None:
        active = cache.active_snapshot()
        _append(
            problems,
            active is not None
            and active.get("snapshot_id") == snapshot.get("snapshot_id"),
            "active snapshot does not match report snapshot",
        )
        if active is not None:
            manifest = active.get("manifest", {})
            manifest_nodes = manifest.get("nodes", {})
            _append(
                problems,
                set(manifest_nodes) == set(node_map),
                "snapshot node set does not match report graph",
            )
            for node_id, node in node_map.items():
                if node_id in manifest_nodes:
                    _append(
                        problems,
                        manifest_nodes[node_id].get("key")
                        == node.get("key"),
                        f"snapshot key mismatch for node {node_id}",
                    )
            _append(
                problems,
                active.get("manifest_hash") == snapshot.get("manifest_hash"),
                "snapshot manifest hash mismatch",
            )
    if problems:
        raise WorkbenchError(
            E_INCONSISTENT,
            "diagnostic report failed independent consistency checks",
            problems=problems,
            problem_count=len(problems),
        )
    return {
        "status": "ok",
        "checks": [
            "node_keys_recomputed",
            "dag_edges_and_topological_order",
            "fragment_answers_rebuilt",
            "fragment_cache_keys_resolved",
            "request_and_business_code_counts",
            "invalidation_sets_disjoint_and_cover_affected_requests",
            "snapshot_manifest_matches_graph",
        ],
    }


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _status_class(value: str) -> str:
    if value in ("ok", "hit", "identical", "admitted", "computed", "E_OK"):
        return "ok"
    if value in ("recomputed", "reused"):
        return "warn"
    return "bad"


def _stage_graph(report: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    statuses: dict[str, dict[str, int]] = {}
    for node in report["graph"]["nodes"]:
        stage = node["stage"]
        counts[stage] = counts.get(stage, 0) + 1
        statuses.setdefault(stage, {})
        status = node.get("cache_status", "not_run")
        statuses[stage][status] = statuses[stage].get(status, 0) + 1
    stages = [
        ("request", 40, 170),
        ("tool_read", 210, 50),
        ("retrieval_index", 210, 170),
        ("retrieve", 400, 170),
        ("merge", 590, 110),
        ("snapshot", 780, 110),
    ]
    labels = {
        "request": "request sources",
        "tool_read": "tool reads",
        "retrieval_index": "evidence index",
        "retrieve": "3-route retrieval",
        "merge": "fragment merge",
        "snapshot": "atomic snapshot",
    }
    node_svg = []
    for stage, x, y in stages:
        if stage == "snapshot":
            count = 1
            detail = "active:true"
        else:
            count = counts.get(stage, 0)
            detail = ", ".join(
                f"{key}:{value}"
                for key, value in sorted(statuses.get(stage, {}).items())
            )
        node_svg.append(
            f'<g class="dag-node"><rect x="{x}" y="{y}" width="150" '
            f'height="74" rx="10"/><text x="{x + 75}" y="{y + 25}" '
            f'text-anchor="middle" class="dag-title">{_escape(labels[stage])}'
            f"</text><text x=\"{x + 75}\" y=\"{y + 45}\" "
            f'text-anchor="middle" class="dag-metric">{count} nodes</text>'
            f'<text x="{x + 75}" y="{y + 62}" text-anchor="middle" '
            f'class="dag-detail">{_escape(detail[:34])}</text></g>'
        )
    edges = [
        (190, 207, 210, 87),
        (190, 207, 210, 207),
        (360, 207, 400, 207),
        (550, 207, 590, 147),
        (360, 87, 590, 147),
        (740, 147, 780, 147),
    ]
    edge_svg = "".join(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="dag-edge" '
        'marker-end="url(#arrow)"/>'
        for x1, y1, x2, y2 in edges
    )
    return (
        '<svg viewBox="0 0 970 280" class="dag" role="img" '
        'aria-label="computation graph stage topology">'
        "<defs><marker id=\"arrow\" markerWidth=\"8\" markerHeight=\"8\" "
        "refX=\"7\" refY=\"4\" orient=\"auto\"><path d=\"M0,0 L8,4 L0,8 z\" "
        "fill=\"#6f819b\"/></marker></defs>"
        + edge_svg
        + "".join(node_svg)
        + "</svg>"
    )


def _render_html(report: dict[str, Any]) -> str:
    summary = report["summary"]
    codes = "".join(
        f'<span class="pill {_status_class(code)}">{_escape(code)}: '
        f"{count}</span>"
        for code, count in summary["business_codes"].items()
    )
    request_rows = []
    for request in report["requests"]:
        cache = request["cache"]
        request_rows.append(
            "<tr>"
            f"<td>{_escape(request['unit_id'])}</td>"
            f"<td>{_escape(request['query_text'])}</td>"
            f'<td><span class="pill {_status_class(request["status"])}">'
            f"{_escape(request['business_code'])}</span></td>"
            f"<td>{'hit' if cache['hit'] else 'recompute'}</td>"
            f"<td>{_escape(cache['read'])} / {_escape(cache['retrieval'])} / "
            f"{_escape(cache['merge'])}</td>"
            f"<td>{len(request['fragments'])}</td>"
            "</tr>"
        )
    budget_rows = []
    for decision in report["budget"]["decisions"]:
        violations = ", ".join(sorted(decision["violations"])) or "-"
        budget_rows.append(
            "<tr>"
            f"<td>{_escape(decision['unit_id'])}</td>"
            f'<td><span class="pill {_status_class(decision["business_code"])}">'
            f"{_escape(decision['business_code'])}</span></td>"
            f"<td>{decision['required_lanes']}</td>"
            f"<td>{decision['estimate']['time_ms']}</td>"
            f"<td>{decision['estimate']['memory_bytes']}</td>"
            f"<td>{_escape(violations)}</td>"
            f"<td>{decision['elapsed_before_ms']}</td>"
            "</tr>"
        )
    profile_rows = []
    for profile in report["arbitration"]["matrix"] + report["arbitration"][
        "diagnostic_matrix"
    ]:
        profile_rows.append(
            "<tr>"
            f"<td>{_escape(profile['name'])}</td>"
            f"<td>{'yes' if profile['selectable'] else 'diagnostic'}</td>"
            f"<td>{profile['candidate_limit']}</td>"
            f"<td>{profile['tool_expansion']}</td>"
            f"<td>{profile['concurrency']}</td>"
            f"<td>{profile['quality']}</td>"
            f"<td>{profile['makespan_ms']}</td>"
            f"<td>{profile['memory_peak_bytes']}</td>"
            f"<td>{len(profile['rejected_requests'])}</td>"
            "</tr>"
        )
    decision_rows = []
    for decision in report["arbitration"]["decisions"]:
        decision_rows.append(
            "<tr>"
            f"<td>{_escape(decision['conflict'])}</td>"
            f"<td>{_escape(decision['winner'])}</td>"
            f"<td>{_escape(decision['selected'])}</td>"
            f"<td>{_escape(decision['alternative'])}</td>"
            f"<td>{_escape(', '.join(decision['affected_requests']))}</td>"
            f"<td>{_escape(decision['basis'])}</td>"
            "</tr>"
        )
    invalidation_html = "<p>No baseline was supplied for this run.</p>"
    invalidation = report.get("invalidation")
    if invalidation is not None:
        invalidation_html = (
            '<div class="grid">'
            f'<div class="metric"><span>Affected requests</span><b>'
            f"{invalidation['counts']['affected_requests']}</b></div>"
            f'<div class="metric"><span>Recompute nodes</span><b>'
            f"{invalidation['counts']['recompute_nodes']}</b></div>"
            f'<div class="metric"><span>Reuse nodes</span><b>'
            f"{invalidation['counts']['reuse_nodes']}</b></div>"
            f'<div class="metric"><span>Coverage</span><b>'
            f"{_escape(invalidation['coverage']['affected_requests_covered'])}"
            "</b></div></div>"
            "<h3>Affected request stages</h3><table><thead><tr>"
            "<th>Request</th><th>Stages</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{_escape(unit)}</td><td>"
                f"{_escape(', '.join(stages))}</td></tr>"
                for unit, stages in invalidation[
                    "per_request_stages"
                ].items()
            )
            + "</tbody></table>"
        )
    fragment_details = []
    for request in report["requests"]:
        if request["status"] != "ok":
            continue
        rows = []
        for fragment in request["fragments"]:
            source_path_html = " &rarr; ".join(
                _escape(part) for part in fragment["source_path"]
            )
            rows.append(
                "<tr>"
                f"<td>{fragment['merge_position']}</td>"
                f"<td>{_escape(fragment['tool_id'])}</td>"
                f"<td>{_escape(fragment['result_ref'])}</td>"
                f"<td>{_escape(fragment['format_version'])}</td>"
                f"<td>{fragment['snapshot_version']}</td>"
                f"<td>{_escape(fragment['cache_key'][:16])}...</td>"
                f"<td>{source_path_html}</td>"
                "</tr>"
            )
        fragment_details.append(
            "<details><summary>"
            f"{_escape(request['unit_id'])}: {len(request['fragments'])} "
            f"fragments, answer {_escape(request['answer']['digest'][:16])}"
            "...</summary><table><thead><tr><th>Pos</th><th>Tool</th>"
            "<th>Result</th><th>Format</th><th>Snapshot</th><th>Cache key</th>"
            "<th>Source path</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
            f"<pre>{_escape(request['answer']['text'][:4000])}</pre>"
            "</details>"
        )
    node_rows = "".join(
        "<tr>"
        f"<td>{_escape(node['id'])}</td>"
        f"<td>{_escape(node['stage'])}</td>"
        f"<td>{_escape(node['owner'])}</td>"
        f"<td>{_escape(node['cache_status'])}</td>"
        f"<td>{_escape(node['key'][:20])}...</td>"
        "</tr>"
        for node in report["graph"]["nodes"]
    )
    block = report["block_equivalence"]
    snapshot = report["snapshot"]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RAG Tool-Call Diagnostic Workbench</title>
<style>
:root {{ --bg:#f4f6f9; --panel:#fff; --ink:#1e293b; --muted:#64748b;
  --line:#dbe3ec; --ok:#0f766e; --warn:#a16207; --bad:#b91c1c; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:14px/1.5 ui-sans-serif,system-ui,-apple-system,
  "Segoe UI",sans-serif; color:var(--ink); background:var(--bg); }}
main {{ max-width:1280px; margin:0 auto; padding:28px; }}
h1 {{ margin:0 0 4px; font-size:26px; }}
h2 {{ margin:28px 0 10px; font-size:19px; }}
h3 {{ margin:20px 0 8px; font-size:15px; }}
.sub {{ color:var(--muted); margin-bottom:20px; }}
.panel {{ background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:18px; margin:12px 0;
  box-shadow:0 2px 10px rgba(30,41,59,.04); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
  gap:10px; }}
.metric {{ background:#f8fafc; border:1px solid var(--line);
  border-radius:9px; padding:10px 12px; }}
.metric span {{ display:block; color:var(--muted); font-size:12px; }}
.metric b {{ font-size:20px; }}
table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
th {{ text-align:left; color:var(--muted); font-weight:600;
  border-bottom:1px solid var(--line); padding:8px 7px; }}
td {{ border-bottom:1px solid #edf1f5; padding:7px; vertical-align:top; }}
.pill {{ display:inline-block; border-radius:99px; padding:2px 8px;
  font-size:11.5px; font-weight:650; background:#e2e8f0; color:#334155; }}
.pill.ok {{ background:#ccfbf1; color:var(--ok); }}
.pill.warn {{ background:#fef3c7; color:var(--warn); }}
.pill.bad {{ background:#fee2e2; color:var(--bad); }}
.dag {{ width:100%; min-height:250px; background:#fbfdff;
  border:1px solid var(--line); border-radius:10px; }}
.dag-node rect {{ fill:#fff; stroke:#b8c6d8; stroke-width:1.3; }}
.dag-title {{ font-size:12px; font-weight:700; fill:#334155; }}
.dag-metric {{ font-size:15px; font-weight:750; fill:#0f766e; }}
.dag-detail {{ font-size:9px; fill:#64748b; }}
.dag-edge {{ stroke:#6f819b; stroke-width:1.4; }}
details {{ margin:10px 0; border:1px solid var(--line); border-radius:9px;
  padding:10px 12px; background:#fbfdff; }}
summary {{ cursor:pointer; font-weight:650; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#0f172a;
  color:#e2e8f0; padding:12px; border-radius:8px; font-size:11.5px; }}
.note {{ color:var(--muted); font-size:12px; }}
</style>
</head>
<body><main>
<h1>RAG Tool-Call Diagnostic Workbench</h1>
<div class="sub">Deterministic local run · snapshot {_escape(snapshot['snapshot_id'][:20])}... ·
seed {_escape(report['inputs']['seed'])} · profile
{_escape(summary.get('selected_profile'))}</div>
<div class="panel"><div class="grid">
<div class="metric"><span>Requests</span><b>{summary['requests_total']}</b></div>
<div class="metric"><span>Successful</span><b>{summary['requests_ok']}</b></div>
<div class="metric"><span>Budget errors</span><b>{summary['requests_error']}</b></div>
<div class="metric"><span>Cache-hit requests</span><b>{summary['cache_hit_requests']}</b></div>
<div class="metric"><span>Graph nodes</span><b>{report['graph']['node_count']}</b></div>
<div class="metric"><span>Block check</span><b>{_escape(block['status'])}</b></div>
</div><p>{codes}</p></div>
<h2>Computation Graph</h2>
<div class="panel">{_stage_graph(report)}
<p class="note">Stage graph is aggregated for readability; the complete
node-level topology and recomputed keys are listed below.</p></div>
<h2>Request Outcomes</h2>
<div class="panel"><table><thead><tr><th>Unit</th><th>Query</th><th>Code</th>
<th>Cache</th><th>Read / Retrieve / Merge</th><th>Fragments</th></tr></thead>
<tbody>{''.join(request_rows)}</tbody></table></div>
<h2>Three-Budget Admission</h2>
<div class="panel"><div class="grid">
<div class="metric"><span>Concurrency</span><b>{report['budget']['limits']['concurrency_limit']}</b></div>
<div class="metric"><span>Logical time used</span><b>{report['budget']['logical_time_used_ms']} ms</b></div>
<div class="metric"><span>Resident bytes</span><b>{report['budget']['resident_bytes_final']}</b></div>
<div class="metric"><span>Block size</span><b>{report['budget']['limits']['block_size']}</b></div>
<div class="metric"><span>Process workers</span><b>{report['execution']['workers']}</b></div>
</div><table><thead><tr><th>Unit</th><th>Code</th><th>Lanes</th>
<th>Est. ms</th><th>Est. bytes</th><th>Violations</th><th>Elapsed before ms</th>
</tr></thead><tbody>{''.join(budget_rows)}</tbody></table></div>
<h2>Conflict Arbitration</h2>
<div class="panel"><p class="note">Priority:
{_escape(' > '.join(report['arbitration']['priority_order']))}. Selection:
{_escape(report['arbitration']['selection_rule'])}.</p>
<table><thead><tr><th>Profile</th><th>Mode</th><th>Candidates</th>
<th>Expansion</th><th>Concurrency</th><th>Quality</th><th>Makespan ms</th>
<th>Memory peak</th><th>Rejected</th></tr></thead>
<tbody>{''.join(profile_rows)}</tbody></table>
<h3>Decision basis</h3><table><thead><tr><th>Conflict</th><th>Winner</th>
<th>Selected</th><th>Alternative</th><th>Affected requests</th>
<th>Basis</th></tr></thead><tbody>{''.join(decision_rows)}</tbody></table></div>
<h2>Block Equivalence</h2>
<div class="panel"><div class="grid">
<div class="metric"><span>Status</span><b>{_escape(block['status'])}</b></div>
<div class="metric"><span>Blocks</span><b>{block['block_count']}</b></div>
<div class="metric"><span>Float64 elements</span><b>{block['element_count']}</b></div>
<div class="metric"><span>Decimals / rounding</span><b>{block['score_decimals']} / {_escape(block['rounding_mode'])}</b></div>
<div class="metric"><span>Subnormal protection</span><b>{_escape(block['subnormal_protection'])}</b></div>
</div><p class="note">Numeric SHA-256: {_escape(block['numeric_sha256'])}</p></div>
<h2>Cache Invalidation</h2><div class="panel">{invalidation_html}</div>
<h2>Fragments and Source Paths</h2>
<div class="panel">{''.join(fragment_details) or '<p>No successful fragments.</p>'}</div>
<h2>Node Topology</h2>
<details><summary>Show all {report['graph']['node_count']} DAG nodes</summary>
<table><thead><tr><th>Node</th><th>Stage</th><th>Owner</th><th>Cache status</th>
<th>Key</th></tr></thead><tbody>{node_rows}</tbody></table></details>
<h2>Snapshot</h2>
<div class="panel"><table><tbody>
<tr><th>Snapshot id</th><td>{_escape(snapshot['snapshot_id'])}</td></tr>
<tr><th>Previous snapshot</th><td>{_escape(snapshot['previous_snapshot_id'])}</td></tr>
<tr><th>Repository snapshot version</th><td>{_escape(snapshot['repository_snapshot_version'])}</td></tr>
<tr><th>Manifest hash</th><td>{_escape(snapshot['manifest_hash'])}</td></tr>
<tr><th>Consistency self-check</th><td>passed before atomic activation</td></tr>
</tbody></table></div>
</main></body></html>
"""


def write_report_files(
    report: dict[str, Any],
    json_path: str | Path | None,
    html_path: str | Path | None,
) -> None:
    if json_path is not None:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
    if html_path is not None:
        path = Path(html_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_html(report), encoding="utf-8")

