# -*- coding: utf-8 -*-
"""地形高度标量场等值线查看器（PySide6，仅本地运行）。

用法:
    python contour_viewer.py [网格JSON路径] [--quit-after 毫秒]

    省略路径时默认读取 samples/sample_normal.json（先运行 gen_samples.py 生成）。
    --quit-after 为自检辅助选项：窗口显示指定毫秒数后自动关闭。

网格 JSON 格式:
{
  "rows": 61,               行数（y 方向网格点数，2..200）
  "cols": 81,               列数（x 方向网格点数，2..200）
  "x_range": [0.0, 8.0],    x 最小/最大值
  "y_range": [-3.0, 5.0],   y 最小/最大值
  "matrix": [[...], ...],   rows 行 x cols 列；null 或 NaN 表示无数据单元格
  "levels": [-1.5, ...]     等值线级别，1..20 个
}
matrix[j][i] 对应网格点 (x_i, y_j)，j = 0 为 y 最小值所在的行。
"""
from __future__ import annotations

import json
import math
import os
import sys

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QToolBar,
                               QVBoxLayout, QWidget)

MAX_DIM = 200
MAX_LEVELS = 20
REQUIRED_FIELDS = ("rows", "cols", "x_range", "y_range", "matrix", "levels")


class GridDataError(Exception):
    """网格 JSON 非法时抛出，信息面向用户。"""


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt(v):
    """刻度/级别标签的统一格式化。"""
    if v == 0:
        v = 0.0  # 去掉 "-0"
    return f"{v:.6g}"


class GridData:
    """校验通过的网格数据。"""

    def __init__(self, rows, cols, x_range, y_range, matrix, levels):
        self.rows = rows
        self.cols = cols
        self.xmin, self.xmax = float(x_range[0]), float(x_range[1])
        self.ymin, self.ymax = float(y_range[0]), float(y_range[1])
        self.matrix = matrix          # matrix[j][i]，float 或 math.nan
        self.levels = list(levels)
        self.dx = (self.xmax - self.xmin) / (cols - 1)
        self.dy = (self.ymax - self.ymin) / (rows - 1)
        finite = [v for row in matrix for v in row if not math.isnan(v)]
        self.zmin = min(finite) if finite else None
        self.zmax = max(finite) if finite else None

    def x(self, i):
        return self.xmin + i * self.dx

    def y(self, j):
        return self.ymin + j * self.dy


