"""Tkinter 可视化工作台。

五个标签页均以确定性顺序渲染（服务名/簇 ID/时间戳排序）：
拓扑与依赖边、告警簇、根因候选、折叠衍生告警、注入轨迹与版本。
无显示环境时 Tk() 抛出 TclError，由入口给出明确提示。
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

from .injection import inject_gap, inject_order
from .persistence import WorkbenchStore
from .pipeline import run_pipeline, run_topology_evolution, rerun_gap_scenario, rerun_order_scenario
from .reporting import render_summary


class WorkbenchApp:
    def __init__(self, root: tk.Tk, workdir: str):
        self.root = root
        self.workdir = workdir
        os.makedirs(workdir, exist_ok=True)
        self.store = WorkbenchStore(os.path.join(workdir, "workbench.sqlite3"))
        self.report = None
        self.topology = None
        self.paths: dict[str, str] = {}

        root.title("云原生可观测性告警关联与诊断工作台")
        root.geometry("1080x760")
        self._build_controls()
        self._build_tabs()

    def _build_controls(self) -> None:
        frame = ttk.Frame(self.root, padding=8)
        frame.pack(fill=tk.X)
        ttk.Label(frame, text="拓扑 JSON:").grid(row=0, column=0, sticky=tk.W)
        self.topology_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.topology_var, width=46).grid(row=0, column=1)
        ttk.Label(frame, text="规则 JSON:").grid(row=0, column=2, sticky=tk.W)
        self.rules_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.rules_var, width=40).grid(row=0, column=3)
        ttk.Label(frame, text="观测 NDJSON:").grid(row=1, column=0, sticky=tk.W)
        self.obs_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.obs_var, width=46).grid(row=1, column=1)
        ttk.Label(frame, text="窗口ms:").grid(row=1, column=2, sticky=tk.W)
        self.window_var = tk.StringVar(value="5000")
        ttk.Entry(frame, textvariable=self.window_var, width=10).grid(row=1, column=3, sticky=tk.W)
        ttk.Button(frame, text="加载并全量关联", command=self.action_run).grid(row=2, column=0, pady=6)
        ttk.Button(frame, text="注入 E_ORDER", command=lambda: self.action_inject("E_ORDER")).grid(row=2, column=1, sticky=tk.W)
        ttk.Button(frame, text="注入 E_GAP", command=lambda: self.action_inject("E_GAP")).grid(row=2, column=2, sticky=tk.W)
        self.changeset_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.changeset_var, width=40).grid(row=3, column=1)
        ttk.Label(frame, text="拓扑变更集:").grid(row=3, column=0, sticky=tk.W)
        ttk.Button(frame, text="注入 E_TOPO 并演进", command=self.action_evolve).grid(row=3, column=2, sticky=tk.W)
        self.status_var = tk.StringVar(value="就绪：填入样本路径后点击“加载并全量关联”")
        ttk.Label(frame, textvariable=self.status_var, foreground="#0a6").grid(row=4, column=0, columnspan=4, sticky=tk.W)

    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.tabs: dict[str, tk.Text] = {}
        for name in ("拓扑", "告警簇", "根因候选", "折叠衍生告警", "注入轨迹与版本"):
            frame = ttk.Frame(notebook)
            text = tk.Text(frame, wrap=tk.NONE, font=("Consolas", 10))
            scroll_y = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
            scroll_x = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=text.xview)
            text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
            text.grid(row=0, column=0, sticky=tk.NSEW)
            scroll_y.grid(row=0, column=1, sticky=tk.NS)
            scroll_x.grid(row=1, column=0, sticky=tk.EW)
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            notebook.add(frame, text=name)
            self.tabs[name] = text

    def _set_text(self, name: str, content: str) -> None:
        widget = self.tabs[name]
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, content)

    def _current_inputs(self) -> tuple[str, str, str, int]:
        topology_path = self.topology_var.get().strip()
        rules_path = self.rules_var.get().strip()
        obs_path = self.obs_var.get().strip()
        if not (topology_path and rules_path and obs_path):
            raise ValueError("请先填写拓扑、规则与观测文件路径")
        return topology_path, rules_path, obs_path, int(self.window_var.get())

    def action_run(self) -> None:
        try:
            topology_path, rules_path, obs_path, window_ms = self._current_inputs()
            self.report, self.topology, _, _ = run_pipeline(
                topology_path, rules_path, obs_path, window_ms,
                concurrency=1, tag="gui-baseline", store=self.store,
            )
            self.paths = {
                "topology": topology_path, "rules": rules_path, "observations": obs_path,
            }
            self._render_all()
            self.status_var.set(f"关联完成：{len(self.report.clusters)} 个簇，已持久化到 SQLite")
        except Exception as exc:  # GUI 中以对话框呈现业务错误
            messagebox.showerror("处理失败", str(exc))

    def action_inject(self, kind: str) -> None:
        if self.report is None:
            messagebox.showwarning("提示", "请先执行全量关联")
            return
        try:
            topology_path = self.paths["topology"]
            rules_path = self.paths["rules"]
            obs_path = self.paths["observations"]
            window_ms = self.report.window_ms
            if kind == "E_ORDER":
                target = os.path.join(self.workdir, "gui.observations.e_order.ndjson")
                injection, _ = inject_order(obs_path, target)
                rerun, verified = rerun_order_scenario(
                    self.report, topology_path, rules_path, target,
                    window_ms, 1, injection, store=self.store,
                )
            else:
                target = os.path.join(self.workdir, "gui.observations.e_gap.ndjson")
                injection, _ = inject_gap(obs_path, target, "payment-db", self.topology)
                rerun, verified = rerun_gap_scenario(
                    self.report, self.topology, topology_path, rules_path, target,
                    window_ms, 1, injection, "payment-db", store=self.store,
                )
            self.report = rerun
            self._render_all()
            self.status_var.set(f"{kind} 注入完成 verified={verified.verified}")
        except Exception as exc:
            messagebox.showerror("注入失败", str(exc))

    def action_evolve(self) -> None:
        if self.report is None:
            messagebox.showwarning("提示", "请先执行全量关联")
            return
        changeset = self.changeset_var.get().strip()
        if not changeset:
            messagebox.showwarning("提示", "请填写拓扑变更集路径")
            return
        try:
            rerun, new_topology, verified, _diff = run_topology_evolution(
                self.report, self.topology, self.paths["topology"], self.paths["rules"],
                self.paths["observations"], changeset, self.report.window_ms, 1,
                store=self.store,
            )
            self.report = rerun
            self.topology = new_topology
            self._render_all()
            self.status_var.set(f"E_TOPO 演进完成 verified={verified.verified}，视图已整体替换")
        except Exception as exc:
            messagebox.showerror("演进失败", str(exc))

    def _render_all(self) -> None:
        self._render_topology()
        self._render_clusters()
        self._render_root_causes()
        self._render_folded()
        self._render_injections()

    def _render_topology(self) -> None:
        lines = [f"topology_version=v{self.topology.version}"]
        lines.append("services: " + ", ".join(self.topology.services))
        lines.append("edges (source -> target, weight):")
        for source, target, weight in self.topology.edges:
            lines.append(f"  {source} -> {target}  weight={weight}")
        self._set_text("拓扑", "\n".join(lines))

    def _render_clusters(self) -> None:
        lines = [
            f"topology=v{self.report.topology_version} rules=v{self.report.rule_version} "
            f"window_ms={self.report.window_ms} clusters={len(self.report.clusters)}"
        ]
        for cluster in self.report.clusters:
            lines.append(f"\n[{cluster.cluster_id}] members={len(cluster.members)}")
            for event in cluster.members:
                lines.append(
                    f"  ts={event.timestamp_ms} service={event.service} rule={event.rule_id} "
                    f"value={event.value} severity={event.severity}"
                )
            for service, path in cluster.reach_paths:
                chain = ", ".join(f"{link.source}->{link.target}(w={link.weight})" for link in path)
                lines.append(f"  reach[{service}]: {chain}")
        self._set_text("告警簇", "\n".join(lines))

    def _render_root_causes(self) -> None:
        lines = []
        for cluster in self.report.clusters:
            lines.append(f"[{cluster.cluster_id}]")
            for candidate in cluster.root_causes:
                lines.append(
                    f"  rank={candidate.rank} service={candidate.service} "
                    f"in_degree={candidate.in_degree} severity={candidate.severity} "
                    f"earliest_ts={candidate.earliest_ts}"
                )
                lines.append(f"    support_events={len(candidate.supporting_events)}")
                for reason in candidate.reasons:
                    lines.append(f"    reason: {reason}")
            lines.append("")
        self._set_text("根因候选", "\n".join(lines))

    def _render_folded(self) -> None:
        lines = []
        for cluster in self.report.clusters:
            if not cluster.folded:
                continue
            lines.append(f"[{cluster.cluster_id}] folded={len(cluster.folded)}")
            for folded in cluster.folded:
                event = folded.event
                lines.append(
                    f"  ts={event.timestamp_ms} service={event.service} rule={event.rule_id} "
                    f"-> triggered_by={folded.triggered_by_service}"
                )
                lines.append(f"    {folded.reason}")
        if not lines:
            lines.append("(no folded propagated alerts)")
        self._set_text("折叠衍生告警", "\n".join(lines))

    def _render_injections(self) -> None:
        lines = [
            f"session={self.report.session_id} status={self.report.status} "
            f"business_codes={','.join(self.report.business_codes) or '-'}"
        ]
        for injection in self.report.injections:
            lines.append(
                f"\n{injection.injection_id} code={injection.code} verified={injection.verified}"
            )
            lines.append(f"  scope: {injection.scope}")
            lines.append(f"  expected: {list(injection.expected_affected_cluster_ids)}")
            lines.append(f"  actual:   {list(injection.actual_affected_cluster_ids)}")
            lines.append(f"  detail: {injection.detail}")
        for diff in self.report.version_diffs:
            lines.append(
                f"\nversion_diff topo v{diff.old_topology_version}->v{diff.new_topology_version} "
                f"rules v{diff.old_rule_version}->v{diff.new_rule_version}"
            )
            lines.append(f"  invalidated: {list(diff.invalidated_cluster_ids)}")
            lines.append(f"  retained:    {list(diff.retained_cluster_ids)}")
            lines.append(f"  new:         {list(diff.new_cluster_ids)}")
        lines.append("\n" + render_summary(self.report))
        self._set_text("注入轨迹与版本", "\n".join(lines))

    def close(self) -> None:
        self.store.close()


def launch_gui(workdir: str) -> None:
    root = tk.Tk()
    app = WorkbenchApp(root, workdir)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.close(), root.destroy()))
    root.mainloop()
