"""Tkinter 诊断工作台。

所有面板的数据来源都是 :class:`gpu_diag.session.Session`，渲染顺序固定，
不依赖字典或哈希遍历。无图形环境（或缺少 tkinter）时抛出带说明的 RuntimeError。
"""

import os

from .errors import BusinessError
from .logging_utils import get_logger
from .session import Session
from .manifest import load_manifest

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except Exception as exc:  # pragma: no cover - 取决于主机图形环境
    tk = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

_SLOT_SIZE = 46
_OCCUPIED_COLOR = "#cfe8ff"
_DRAINED_COLOR = "#d9d9d9"
_FREE_COLOR = "#ffffff"
_REJECT_COLOR = "#ffd6d6"


def require_tk():
    if tk is None:
        raise RuntimeError(
            "tkinter is unavailable on this host: {}. "
            "On Windows install the official python.org Python 3.10+ which bundles Tk.".format(
                _IMPORT_ERROR))


class Workbench(tk.Tk if tk is not None else object):
    def __init__(self, manifest_path, snapshot_dir=None):
        require_tk()
        super().__init__()
        self.logger = get_logger()
        self.manifest_path = manifest_path
        self.session = Session(load_manifest(manifest_path))
        if snapshot_dir:
            self.session.snapshot_dir = snapshot_dir
        self.title("GPU 调度与配额诊断工作台")
        self.geometry("1180x820")
        self._build_widgets()
        self.refresh_all()

    def _build_widgets(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        self.status_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="刷新重调度", command=self._refresh).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="保存快照", command=self._save_snapshot).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="恢复快照", command=self._restore_snapshot).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="重放校验", command=self._replay_snapshot).pack(side=tk.RIGHT, padx=4)

        scale_frame = ttk.LabelFrame(self, text="数据规模参数（修改后参数版本 +1 并整体重调度）", padding=8)
        scale_frame.pack(fill=tk.X, padx=8)
        self.scale_vars = {}
        for column, name in enumerate(("batch_size", "seq_len", "model_dim")):
            ttk.Label(scale_frame, text=name).grid(row=0, column=column * 2, padx=4, sticky=tk.E)
            var = tk.StringVar()
            ttk.Entry(scale_frame, textvariable=var, width=10).grid(row=0, column=column * 2 + 1, padx=4)
            self.scale_vars[name] = var
        ttk.Button(scale_frame, text="应用参数并重调度", command=self._apply_scale).grid(
            row=0, column=6, padx=10)
        ttk.Label(scale_frame, text="池卡位下线(格式 pool:index)").grid(row=0, column=7, padx=4)
        self.drain_var = tk.StringVar()
        ttk.Entry(scale_frame, textvariable=self.drain_var, width=14).grid(row=0, column=8, padx=4)
        ttk.Button(scale_frame, text="下线/恢复", command=self._toggle_drain).grid(row=0, column=9, padx=4)

        body = ttk.Panedwindow(self, orient=tk.VERTICAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        upper = ttk.Frame(body)
        body.add(upper, weight=1)
        self.quota_tree = self._make_tree(
            upper, "配额组占用（按 group_id 升序）",
            ("group_id", "used_cards", "max_cards", "used_memory_mib", "max_memory_mib"))
        self.quota_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.pool_canvas = tk.Canvas(upper, bg="#f4f4f4", highlightthickness=0)
        pool_wrap = ttk.LabelFrame(upper, text="资源池卡位占用图（卡位索引升序）")
        pool_wrap.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        self.pool_canvas = tk.Canvas(pool_wrap, bg="#f4f4f4", highlightthickness=0)
        self.pool_canvas.pack(fill=tk.BOTH, expand=True)

        lower = ttk.Frame(body)
        body.add(lower, weight=2)
        self.job_tree = self._make_tree(
            lower, "作业估算需求与准入结论（按优先级降序、清单顺序升序）",
            ("job_id", "pool", "group", "priority", "per_card_mib", "total_mib",
             "slots", "topology", "code", "reason_chain"))
        self.job_tree.pack(fill=tk.BOTH, expand=True)
        self.job_tree.bind("<Double-1>", self._show_job_detail)

        bottom = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom.pack(fill=tk.X)
        ttk.Label(bottom, text="快照列表：").pack(side=tk.LEFT)
        self.snapshot_var = tk.StringVar()
        self.snapshot_combo = ttk.Combobox(bottom, textvariable=self.snapshot_var, width=40, state="readonly")
        self.snapshot_combo.pack(side=tk.LEFT, padx=4)
        self._reload_snapshot_list()

    def _make_tree(self, parent, title, columns):
        wrap = ttk.LabelFrame(parent, text=title)
        tree = ttk.Treeview(wrap, columns=columns, show="headings", height=8)
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=max(90, len(column) * 10), anchor=tk.W)
        scroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    def _refresh(self):
        self.session.reschedule("manual_refresh")
        self.refresh_all()

    def refresh_all(self):
        run = self.session.current_run
        self.status_var.set(
            "参数版本 v{} | 会话修订 r{} | 准入 {}/{}".format(
                self.session.parameter_version, self.session.session_revision,
                sum(1 for item in run.results if item.admitted), len(run.results)))
        for name, var in self.scale_vars.items():
            if not var.get():
                var.set(str(run.scale_params.to_dict()[name]))
        self._refresh_quota()
        self._refresh_pools()
        self._refresh_jobs()
        self._reload_snapshot_list()

    def _refresh_quota(self):
        for item in self.quota_tree.get_children():
            self.quota_tree.delete(item)
        for group_id in sorted(self.session.current_run.quota_usage):
            usage = self.session.current_run.quota_usage[group_id]
            self.quota_tree.insert("", tk.END, values=(
                group_id, usage["used_cards"], usage["max_cards"],
                usage["used_memory_mib"], usage["max_memory_mib"]))

    def _refresh_pools(self):
        self.pool_canvas.delete("all")
        y = 10
        for pool in sorted(self.session.pools.values(), key=lambda item: item.name):
            self.pool_canvas.create_text(10, y + _SLOT_SIZE // 2, anchor=tk.W,
                                         text="{} ({} MiB/卡)".format(pool.name, pool.card_memory_mib))
            occupied = {}
            for result in self.session.current_run.results:
                if result.admitted and result.pool_name == pool.name:
                    for slot in result.slots:
                        occupied[slot] = result.job_id
            for index in range(pool.card_count):
                x0 = 200 + index * (_SLOT_SIZE + 6)
                if index in pool.drained_cards:
                    color = _DRAINED_COLOR
                elif index in occupied:
                    color = _OCCUPIED_COLOR
                else:
                    color = _FREE_COLOR
                self.pool_canvas.create_rectangle(
                    x0, y, x0 + _SLOT_SIZE, y + _SLOT_SIZE, fill=color, outline="#555")
                self.pool_canvas.create_text(x0 + _SLOT_SIZE // 2, y + 12,
                                             text=str(index), font=("Segoe UI", 8))
                label = pool.card_labels[index]
                self.pool_canvas.create_text(x0 + _SLOT_SIZE // 2, y + 32,
                                             text=label, font=("Segoe UI", 7))
                if index in occupied:
                    self.pool_canvas.create_text(x0 + _SLOT_SIZE // 2, y + _SLOT_SIZE - 6,
                                                 text=occupied[index].replace("j-", ""),
                                                 font=("Segoe UI", 6))
            y += _SLOT_SIZE + 24

    def _refresh_jobs(self):
        for item in self.job_tree.get_children():
            self.job_tree.delete(item)
        for result in self.session.current_run.results:
            chain = "; ".join("{}:{}".format(reason.code_name, reason.dimension)
                              for reason in result.reasons)
            self.job_tree.insert("", tk.END, iid=result.job_id, values=(
                result.job_id, result.pool_name,
                self._job_group(result.job_id), self._job_priority(result.job_id),
                result.estimate.per_card_mib, result.estimate.total_mib,
                ",".join(map(str, result.slots)), result.topology_group or "-",
                result.code_name, chain or "-"))
            if not result.admitted:
                self.job_tree.tag_configure(result.job_id, background=_REJECT_COLOR)
                self.job_tree.item(result.job_id, tags=(result.job_id,))

    def _job_group(self, job_id):
        return next(job.quota_group for job in self.session.jobs if job.job_id == job_id)

    def _job_priority(self, job_id):
        return next(job.priority for job in self.session.jobs if job.job_id == job_id)

    def _show_job_detail(self, _event=None):
        selection = self.job_tree.selection()
        if not selection:
            return
        result = next(item for item in self.session.current_run.results if item.job_id == selection[0])
        lines = ["作业 {} 估算中间项 (参数版本 v{})".format(result.job_id, result.estimate.parameter_version)]
        for key in sorted(result.estimate.components):
            if key == "formula":
                continue
            lines.append("{} = {}".format(key, result.estimate.components[key]))
        lines.append("")
        lines.append("拒绝原因链:")
        if result.reasons:
            for reason in result.reasons:
                lines.append("{} [{}] current={} requested={} limit={} @ {}".format(
                    reason.code_name, reason.dimension, reason.current_usage,
                    reason.requested, reason.limit, reason.field_path))
        else:
            lines.append("(none)")
        messagebox.showinfo("作业诊断详情", "\n".join(lines))

    def _apply_scale(self):
        try:
            values = {name: int(var.get()) for name, var in self.scale_vars.items()}
            self.session.update_scale(**values)
        except (ValueError, BusinessError) as exc:
            messagebox.showerror("E_ESTIMATE", str(exc))
            return
        self.logger.info("scale params updated: %s", values)
        self.refresh_all()

    def _toggle_drain(self):
        try:
            pool_name, raw_index = self.drain_var.get().split(":")
            card_index = int(raw_index)
            pool = self.session.pools[pool_name]
            self.session.set_drained(pool_name, card_index, card_index not in pool.drained_cards)
        except (ValueError, KeyError, BusinessError) as exc:
            messagebox.showerror("E_SNAPSHOT", str(exc))
            return
        self.refresh_all()

    def _save_snapshot(self):
        name = simpledialog_ask("快照名称")
        if not name:
            return
        try:
            path = self.session.save_snapshot(name)
        except BusinessError as exc:
            messagebox.showerror(exc.code_name, exc.message)
            return
        self.logger.info("snapshot saved: %s", path)
        self._reload_snapshot_list()
        messagebox.showinfo("OK", "快照已保存: {}".format(path))

    def _restore_snapshot(self):
        path = self._selected_snapshot_path()
        if not path:
            return
        try:
            session, info = Session.load_snapshot(path)
        except BusinessError as exc:
            messagebox.showerror(exc.code_name, exc.message)
            return
        self.session = session
        self.logger.info("snapshot restored (full replace): %s", info)
        self.refresh_all()
        messagebox.showinfo("OK", "整体替换恢复完成: {}".format(info))

    def _replay_snapshot(self):
        path = self._selected_snapshot_path()
        if not path:
            return
        try:
            report = Session.replay_snapshot(path)
        except BusinessError as exc:
            messagebox.showerror(exc.code_name, exc.details or exc.message)
            return
        messagebox.showinfo("OK", report["message"])

    def _selected_snapshot_path(self):
        name = self.snapshot_var.get()
        if not name:
            messagebox.showwarning("提示", "请先选择一个快照")
            return None
        return os.path.join(self.session.snapshot_dir, name)

    def _reload_snapshot_list(self):
        directory = getattr(self, "session", None) and self.session.snapshot_dir
        if not directory or not os.path.isdir(directory):
            self.snapshot_combo["values"] = []
            return
        names = sorted(item for item in os.listdir(directory) if item.endswith(".json"))
        self.snapshot_combo["values"] = names


def simpledialog_ask(prompt):
    from tkinter import simpledialog
    return simpledialog.askstring("输入", prompt)


def launch(manifest_path, snapshot_dir=None):
    require_tk()
    app = Workbench(manifest_path, snapshot_dir)
    app.mainloop()
