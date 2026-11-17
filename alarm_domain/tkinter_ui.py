"""Optional Tkinter diagnostic viewer; it never owns business processing."""

from __future__ import annotations

from typing import Any, Mapping


class UiUnavailable(RuntimeError):
    pass


def launch_diagnostic_view(model: Mapping[str, Any], *, title: str = "Alarm Diagnostics") -> None:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        raise UiUnavailable(
            "tkinter is unavailable; install the standard Windows Python distribution"
        ) from exc

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise UiUnavailable(f"cannot start Tkinter display: {exc}") from exc

    root.title(title)
    root.geometry("1280x820")
    root.minsize(980, 640)

    header = ttk.Frame(root, padding=8)
    header.pack(fill="x")
    versions = model.get("versions", {})
    ttk.Label(
        header,
        text=(
            f"rules={versions.get('rules')}  topology={versions.get('topology')}  "
            f"window_ms={model.get('window_ms')}  clusters={model.get('cluster_count')}  "
            f"cursor={model.get('event_cursor')}/{model.get('event_count')}"
        ),
        anchor="w",
    ).pack(fill="x")

    panes = ttk.Panedwindow(root, orient="horizontal")
    panes.pack(fill="both", expand=True, padx=8, pady=4)

    left = ttk.Frame(panes)
    right = ttk.Frame(panes)
    panes.add(left, weight=1)
    panes.add(right, weight=2)

    rules_frame = ttk.LabelFrame(left, text="Rules", padding=4)
    rules_frame.pack(fill="both", expand=True)
    rules_tree = ttk.Treeview(
        rules_frame,
        columns=("service", "severity", "condition"),
        show="headings",
        height=8,
    )
    for column, heading in (
        ("service", "Service"),
        ("severity", "Severity"),
        ("condition", "Condition"),
    ):
        rules_tree.heading(column, text=heading)
        rules_tree.column(column, width=130 if column != "condition" else 240)
    rules_scroll = ttk.Scrollbar(rules_frame, orient="vertical", command=rules_tree.yview)
    rules_tree.configure(yscrollcommand=rules_scroll.set)
    rules_tree.pack(side="left", fill="both", expand=True)
    rules_scroll.pack(side="right", fill="y")
    for rule in model.get("rules", []):
        rules_tree.insert(
            "",
            "end",
            iid=rule["rule_id"],
            values=(rule["service"], rule["severity"], rule["condition"]),
        )

    edges_frame = ttk.LabelFrame(left, text="Dependency Edges", padding=4)
    edges_frame.pack(fill="both", expand=True, pady=(6, 0))
    edges_tree = ttk.Treeview(
        edges_frame,
        columns=("upstream", "downstream", "weight"),
        show="headings",
        height=7,
    )
    for column, heading in (
        ("upstream", "Upstream"),
        ("downstream", "Downstream"),
        ("weight", "Weight"),
    ):
        edges_tree.heading(column, text=heading)
        edges_tree.column(column, width=150)
    edges_tree.pack(fill="both", expand=True)
    for index, edge in enumerate(model.get("edges", [])):
        edges_tree.insert(
            "",
            "end",
            iid=f"edge-{index}",
            values=(edge["upstream"], edge["downstream"], edge["weight"]),
        )

    clusters_frame = ttk.LabelFrame(right, text="Clusters and Root Causes", padding=4)
    clusters_frame.pack(fill="both", expand=True)
    clusters_tree = ttk.Treeview(
        clusters_frame,
        columns=("time", "services", "root", "members", "folded"),
        show="headings",
        height=10,
    )
    for column, heading in (
        ("time", "Time"),
        ("services", "Services"),
        ("root", "Root Candidate"),
        ("members", "Members"),
        ("folded", "Folded"),
    ):
        clusters_tree.heading(column, text=heading)
        clusters_tree.column(column, width=150 if column != "time" else 170)
    clusters_tree.pack(fill="both", expand=True)
    for cluster in model.get("clusters", []):
        clusters_tree.insert(
            "",
            "end",
            iid=cluster["cluster_id"],
            values=(
                f"{cluster['start_ts']}..{cluster['end_ts']}",
                ",".join(cluster["services"]),
                cluster["root_candidate"],
                ",".join(cluster["members"]),
                cluster["folded_count"],
            ),
        )

    detail = tk.Text(right, wrap="word", height=18, font=("Consolas", 9))
    detail.pack(fill="both", expand=True, pady=(6, 0))

    def render_cluster(_event: object | None = None) -> None:
        selection = clusters_tree.selection()
        detail.delete("1.0", "end")
        if not selection:
            invalidation = model.get("invalidation", {}).get("latest_trace")
            snapshot = model.get("snapshot")
            detail.insert(
                "end",
                f"Latest invalidation: {invalidation}\n\nSnapshot diff: {snapshot}\n",
            )
            return
        target = next(
            cluster for cluster in model.get("clusters", []) if cluster["cluster_id"] == selection[0]
        )
        lines = [
            f"Cluster {target['cluster_id']}",
            f"Coverage: {target['start_ts']}..{target['end_ts']}",
            f"Services: {', '.join(target['services'])}",
            f"Selected root: {target['root_candidate']}",
            f"Reason: {target['root_reason']}",
            "",
            "Candidates:",
        ]
        for candidate in target["candidates"]:
            lines.append(
                f"  {candidate['rank']}. {candidate['service']}  "
                f"support={candidate['support_alerts']}  {candidate['reason']}"
            )
        lines.extend(
            [
                "",
                f"Retained alerts ({target['retained_count']}): "
                + ", ".join(item["event_id"] for item in target["retained_alerts"]),
                f"Folded alerts ({target['folded_count']}): "
                + ", ".join(
                    f"{item['event_id']}->{item['folded_into']}"
                    for item in target["folded_alerts"]
                ),
            ]
        )
        detail.insert("end", "\n".join(lines) + "\n")

    clusters_tree.bind("<<TreeviewSelect>>", render_cluster)

    footer = ttk.Frame(root, padding=8)
    footer.pack(fill="x")
    invalidation = model.get("invalidation", {}).get("latest_trace") or {}
    ttk.Label(
        footer,
        text=(
            f"Invalidated: {invalidation.get('invalidated_clusters', [])}  "
            f"recomputed={invalidation.get('recomputed_count', 0)}  "
            f"reused={invalidation.get('reused_count', 0)}  "
            f"counts_consistent={invalidation.get('counts_consistent')}"
        ),
        anchor="w",
    ).pack(fill="x")
    snapshot = model.get("snapshot") or {}
    ttk.Label(
        footer,
        text=(
            "Snapshot version diff: "
            f"{(snapshot.get('version_diff') or {}).get('dimensions')}  "
            f"replay_match={(snapshot.get('replay') or {}).get('match')}"
        ),
        anchor="w",
    ).pack(fill="x")

    render_cluster()
    root.mainloop()
