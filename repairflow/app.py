"""Tkinter desktop application for deterministic incremental repair."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np

from .engine import RepairEngine
from .errors import BusinessError, ErrorCode
from .mask import MaskEditor
from .samples import load_sample, mask_overlay, write_ppm


def run_app(
    data_dir: str | Path = "samples/data",
    cache_dir: str | Path = ".repairflow_cache/gui",
) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:
        raise RuntimeError(
            "Tkinter is required; use the official Windows Python installer and verify "
            "python -c \"import numpy, tkinter\""
        ) from exc

    base, masks, config = load_sample(data_dir)
    root = tk.Tk()
    try:
        app = RepairFlowApp(
            root=root,
            tk=tk,
            ttk=ttk,
            messagebox=messagebox,
            base=base,
            masks=masks,
            config=config,
            cache_dir=Path(cache_dir),
        )
        app.run()
    except Exception:
        try:
            root.destroy()
        except Exception:
            pass
        raise


class RepairFlowApp:
    def __init__(
        self,
        *,
        root: Any,
        tk: Any,
        ttk: Any,
        messagebox: Any,
        base: np.ndarray,
        masks: Any,
        config: dict[str, Any],
        cache_dir: Path,
    ) -> None:
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.initial_base = base.copy()
        self.initial_masks = masks
        self.initial_config = copy.deepcopy(config)
        self.cache_dir = cache_dir
        self.preview_path = cache_dir / "ui_preview.ppm"
        self.preview_path.parent.mkdir(parents=True, exist_ok=True)
        self.preview_serial = 0
        self.photo: Any = None
        self.last_comparison: dict[str, Any] | None = None

        self.engine = RepairEngine(base=base, cache_dir=cache_dir, config=config)
        initial = self.engine.submit(masks)
        self.last_report = initial.report

        self.root.title("RepairFlow Studio - 确定性区域增量修复")
        self.root.geometry("1320x880")
        self.root.minsize(1080, 720)
        try:
            self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
        except Exception:
            pass

        self.region_var = tk.StringVar(value=str(self.engine.mask_state.region_ids[0]))
        self.merge_var = tk.StringVar(
            value=str(
                self.engine.mask_state.region_ids[1]
                if len(self.engine.mask_state.region_ids) > 1
                else self.engine.mask_state.region_ids[0]
            )
        )
        self.radius_var = tk.IntVar(value=3)
        self.iterations_var = tk.IntVar(
            value=int(self.engine.style_config["style"]["iterations"])
        )
        self.context_radius_var = tk.IntVar(
            value=int(self.engine.style_config["context_radius"])
        )
        self.feather_var = tk.IntVar(
            value=int(self.engine.style_config["style"]["blend"]["feather_px"])
        )
        self.precision_var = tk.StringVar(
            value=self.engine.style_config["style"]["precision"]
        )
        self.strength_var = tk.DoubleVar(
            value=float(self.engine.style_config["style"]["style_strength"])
        )
        self.overlay_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="就绪")

        self._build_ui()
        self._refresh_all()

    def run(self) -> None:
        self.root.mainloop()

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk

        toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(toolbar, text="区域", font=("Microsoft YaHei UI", 9, "bold")).pack(
            side=tk.LEFT
        )
        self.region_combo = ttk.Combobox(
            toolbar, textvariable=self.region_var, width=8, state="readonly"
        )
        self.region_combo.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(toolbar, text="合并到").pack(side=tk.LEFT)
        self.merge_combo = ttk.Combobox(
            toolbar, textvariable=self.merge_var, width=8, state="readonly"
        )
        self.merge_combo.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(toolbar, text="半径").pack(side=tk.LEFT)
        ttk.Spinbox(
            toolbar,
            from_=1,
            to=12,
            width=5,
            textvariable=self.radius_var,
        ).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(toolbar, text="风格强度").pack(side=tk.LEFT)
        ttk.Scale(
            toolbar,
            from_=0.0,
            to=1.0,
            variable=self.strength_var,
            orient=tk.HORIZONTAL,
            length=150,
        ).pack(side=tk.LEFT, padx=(4, 3))
        self.strength_label = ttk.Label(toolbar, width=6)
        self.strength_label.pack(side=tk.LEFT, padx=(0, 12))
        self.strength_var.trace_add("write", lambda *_: self._update_strength_label())
        self._update_strength_label()

        ttk.Button(toolbar, text="扩大并提交", command=self._grow).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(toolbar, text="缩小并提交", command=self._shrink).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(toolbar, text="合并并提交", command=self._merge).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(toolbar, text="应用参数", command=self._apply_style).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(toolbar, text="演示：编辑+全量对比", command=self._demo).pack(
            side=tk.LEFT, padx=(12, 3)
        )
        ttk.Button(toolbar, text="全量重算对比", command=self._compare).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(toolbar, text="一致性自检", command=self._self_check).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(toolbar, text="重置样例", command=self._reset).pack(
            side=tk.LEFT, padx=3
        )

        params = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        params.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(params, text="精度档").pack(side=tk.LEFT)
        ttk.Combobox(
            params,
            textvariable=self.precision_var,
            values=("high", "standard"),
            state="readonly",
            width=9,
        ).pack(side=tk.LEFT, padx=(4, 14))
        ttk.Label(params, text="迭代步数").pack(side=tk.LEFT)
        ttk.Spinbox(
            params,
            from_=1,
            to=64,
            width=6,
            textvariable=self.iterations_var,
        ).pack(side=tk.LEFT, padx=(4, 14))
        ttk.Label(params, text="上下文半径").pack(side=tk.LEFT)
        ttk.Spinbox(
            params,
            from_=1,
            to=32,
            width=6,
            textvariable=self.context_radius_var,
        ).pack(side=tk.LEFT, padx=(4, 14))
        ttk.Label(params, text="羽化像素").pack(side=tk.LEFT)
        ttk.Spinbox(
            params,
            from_=0,
            to=24,
            width=6,
            textvariable=self.feather_var,
        ).pack(side=tk.LEFT, padx=(4, 14))
        ttk.Label(
            params,
            text="参数提交后按依赖投影定向失效：风格参数只触发着色及融合，迭代/精度触发补全及下游。",
        ).pack(side=tk.LEFT, padx=(8, 0))

        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=3)
        main.add(right, weight=2)

        canvas_frame = ttk.LabelFrame(left, text="全图输出 / 掩码轮廓", padding=8)
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(
            canvas_frame,
            width=660,
            height=510,
            background="#16191d",
            highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        options = ttk.Frame(canvas_frame)
        options.pack(fill=tk.X, pady=(6, 0))
        ttk.Checkbutton(
            options,
            text="显示区域轮廓与编号",
            variable=self.overlay_var,
            command=self._render,
        ).pack(side=tk.LEFT)
        ttk.Label(
            options,
            text="羽化权重严格限制在掩码内；区域内权重 + 底图权重 = 1",
        ).pack(side=tk.LEFT, padx=(16, 0))

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.graph_text = self._text_tab("计算图")
        self.cache_tree = self._cache_tab()
        self.report_text = self._text_tab("增量报告")
        self.blend_tree = self._blend_tab()
        self.snapshot_text = self._text_tab("快照与业务错误")

        status = ttk.Frame(self.root, padding=(10, 4))
        status.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(status, textvariable=self.status_var, anchor=tk.W).pack(fill=tk.X)

    def _text_tab(self, title: str) -> Any:
        tk = self.tk
        ttk = self.ttk
        frame = ttk.Frame(self.notebook, padding=6)
        text = tk.Text(
            frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            background="#fbfbfb",
            relief=tk.FLAT,
        )
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.notebook.add(frame, text=title)
        return text

    def _cache_tab(self) -> Any:
        tk = self.tk
        ttk = self.ttk
        frame = ttk.Frame(self.notebook, padding=6)
        columns = ("region", "node", "status", "key", "product", "exists")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=18)
        headings = {
            "region": "区域",
            "node": "节点",
            "status": "状态",
            "key": "缓存键",
            "product": "产物哈希",
            "exists": "落盘",
        }
        widths = {
            "region": 52,
            "node": 132,
            "status": 74,
            "key": 160,
            "product": 160,
            "exists": 52,
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor=tk.W)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.notebook.add(frame, text="区域缓存")
        return tree

    def _blend_tab(self) -> Any:
        tk = self.tk
        ttk = self.ttk
        frame = ttk.Frame(self.notebook, padding=6)
        columns = ("region", "band", "diff", "max", "hist")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        headings = {
            "region": "区域",
            "band": "融合带像素",
            "diff": "差异像素",
            "max": "最大通道差",
            "hist": "偏差谱 0/1/2-3/4-7/8-15/16-31/32-63/64+",
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(
                column,
                width=95 if column != "hist" else 330,
                anchor=tk.W,
            )
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.notebook.add(frame, text="融合带偏差谱")
        return tree

    def _update_strength_label(self) -> None:
        try:
            self.strength_label.configure(text=f"{self.strength_var.get():.3f}")
        except Exception:
            pass

    def _selected_region(self) -> int:
        try:
            return int(self.region_var.get())
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG, "select a region", field="region_id"
            ) from exc

    def _grow(self) -> None:
        if self.engine.mask_state is None:
            return
        region_id = self._selected_region()
        radius = max(1, int(self.radius_var.get()))
        mask = MaskEditor.grow(self.engine.mask_state, region_id, radius)
        self._submit(
            mask,
            self._style_from_control(),
            f"区域 {region_id} 扩大 {radius}px",
            edit={
                "type": "ui_edit",
                "action": "grow",
                "region_id": region_id,
                "radius": radius,
            },
        )

    def _shrink(self) -> None:
        if self.engine.mask_state is None:
            return
        region_id = self._selected_region()
        radius = max(1, int(self.radius_var.get()))
        mask = MaskEditor.shrink(self.engine.mask_state, region_id, radius)
        self._submit(
            mask,
            self._style_from_control(),
            f"区域 {region_id} 缩小 {radius}px",
            edit={
                "type": "ui_edit",
                "action": "shrink",
                "region_id": region_id,
                "radius": radius,
            },
        )

    def _merge(self) -> None:
        if self.engine.mask_state is None:
            return
        region_id = self._selected_region()
        merge_id = int(self.merge_var.get())
        if region_id == merge_id:
            self.status_var.set("合并区域不能相同")
            return
        mask = MaskEditor.merge(
            self.engine.mask_state, (region_id, merge_id), target_id=region_id
        )
        self._submit(
            mask,
            self._style_from_control(),
            f"合并区域 {merge_id} 到 {region_id}",
            edit={
                "type": "ui_edit",
                "action": "merge",
                "region_ids": [region_id, merge_id],
                "target_id": region_id,
            },
        )

    def _apply_style(self) -> None:
        if self.engine.mask_state is None:
            return
        self._submit(
            self.engine.mask_state,
            self._style_from_control(),
            "应用风格强度参数",
        )

    def _demo(self) -> None:
        if self.engine.mask_state is None:
            return
        region_id = self._selected_region()
        radius = max(1, int(self.radius_var.get()))
        mask = MaskEditor.grow(self.engine.mask_state, region_id, radius)
        style = self._style_from_control()
        style["style"]["style_strength"] = min(
            1.0, float(style["style"]["style_strength"]) + 0.05
        )
        self.strength_var.set(style["style"]["style_strength"])
        result = self._submit(
            mask,
            style,
            f"演示：区域 {region_id} 扩大并调整风格",
            edit={
                "type": "ui_demo_edit",
                "action": "grow_plus_style",
                "region_id": region_id,
                "radius": radius,
            },
        )
        if result:
            self._compare()

    def _submit(
        self,
        mask: Any,
        style: dict[str, Any],
        description: str,
        *,
        edit: dict[str, Any] | None = None,
    ) -> Any:
        try:
            result = self.engine.submit(mask, style=style, edit_record=edit)
        except BusinessError as exc:
            self._show_error(exc)
            return None
        self.last_report = result.report
        self.last_comparison = None
        self.status_var.set(f"{description} 完成：{self._status_line(result.report)}")
        self._refresh_all()
        return result

    def _compare(self) -> None:
        try:
            comparison = self.engine.compare_with_full(raise_on_mismatch=False)
        except BusinessError as exc:
            self._show_error(exc)
            return
        self.last_comparison = comparison
        spectrum = comparison["blend_deviation_spectrum"]
        self._fill_blend(spectrum)
        if comparison["byte_identical"] and comparison["artifact_hashes_equal"]:
            self.status_var.set(
                "全量对比通过：整图逐字节一致，五节点产物哈希一致，融合偏差为 0"
            )
        else:
            self.status_var.set("全量对比失败，请检查增量报告与首个差异坐标")
        self._fill_report()
        self._fill_snapshots()

    def _self_check(self) -> None:
        try:
            result = self.engine.self_check()
            verify = self.engine.verify_snapshots()
        except BusinessError as exc:
            self._show_error(exc)
            return
        self.status_var.set(
            f"自检通过：重算/复用集合不相交且覆盖 {len(result['affected_regions'])} 个区域；"
            f"快照校验 {verify['ok']}"
        )
        self._fill_snapshots()

    def _reset(self) -> None:
        try:
            self.engine = RepairEngine(
                base=self.initial_base,
                cache_dir=self.cache_dir / "reset",
                config=self.initial_config,
            )
            result = self.engine.submit(self.initial_masks)
        except BusinessError as exc:
            self._show_error(exc)
            return
        self.last_report = result.report
        self.last_comparison = None
        self.strength_var.set(self.engine.style_config["style"]["style_strength"])
        self.status_var.set("已重置为固定种子样例")
        self._refresh_all()

    def _style_from_control(self) -> dict[str, Any]:
        style = copy.deepcopy(self.engine.style_config or self.initial_config)
        try:
            style["context_radius"] = int(self.context_radius_var.get())
            style["style"]["style_strength"] = float(self.strength_var.get())
            style["style"]["iterations"] = int(self.iterations_var.get())
            style["style"]["precision"] = str(self.precision_var.get())
            style["style"]["blend"]["feather_px"] = int(self.feather_var.get())
        except Exception as exc:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "invalid numeric parameter control value",
                field="style",
                error_message=str(exc),
            ) from exc
        return style

    def _status_line(self, report: dict[str, Any]) -> str:
        return (
            f"重算区域 {report['recomputed_regions']}，复用区域 {report['reused_regions']}，"
            f"缓存命中 {report['cache_hit_count']}，重算节点 {report['recomputed_node_count']}"
        )

    def _refresh_all(self) -> None:
        ids = list(self.engine.mask_state.region_ids)
        values = [str(value) for value in ids]
        self.region_combo.configure(values=values)
        self.merge_combo.configure(values=values)
        if self.region_var.get() not in values:
            self.region_var.set(values[0])
        if self.merge_var.get() not in values:
            self.merge_var.set(values[1] if len(values) > 1 else values[0])
        if self.engine.style_config is not None:
            self.strength_var.set(self.engine.style_config["style"]["style_strength"])
            self.iterations_var.set(self.engine.style_config["style"]["iterations"])
            self.context_radius_var.set(self.engine.style_config["context_radius"])
            self.feather_var.set(
                self.engine.style_config["style"]["blend"]["feather_px"]
            )
            self.precision_var.set(self.engine.style_config["style"]["precision"])
        self._fill_graph()
        self._fill_cache()
        self._fill_report()
        self._fill_snapshots()
        self._render()
        if self.last_report is not None and not self.status_var.get().startswith("全量对比"):
            self.status_var.set(self._status_line(self.last_report))

    def _fill_graph(self) -> None:
        payload = {
            "节点": self.engine.graph.to_dict()["nodes"],
            "连接": self.engine.graph.to_dict()["edges"],
            "拓扑顺序": self.engine.graph.to_dict()["topological_order"],
            "上下文半径": self.engine.style_config["context_radius"],
            "迭代步数": self.engine.style_config["style"]["iterations"],
            "精度档": self.engine.style_config["style"]["precision"],
        }
        self._set_text(self.graph_text, json.dumps(payload, indent=2, ensure_ascii=False))

    def _fill_cache(self) -> None:
        for item in self.cache_tree.get_children():
            self.cache_tree.delete(item)
        if self.engine.mask_state is None:
            return
        for region_id in self.engine.mask_state.region_ids:
            inspection = self.engine.inspect_region(region_id)
            for node, item in inspection["nodes"].items():
                self.cache_tree.insert(
                    "",
                    "end",
                    values=(
                        region_id,
                        node,
                        item["last_status"],
                        item["key"][:24] + "...",
                        item["artifact_hash"][:24] + "...",
                        "是" if item["artifact_exists"] else "否",
                    ),
                )

    def _fill_report(self) -> None:
        if self.last_report is None:
            return
        summary = {
            "触发编辑": self.last_report["edits"],
            "失效区域": self.last_report["invalidated_regions"],
            "影响区域": self.last_report["affected_regions"],
            "重算区域": self.last_report["recomputed_regions"],
            "复用区域": self.last_report["reused_regions"],
            "重算节点": self.last_report["recomputed_nodes"],
            "复用节点": self.last_report["reused_nodes"],
            "缓存命中数": self.last_report["cache_hit_count"],
            "重算节点数": self.last_report["recomputed_node_count"],
            "降级": self.last_report["degradation"],
            "融合检查": self.last_report["blend_check"],
            "自检": self.last_report["self_check"],
            "输出哈希": self.last_report["output_hash"],
        }
        if self.last_comparison is not None:
            summary["全量对比"] = {
                "byte_identical": self.last_comparison["byte_identical"],
                "artifact_hashes_equal": self.last_comparison["artifact_hashes_equal"],
                "first_difference": self.last_comparison["first_difference"],
            }
        self._set_text(self.report_text, json.dumps(summary, indent=2, ensure_ascii=False))

    def _fill_snapshots(self) -> None:
        payload = {
            "快照版本": self.engine.snapshot_status(),
            "最近业务错误码": self.engine.last_error,
            "缓存目录": str(self.cache_dir),
        }
        self._set_text(
            self.snapshot_text,
            json.dumps(payload, indent=2, ensure_ascii=False),
        )

    def _fill_blend(self, spectrum: list[dict[str, Any]]) -> None:
        for item in self.blend_tree.get_children():
            self.blend_tree.delete(item)
        for entry in spectrum:
            bins = "/".join(str(value) for value in entry["delta_histogram_0_1_2_3_4_7_8_15_16_31_32_63_64plus"])
            self.blend_tree.insert(
                "",
                "end",
                values=(
                    entry["region_id"],
                    entry["blend_band_pixels"],
                    entry["differing_pixels"],
                    entry["max_abs_channel_delta"],
                    bins,
                ),
            )

    def _render(self) -> None:
        image = self.engine.image
        if image is None or self.engine.mask_state is None:
            return
        if self.overlay_var.get():
            image = mask_overlay(image, self.engine.mask_state)
        zoom = 2
        display = np.repeat(np.repeat(image, zoom, axis=0), zoom, axis=1)
        self.preview_serial += 1
        path = self.preview_path.with_name(
            f"{self.preview_path.stem}_{self.preview_serial}{self.preview_path.suffix}"
        )
        write_ppm(path, display)
        self.photo = self.tk.PhotoImage(file=str(path))

        self.canvas.delete("all")
        canvas_width = max(660, self.canvas.winfo_width())
        canvas_height = max(510, self.canvas.winfo_height())
        cx = canvas_width // 2
        cy = canvas_height // 2
        self.canvas.create_image(cx, cy, image=self.photo, anchor=self.tk.CENTER)
        origin_x = cx - display.shape[1] // 2
        origin_y = cy - display.shape[0] // 2
        if self.overlay_var.get():
            palette = {
                1: "#ef5c4c",
                2: "#38ace1",
                3: "#f5bd42",
                4: "#5ccd7a",
            }
            for region in self.engine.mask_state.regions:
                y0, x0, y1, x1 = region.bbox()
                color = palette.get(region.region_id, "#dddddd")
                self.canvas.create_rectangle(
                    origin_x + x0 * zoom,
                    origin_y + y0 * zoom,
                    origin_x + x1 * zoom,
                    origin_y + y1 * zoom,
                    outline=color,
                    width=2,
                )
                self.canvas.create_text(
                    origin_x + ((x0 + x1) // 2) * zoom,
                    origin_y + ((y0 + y1) // 2) * zoom,
                    text=f"R{region.region_id}",
                    fill="#ffffff",
                    font=("Consolas", 11, "bold"),
                )
        if self.last_report is not None:
            self._fill_blend(self.last_report["blend_check"]["spectrum"])

    def _set_text(self, widget: Any, text: str) -> None:
        widget.configure(state=self.tk.NORMAL)
        widget.delete("1.0", self.tk.END)
        widget.insert("1.0", text)
        widget.configure(state=self.tk.DISABLED)

    def _show_error(self, exc: BusinessError) -> None:
        payload = json.dumps(exc.to_dict(), indent=2, ensure_ascii=False)
        self.status_var.set(f"{exc.code}: {exc.message}")
        self._set_text(self.snapshot_text, payload)
        try:
            self.messagebox.showerror(exc.code, exc.message, parent=self.root)
        except Exception:
            pass


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="RepairFlow Tkinter desktop application")
    parser.add_argument("--data", default="samples/data")
    parser.add_argument("--cache", default=".repairflow_cache/gui")
    args = parser.parse_args()
    try:
        run_app(args.data, args.cache)
    except Exception as exc:
        print(f"RepairFlow GUI failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
