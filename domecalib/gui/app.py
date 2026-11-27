"""Tkinter operator console for the dome calibration kernel.

The GUI is a thin view over :mod:`domecalib.core`: every button calls the
same :class:`CalibrationEngine` methods used by the command line, so results
are identical between the two entry points.
"""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk

import numpy as np

from ..core.engine import CalibrationEngine
from ..core.errors import CalibrationError
from ..core.persistence import (
    load_snapshot,
    save_snapshot,
    list_snapshots,
)
from ..core.samplegen import generate_sample_project
from ..core.visualization import (
    encode_ppm,
    lut_visualization,
    mask_visualization,
)


class DomeCalibApp:
    def __init__(self, root: tk.Tk, snapshot_dir: str = "snapshots"):
        self.root = root
        self.snapshot_dir = snapshot_dir
        self.engine: CalibrationEngine | None = None
        self._photo_refs: list[tk.PhotoImage] = []
        root.title("穹幕投影校正融合控制台")
        root.geometry("1180x760")
        self._build_layout()

    # ------------------------------------------------------------------ UI
    def _build_layout(self) -> None:
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="载入样本工程", command=self.load_sample).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="载入快照", command=self.load_snapshot_dialog).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="保存快照", command=self.save_snapshot_dialog).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="全量重算", command=self.full_rebuild).pack(side=tk.LEFT, padx=4)

        selector = ttk.Frame(self.root, padding=(8, 0))
        selector.pack(fill=tk.X)
        ttk.Label(selector, text="投影机:").pack(side=tk.LEFT)
        self.projector_var = tk.StringVar()
        self.projector_combo = ttk.Combobox(
            selector, textvariable=self.projector_var, state="readonly", width=10
        )
        self.projector_combo.pack(side=tk.LEFT, padx=6)
        self.projector_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_views())

        blend_box = ttk.LabelFrame(self.root, text="融合带羽化参数", padding=8)
        blend_box.pack(fill=tk.X, padx=8, pady=6)
        self.feather_var = tk.DoubleVar(value=6.0)
        self.gamma_var = tk.DoubleVar(value=1.0)
        self.curve_var = tk.StringVar(value="smoothstep")
        ttk.Label(blend_box, text="羽化宽度(度)").grid(row=0, column=0, padx=4)
        ttk.Entry(blend_box, textvariable=self.feather_var, width=8).grid(row=0, column=1)
        ttk.Label(blend_box, text="gamma").grid(row=0, column=2, padx=4)
        ttk.Entry(blend_box, textvariable=self.gamma_var, width=8).grid(row=0, column=3)
        ttk.Label(blend_box, text="曲线").grid(row=0, column=4, padx=4)
        ttk.Combobox(blend_box, textvariable=self.curve_var, width=10,
                     values=["smoothstep", "linear"], state="readonly").grid(row=0, column=5)
        ttk.Button(blend_box, text="应用并重算受影响投影机",
                   command=self.adjust_blending).grid(row=0, column=6, padx=10)

        body = ttk.Frame(self.root)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        left = ttk.LabelFrame(body, text="查找表 LUT（红=经度 绿=纬度 蓝=有效）")
        right = ttk.LabelFrame(body, text="融合遮罩")
        left.grid(row=0, column=0, sticky="nsew", padx=4)
        right.grid(row=0, column=1, sticky="nsew", padx=4)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self.lut_canvas = tk.Label(left, background="#202020")
        self.lut_canvas.pack(fill=tk.BOTH, expand=True)
        self.mask_canvas = tk.Label(right, background="#202020")
        self.mask_canvas.pack(fill=tk.BOTH, expand=True)

        report_box = ttk.LabelFrame(self.root, text="对齐误差与亮度报告", padding=6)
        report_box.pack(fill=tk.BOTH, padx=8, pady=6)
        self.report_text = tk.Text(report_box, height=12, wrap=tk.NONE)
        self.report_text.pack(fill=tk.BOTH, expand=True)
        self.status_var = tk.StringVar(value="就绪。请载入样本工程或快照。")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor="w").pack(fill=tk.X, side=tk.BOTTOM)

    # ------------------------------------------------------------- actions
    def _guard(self, action):
        try:
            action()
        except CalibrationError as exc:
            self.status_var.set(f"错误 {exc.code}: {exc.message}")
            # Non-blocking diagnostic: surface the full business error in the
            # report panel so the operator workflow is never interrupted by a
            # modal dialog and existing calibration data stays untouched.
            self.report_text.delete("1.0", tk.END)
            self.report_text.insert(
                tk.END,
                f"业务错误码: {exc.code}\n说明: {exc.message}\n"
                f"上下文: {json.dumps(exc.details, ensure_ascii=False, indent=2)}",
            )

    def load_sample(self) -> None:
        self._guard(self._load_sample_impl)

    def _load_sample_impl(self) -> None:
        project = generate_sample_project()
        self.engine = CalibrationEngine(project)
        self.engine.full_rebuild()
        self._after_project_loaded("固定种子样本工程已载入并完成全量校正。")

    def load_snapshot_dialog(self) -> None:
        self._guard(self._load_snapshot_impl)

    def _load_snapshot_impl(self) -> None:
        snapshots = list_snapshots(self.snapshot_dir)
        if not snapshots:
            raise CalibrationError("E1008_SNAPSHOT_IO",
                                   "快照目录中没有可用快照",
                                   {"dir": self.snapshot_dir})
        label = snapshots[-1]["label"]
        project = load_snapshot(self.snapshot_dir, label)
        self.engine = CalibrationEngine(project)
        self._after_project_loaded(f"已载入快照 {label}。")

    def save_snapshot_dialog(self) -> None:
        self._guard(self._save_snapshot_impl)

    def _save_snapshot_impl(self) -> None:
        if self.engine is None:
            raise CalibrationError("E1006_INVALID_PARAMETER", "尚未载入工程")
        label = f"manual_{len(list_snapshots(self.snapshot_dir))}"
        save_snapshot(self.engine.project, self.snapshot_dir, label)
        self.status_var.set(f"快照 {label} 已保存。")

    def full_rebuild(self) -> None:
        self._guard(self._full_rebuild_impl)

    def _full_rebuild_impl(self) -> None:
        if self.engine is None:
            raise CalibrationError("E1006_INVALID_PARAMETER", "尚未载入工程")
        self.engine.full_rebuild()
        self._after_project_loaded("全量重算完成。")

    def adjust_blending(self) -> None:
        self._guard(self._adjust_blending_impl)

    def _adjust_blending_impl(self) -> None:
        if self.engine is None:
            raise CalibrationError("E1006_INVALID_PARAMETER", "尚未载入工程")
        report = self.engine.update_blending_params(
            feather_width_deg=float(self.feather_var.get()),
            curve=str(self.curve_var.get()),
            gamma=float(self.gamma_var.get()),
        )
        self._render_report(report)
        self.refresh_views()
        self.status_var.set("羽化参数已更新，融合遮罩已重算（LUT 未变）。")

    # ------------------------------------------------------------- helpers
    def _after_project_loaded(self, message: str) -> None:
        ids = [s.projector_id for s in self.engine.project.projectors]
        self.projector_combo["values"] = ids
        self.projector_var.set(ids[0])
        self.feather_var.set(self.engine.project.blending.feather_width_deg)
        self.gamma_var.set(self.engine.project.blending.gamma)
        self.curve_var.set(self.engine.project.blending.curve)
        report = self.engine.run_reports()
        self._render_report(report)
        self.refresh_views()
        self.status_var.set(message)

    def _make_photo(self, rgb: np.ndarray, max_side: int = 460) -> tk.PhotoImage:
        ppm = encode_ppm(rgb)
        image = tk.PhotoImage(data=ppm, format="PPM")
        scale = max(image.width() // max_side, image.height() // max_side, 1)
        if scale > 1:
            image = image.subsample(scale, scale)
        return image

    def refresh_views(self) -> None:
        if self.engine is None or not self.projector_var.get():
            return
        pid = self.projector_var.get()
        lut = self.engine.project.luts.get(pid)
        mask = self.engine.project.masks.get(pid)
        self._photo_refs.clear()
        if lut is not None:
            photo = self._make_photo(lut_visualization(lut))
            self.lut_canvas.configure(image=photo)
            self._photo_refs.append(photo)
        if mask is not None:
            photo = self._make_photo(mask_visualization(mask))
            self.mask_canvas.configure(image=photo)
            self._photo_refs.append(photo)

    def _render_report(self, report: dict) -> None:
        self.report_text.delete("1.0", tk.END)
        bright = report["brightness"]
        align = report["alignment"]
        lines = []
        lines.append("== 反向映射真值比对（容差内有效像素） ==")
        for pid, info in align["truth_comparison"].items():
            lines.append(
                f"  {pid}: 最大角误差 {info['max_angular_error_rad']:.3e} rad, "
                f"RMS {info['rms_angular_error_rad']:.3e}, 掩码一致 {info['invalid_agreement']}"
            )
        lines.append("")
        lines.append("== TPS 修正质量 ==")
        for pid, q in report["tps_quality"].items():
            lines.append(
                f"  {pid}: 状态 {q['status']}, 拟合RMS {q['rms_residual_rad']:.3e}, "
                f"LOOCV RMS {q['loocv_rms_rad']:.3e}"
            )
            for warning in q["warnings"]:
                lines.append(f"      ! {warning}")
        lines.append("")
        lines.append("== 亮度一致性 ==")
        post = bright["weight_sum_post_optimization"]
        lines.append(
            f"  覆盖率 {bright['coverage_ratio']:.3f}, 重叠单元 {bright['overlap_cell_count']}, "
            f"三机交叠 {bright['triple_overlap_cell_count']}"
        )
        lines.append(
            f"  优化后权重和 min/max {post['min']:.6f}/{post['max']:.6f}, "
            f"变异系数 {bright['uniformity_cv_post']:.3e}, 最大接缝跳变 {bright['max_seam_jump']:.3e}"
        )
        lines.append(f"  投影机增益 {json.dumps(bright['projector_gains'])}")
        lines.append("")
        lines.append("== 融合诊断 ==")
        diag = report["blend_diagnostics"]
        lines.append(
            f"  单调性违规 {diag.get('monotonicity_violations')}, "
            f"最窄重叠 {diag.get('min_overlap_width_deg')}, "
            f"窄重叠带 {len(diag.get('narrow_overlap', []))} 处"
        )
        lines.append("")
        lines.append(f"== 版本号 == {json.dumps(report['versions'])}")
        self.report_text.insert(tk.END, "\n".join(lines))


def main() -> int:
    root = tk.Tk()
    DomeCalibApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