def load_grid(path):
    """读取并校验网格 JSON；任何非法输入都抛出 GridDataError。"""
    if not os.path.isfile(path):
        raise GridDataError(f"文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)  # Python json 可解析 NaN 字面量
    except json.JSONDecodeError as e:
        raise GridDataError(f"JSON 解析失败: {e}") from e
    except OSError as e:
        raise GridDataError(f"无法读取文件: {e}") from e
    if not isinstance(obj, dict):
        raise GridDataError("JSON 顶层必须是对象")

    for key in REQUIRED_FIELDS:
        if key not in obj:
            raise GridDataError(f"缺少必需字段: '{key}'")

    rows, cols = obj["rows"], obj["cols"]
    for name, n in (("rows", rows), ("cols", cols)):
        if not isinstance(n, int) or isinstance(n, bool):
            raise GridDataError(f"字段 '{name}' 必须是整数，得到: {n!r}")
        if not 2 <= n <= MAX_DIM:
            raise GridDataError(f"字段 '{name}' 必须在 2..{MAX_DIM} 之间，得到: {n}")

    def _range(key):
        r = obj[key]
        if (not isinstance(r, (list, tuple)) or len(r) != 2
                or not all(_is_num(v) for v in r)):
            raise GridDataError(f"字段 '{key}' 必须是 [最小值, 最大值] 两个数值")
        lo, hi = float(r[0]), float(r[1])
        if not (math.isfinite(lo) and math.isfinite(hi)):
            raise GridDataError(f"字段 '{key}' 含有非有限数值")
        if not lo < hi:
            raise GridDataError(f"字段 '{key}' 要求最小值小于最大值，得到: {r}")
        return [lo, hi]

    x_range = _range("x_range")
    y_range = _range("y_range")

    levels = obj["levels"]
    if not isinstance(levels, list) or not 1 <= len(levels) <= MAX_LEVELS:
        raise GridDataError(
            f"字段 'levels' 必须是 1..{MAX_LEVELS} 个数值的列表")
    for v in levels:
        if not _is_num(v) or not math.isfinite(float(v)):
            raise GridDataError(f"级别含有非法数值: {v!r}")
    levels = [float(v) for v in levels]

    matrix = obj["matrix"]
    if not isinstance(matrix, list) or len(matrix) != rows:
        got = len(matrix) if isinstance(matrix, list) else type(matrix).__name__
        raise GridDataError(
            f"矩阵行数与 'rows' 不符: 期望 {rows} 行，实际 {got}")
    checked = []
    for j, row in enumerate(matrix):
        if not isinstance(row, list) or len(row) != cols:
            got = len(row) if isinstance(row, list) else type(row).__name__
            raise GridDataError(
                f"矩阵第 {j} 行列数与 'cols' 不符: 期望 {cols} 列，实际 {got}")
        out_row = []
        for i, v in enumerate(row):
            if v is None:  # null 视为无数据单元格
                out_row.append(math.nan)
                continue
            if not _is_num(v):
                raise GridDataError(f"矩阵单元格 ({i}, {j}) 不是数值: {v!r}")
            v = float(v)
            if math.isinf(v):
                raise GridDataError(f"矩阵单元格 ({i}, {j}) 为无穷大，非法")
            out_row.append(v)  # NaN 保留为无数据单元格
        checked.append(out_row)

    return GridData(rows, cols, x_range, y_range, checked, levels)


# ---------------------------------------------------------------- 等值线计算

# marching squares 边表。角点: a=左下 b=右下 c=右上 d=左上；
# 边: B=下(a-b) R=右(b-c) T=上(c-d) L=左(d-a)。
# 索引位: bit0=a, bit1=b, bit2=c, bit3=d（置位表示角点值 >= 级别）。
_EDGE_TABLE = {
    1: [("L", "B")], 2: [("B", "R")], 3: [("R", "L")],
    4: [("R", "T")], 5: [("B", "L"), ("R", "T")], 6: [("B", "T")],
    7: [("T", "L")], 8: [("T", "L")], 9: [("B", "T")],
    10: [("T", "L"), ("B", "R")], 11: [("R", "T")], 12: [("R", "L")],
    13: [("B", "R")], 14: [("B", "L")],
}


def build_segments(data):
    """对每个唯一级别做 marching squares，返回 {级别: [((x0,y0),(x1,y1)), ...]}。

    含 NaN 角点的单元格整体跳过，因此 NaN 不参与等值线绘制；
    级别超出数据范围时自然得到空线段列表（不绘制该线）。
    """
    m = data.matrix
    result = {}
    for lev in sorted(set(data.levels)):
        segs = []
        for j in range(data.rows - 1):
            y0, y1 = data.y(j), data.y(j + 1)
            row0, row1 = m[j], m[j + 1]
            for i in range(data.cols - 1):
                a, b = row0[i], row0[i + 1]
                d, c = row1[i], row1[i + 1]
                if math.isnan(a) or math.isnan(b) or math.isnan(c) or math.isnan(d):
                    continue
                idx = ((1 if a >= lev else 0) | (2 if b >= lev else 0)
                       | (4 if c >= lev else 0) | (8 if d >= lev else 0))
                pairs = _EDGE_TABLE.get(idx)
                if not pairs:
                    continue
                x0, x1 = data.x(i), data.x(i + 1)

                def interp(v0, v1):
                    den = v1 - v0
                    return 0.5 if den == 0 else (lev - v0) / den

                ep = {}

                def edge_point(e):
                    if e not in ep:
                        if e == "B":
                            t = interp(a, b)
                            ep[e] = (x0 + t * (x1 - x0), y0)
                        elif e == "R":
                            t = interp(b, c)
                            ep[e] = (x1, y0 + t * (y1 - y0))
                        elif e == "T":
                            t = interp(d, c)
                            ep[e] = (x0 + t * (x1 - x0), y1)
                        else:  # "L"
                            t = interp(a, d)
                            ep[e] = (x0, y0 + t * (y1 - y0))
                    return ep[e]

                for e1, e2 in pairs:
                    segs.append((edge_point(e1), edge_point(e2)))
        result[lev] = segs
    return result


def level_colors(levels_sorted):
    """按级别数值从冷（蓝）到暖（红）分配唯一颜色。"""
    n = len(levels_sorted)
    colors = {}
    for k, lev in enumerate(levels_sorted):
        t = 0.5 if n == 1 else k / (n - 1)
        hue = int(round(240 * (1.0 - t)))  # 240=蓝(冷) ... 0=红(暖)
        colors[lev] = QColor.fromHsv(hue, 220, 215)
    return colors


def _make_anchors(segs, max_labels=4):
    """为级别值标注挑选线段中点（数据坐标）。"""
    if not segs:
        return []
    n = min(max_labels, len(segs))
    out = []
    for k in range(n):
        idx = min(int((k + 0.5) * len(segs) / n), len(segs) - 1)
        (p0, p1) = segs[idx]
        out.append(((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0))
    return out


def _nice_step(raw):
    exp = math.floor(math.log10(raw)) if raw > 0 else 0
    f = raw / 10 ** exp
    if f < 1.5:
        nf = 1.0
    elif f < 3.0:
        nf = 2.0
    elif f < 7.0:
        nf = 5.0
    else:
        nf = 10.0
    return nf * 10 ** exp


def nice_ticks(v0, v1, target=6):
    """视图范围内的美观刻度，且始终包含两个端点。"""
    step = _nice_step((v1 - v0) / max(target, 1))
    ticks = []
    t = math.ceil(v0 / step - 1e-9) * step
    guard = 0
    while t <= v1 + 1e-9 * max(1.0, abs(step)) and guard < 1000:
        ticks.append(0.0 if abs(t) < 1e-12 else t)
        t += step
        guard += 1
    eps = 1e-9 * max(1.0, abs(v0), abs(v1))
    if not ticks or abs(ticks[0] - v0) > eps:
        ticks.insert(0, v0)
    if abs(ticks[-1] - v1) > eps:
        ticks.append(v1)
    return ticks


# ---------------------------------------------------------------- 绘图部件

class ContourWidget(QWidget):
    """等值线图部件：绘制、悬停定位、框选缩放。"""

    statusMessage = Signal(str)
    statusCleared = Signal()

    ML, MR, MT, MB = 70, 18, 18, 48   # 坐标轴边距
    MIN_DRAG_PX = 4                     # 小于该像素视为单击

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data
        self.setMouseTracking(True)
        self.setMinimumSize(480, 360)
        self.full_view = [data.xmin, data.xmax, data.ymin, data.ymax]
        self.view = list(self.full_view)  # [vx0, vx1, vy0, vy1]
        self._segments = build_segments(data)
        self._levels_sorted = sorted(self._segments)
        self._colors = level_colors(self._levels_sorted)
        self._paths = {}
        self._anchors = {}
        for lev, segs in self._segments.items():
            path = QPainterPath()
            for (p0, p1) in segs:
                path.moveTo(p0[0], p0[1])
                path.lineTo(p1[0], p1[1])
            self._paths[lev] = path
            self._anchors[lev] = _make_anchors(segs)
        self._drag_start = None
        self._drag_cur = None
        # 供自检读取
        self._xtick_labels = []
        self._ytick_labels = []
        self._drawn_label_count = 0

    # ---- 坐标变换 ----
    def plot_rect(self):
        return QRectF(self.ML, self.MT,
                      max(10.0, self.width() - self.ML - self.MR),
                      max(10.0, self.height() - self.MT - self.MB))

    def data_to_screen(self, x, y):
        vx0, vx1, vy0, vy1 = self.view
        pr = self.plot_rect()
        sx = pr.left() + (x - vx0) / (vx1 - vx0) * pr.width()
        sy = pr.bottom() - (y - vy0) / (vy1 - vy0) * pr.height()
        return QPointF(sx, sy)

    def screen_to_data(self, pos):
        vx0, vx1, vy0, vy1 = self.view
        pr = self.plot_rect()
        x = vx0 + (pos.x() - pr.left()) / pr.width() * (vx1 - vx0)
        y = vy0 + (pr.bottom() - pos.y()) / pr.height() * (vy1 - vy0)
        return x, y

    def reset_view(self):
        self.view = list(self.full_view)
        self._drag_start = None
        self._drag_cur = None
        self.update()

    # ---- 绘制 ----
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), Qt.GlobalColor.white)
        pr = self.plot_rect()
        self._draw_contours(p, pr)
        self._draw_level_labels(p, pr)
        self._draw_axes(p, pr)
        if self._drag_start is not None and self._drag_cur is not None:
            rect = QRectF(self._drag_start, self._drag_cur).normalized()
            p.setPen(QPen(QColor(30, 100, 200), 1, Qt.PenStyle.DashLine))
            p.setBrush(QColor(60, 120, 200, 40))
            p.drawRect(rect)
        p.end()

    def _draw_contours(self, p, pr):
        vx0, vx1, vy0, vy1 = self.view
        p.save()
        p.setClipRect(pr)
        p.translate(pr.left(), pr.bottom())
        p.scale(pr.width() / (vx1 - vx0), -pr.height() / (vy1 - vy0))
        p.translate(-vx0, -vy0)
        for lev in self._levels_sorted:
            if not self._segments[lev]:
                continue  # 超范围级别：不绘制
            pen = QPen(self._colors[lev])
            pen.setWidthF(1.6)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.drawPath(self._paths[lev])
        p.restore()

    def _draw_level_labels(self, p, pr):
        p.save()
        p.setClipRect(pr)
        font = p.font()
        font.setPointSizeF(8.5)
        p.setFont(font)
        self._drawn_label_count = 0
        for lev in self._levels_sorted:
            text = _fmt(lev)
            color = self._colors[lev]
            for ax, ay in self._anchors[lev]:
                sp = self.data_to_screen(ax, ay)
                if not pr.contains(sp):
                    continue
                rect = p.fontMetrics().boundingRect(text)
                rect.moveCenter(sp.toPoint())
                rect.adjust(-2, -1, 2, 1)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 210))
                p.drawRect(rect)
                p.setPen(QPen(color.darker(130)))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
                self._drawn_label_count += 1
        p.restore()

    def _draw_axes(self, p, pr):
        vx0, vx1, vy0, vy1 = self.view
        font = p.font()
        font.setPointSizeF(8.5)
        p.setFont(font)
        grid_pen = QPen(QColor(225, 225, 225))
        axis_pen = QPen(QColor(0, 0, 0))
        p.setPen(axis_pen)
        p.drawRect(pr)

        xticks = nice_ticks(vx0, vx1)
        yticks = nice_ticks(vy0, vy1)
        self._xtick_labels = [_fmt(t) for t in xticks]
        self._ytick_labels = [_fmt(t) for t in yticks]

        for t, label in zip(xticks, self._xtick_labels):
            x = self.data_to_screen(t, vy0).x()
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, pr.top()), QPointF(x, pr.bottom()))
            p.setPen(axis_pen)
            p.drawLine(QPointF(x, pr.bottom()), QPointF(x, pr.bottom() + 5))
            p.drawText(QRectF(x - 45, pr.bottom() + 7, 90, 16),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       label)
        for t, label in zip(yticks, self._ytick_labels):
            y = self.data_to_screen(vx0, t).y()
            p.setPen(grid_pen)
            p.drawLine(QPointF(pr.left(), y), QPointF(pr.right(), y))
            p.setPen(axis_pen)
            p.drawLine(QPointF(pr.left() - 5, y), QPointF(pr.left(), y))
            p.drawText(QRectF(2, y - 8, self.ML - 10, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       label)
        p.drawText(QRectF(pr.left(), pr.bottom() + 26, pr.width(), 16),
                   Qt.AlignmentFlag.AlignHCenter, "X")
        p.drawText(QRectF(2, 2, self.ML - 10, 14),
                   Qt.AlignmentFlag.AlignRight, "Y")

    # ---- 鼠标交互 ----
    def mouseMoveEvent(self, event):
        pos = event.position()
        if (self._drag_start is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self._drag_cur = pos
            self.update()
        self._update_hover(pos)

    def _update_hover(self, pos):
        d = self.data
        if not self.plot_rect().contains(pos):
            self.statusMessage.emit("超出网格范围")
            return
        x, y = self.screen_to_data(pos)
        eps = 1e-9 * max(1.0, abs(d.xmax), abs(d.ymax))
        if (x < d.xmin - eps or x > d.xmax + eps
                or y < d.ymin - eps or y > d.ymax + eps):
            self.statusMessage.emit("超出网格范围")
            return
        i = min(max(int(round((x - d.xmin) / d.dx)), 0), d.cols - 1)
        j = min(max(int(round((y - d.ymin) / d.dy)), 0), d.rows - 1)
        gx, gy = d.x(i), d.y(j)
        v = d.matrix[j][i]
        if math.isnan(v):
            msg = (f"网格点 ({i}, {j})   x = {gx:.4f}   y = {gy:.4f}   无数据")
        else:
            msg = (f"网格点 ({i}, {j})   x = {gx:.4f}   y = {gy:.4f}"
                   f"   高度 = {v:.4f}")
        self.statusMessage.emit(msg)

    def leaveEvent(self, event):
        self.statusCleared.emit()

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.plot_rect().contains(event.position())):
            self._drag_start = event.position()
            self._drag_cur = None

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._drag_start is None:
            return
        start = self._drag_start
        cur = event.position()
        self._drag_start = None
        self._drag_cur = None
        rect = QRectF(start, cur).normalized()
        if rect.width() < self.MIN_DRAG_PX or rect.height() < self.MIN_DRAG_PX:
            self.update()
            return  # 视为单击，不缩放
        d = self.data
        x_a, y_a = self.screen_to_data(rect.topLeft())
        x_b, y_b = self.screen_to_data(rect.bottomRight())
        nx0, nx1 = sorted((x_a, x_b))
        ny0, ny1 = sorted((y_a, y_b))
        nx0 = max(nx0, d.xmin); nx1 = min(nx1, d.xmax)
        ny0 = max(ny0, d.ymin); ny1 = min(ny1, d.ymax)
        vx0, vx1, vy0, vy1 = self.view
        too_small = []
        if nx1 - nx0 < d.dx:
            too_small.append("宽度")
        else:
            vx0, vx1 = nx0, nx1
        if ny1 - ny0 < d.dy:
            too_small.append("高度")
        else:
            vy0, vy1 = ny0, ny1
        if too_small:
            self.statusMessage.emit(
                f"区域太小：框选{'、'.join(too_small)}小于一个网格间距，"
                f"已取消该方向缩放")
        self.view = [vx0, vx1, vy0, vy1]
        self.update()


# ---------------------------------------------------------------- 主窗口

class MainWindow(QMainWindow):
    def __init__(self, data, source_path):
        super().__init__()
        self.data = data
        self.setWindowTitle(f"地形高度等值线查看器 - {os.path.basename(source_path)}")
        self.viewer = ContourWidget(data)
        self.viewer.statusMessage.connect(self.statusBar().showMessage)
        self.viewer.statusCleared.connect(self.statusBar().clearMessage)

        toolbar = QToolBar("视图", self)
        self.addToolBar(toolbar)
        act_reset = QAction("重置视图 (R)", self)
        act_reset.setShortcut("R")
        act_reset.triggered.connect(self.reset_view)
        toolbar.addAction(act_reset)
        self.act_reset = act_reset

        panel = QWidget(self)
        vlay = QVBoxLayout(panel)
        vlay.addWidget(QLabel("等值线级别（冷→暖）"))
        self.legend = QListWidget()
        for lev in data.levels:
            pm = QPixmap(36, 12)
            pm.fill(self.viewer._colors[lev])
            text = _fmt(lev)
            if data.zmin is None:
                text += "（无数据）"
            elif lev < data.zmin or lev > data.zmax:
                text += "（超出范围）"
            self.legend.addItem(QListWidgetItem(QIcon(pm), text))
        vlay.addWidget(self.legend)
        panel.setMaximumWidth(210)

        central = QWidget(self)
        hlay = QHBoxLayout(central)
        hlay.addWidget(self.viewer, 1)
        hlay.addWidget(panel)
        self.setCentralWidget(central)
        self.resize(1060, 720)
        self.statusBar().showMessage("悬停查看网格点数值，拖拽框选放大，按 R 重置视图")

    def reset_view(self):
        self.viewer.reset_view()
        self.statusBar().clearMessage()  # 清除悬停信息


def main(argv):
    args = list(argv[1:])
    quit_after = None
    if "--quit-after" in args:
        idx = args.index("--quit-after")
        try:
            quit_after = int(args[idx + 1])
        except (IndexError, ValueError):
            print("错误: --quit-after 需要整数毫秒数", file=sys.stderr)
            return 2
        del args[idx:idx + 2]
    if args:
        path = args[0]
    else:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "samples", "sample_normal.json")
    try:
        data = load_grid(path)
    except GridDataError as e:
        # 明确报错并终止，不创建任何窗口
        print(f"错误: {e}", file=sys.stderr)
        return 2

    app = QApplication(argv[:1])
    win = MainWindow(data, path)
    win.show()
    if quit_after is not None:
        QTimer.singleShot(quit_after, win.close)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
