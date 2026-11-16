"""Deterministic structured diagnostics (JSONL) and text reports.

No timestamps or random ordering: entries carry a monotonic sequence
number so repeated runs over the same inputs are byte-identical.
"""

import json


class DiagLog:
    def __init__(self, path=None):
        self.path = path
        self.entries = []
        self._seq = 0

    def emit(self, code, message, **context):
        self._seq += 1
        entry = {"seq": self._seq, "code": code, "message": message}
        entry.update(context)
        self.entries.append(entry)
        return entry

    def flush(self):
        if self.path is None:
            return
        with open(self.path, "w", encoding="utf-8") as fh:
            for e in self.entries:
                fh.write(json.dumps(e, sort_keys=True, ensure_ascii=False)
                         + "\n")


def render_pod_report(report):
    """Stable-ordered text rendering of one pod's score propagation."""
    lines = []
    lines.append(f"POD {report['pod']}")
    if report["rejected_nodes"]:
        lines.append("  filtered-out nodes:")
        for r in report["rejected_nodes"]:
            for reason in r["reasons"]:
                lines.append(f"    - {r['node']} [{reason['stage']}] "
                             f"{reason['reason']}")
    if not report["scores"]:
        lines.append("  no feasible node; pod remains unscheduled")
        return lines
    for s in report["scores"]:
        mark = " <== WINNER" if s["node"] == report["winner"] else ""
        lines.append(f"  node {s['node']}: score {s['score_points']} "
                     f"({s['score_units']} units, grid={s['grid']}) "
                     f"error-bound +/-{s['error_bound_points']} "
                     f"({s['error_bound_units']} units){mark}")
        for p in s["plugins"]:
            if p["status"] == "ok":
                contrib = p["contribution_units"]
                contrib = "-" if contrib is None else str(contrib)
                lines.append(f"      {p['plugin']}: raw={p['raw']} "
                             f"weight={p['weight']} "
                             f"contribution={contrib} units "
                             f"inputs={json.dumps(p['inputs'], sort_keys=True)}")
            else:
                lines.append(f"      {p['plugin']}: UNAVAILABLE "
                             f"({p['reason']}); weight {p['weight']} "
                             f"excluded from normalization")
    if report["tie_group"]:
        lines.append(f"  indistinguishable tie group: "
                     f"{sorted([report['winner']] + report['tie_group'])}")
    lines.append(f"  decision: bind -> {report['winner']}")
    lines.append(f"  tie-break: {report['tie_break_reason']}")
    return lines


def render_run_summary(result):
    lines = []
    for report in result["reports"]:
        lines.extend(render_pod_report(report))
    lines.append("BINDINGS: " + json.dumps(result["bindings"],
                                           sort_keys=True))
    return lines
