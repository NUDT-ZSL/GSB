# -*- coding: utf-8 -*-
"""自动化自检：覆盖任务要求 (1)-(14)。

以离屏（offscreen）方式实例化真实窗口部件，注入鼠标事件验证交互，
并把渲染结果截图保存到 results/ 供人工核对。要求 (1) 与 (14) 通过
子进程真实启动程序验证。

运行: python run_tests.py
"""
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
RESULTS = os.path.join(HERE, "results")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, HERE)

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import contour_viewer as cv  # noqa: E402

RESULTS_LIST = []


def check(req, desc, cond, detail=""):
    RESULTS_LIST.append((req, bool(cond)))
    tag = "PASS" if cond else "FAIL"
    line = f"[{tag}] 要求{req}: {desc}"
    if detail:
        line += f"  ({detail})"
    print(line)


def send_move(w, pos, buttons=Qt.MouseButton.NoButton):
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos),
                     Qt.MouseButton.NoButton, buttons,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(w, ev)


def send_press(w, pos):
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(pos),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(w, ev)


def send_release(w, pos):
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(pos),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(w, ev)


def drag(w, p_from, p_to):
    send_press(w, p_from)
    mid = QPointF((p_from.x() + p_to.x()) / 2, (p_from.y() + p_to.y()) / 2)
    send_move(w, mid, Qt.MouseButton.LeftButton)
    send_move(w, p_to, Qt.MouseButton.LeftButton)
    send_release(w, p_to)


def make_window(sample):
    data = cv.load_grid(os.path.join(SAMPLES, sample))
    win = cv.MainWindow(data, sample)
    win.show()
    return win


def main():
    subprocess.run([sys.executable, os.path.join(HERE, "gen_samples.py")],
                   check=True, capture_output=True)
    os.makedirs(RESULTS, exist_ok=True)
    app = QApplication([])

    # ---------------- 要求 (1)：非法输入明确报错并终止，不绘制图形 ----------
    for bad in ("bad_missing_field.json", "bad_dim_mismatch.json"):
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "contour_viewer.py"),
             os.path.join(SAMPLES, bad)],
            capture_output=True, text=True, timeout=120)
        ok = r.returncode == 2 and "错误" in r.stderr
        check(1, f"非法样例 {bad} 报错退出且不创建窗口", ok,
              f"退出码={r.returncode}, stderr 末行: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}")

    # ---------------- 正常样例窗口 ----------------------------------------
    win = make_window("sample_normal.json")
    viewer = win.viewer
    data = viewer.data
    shot = os.path.join(RESULTS, "01_normal.png")
    win.grab().save(shot)  # grab 触发完整绘制

    # ---------------- 要求 (2)：等值线、颜色区分、级别标注 -----------------
    seg_counts = {lev: len(viewer._segments[lev]) for lev in viewer._levels_sorted}
    check(2, "每个级别均绘制等值线", all(n > 0 for n in seg_counts.values()),
          f"各级别线段数: {seg_counts}")
    colors = [viewer._colors[lev] for lev in viewer._levels_sorted]
    check(2, "各级别颜色互不相同",
          len({c.rgb() for c in colors}) == len(colors))
    check(2, "图上标注各级别值",
          viewer._drawn_label_count >= len(viewer._levels_sorted),
          f"标注数={viewer._drawn_label_count}, 级别数={len(viewer._levels_sorted)}")

    # ---------------- 要求 (3)：轴刻度覆盖 JSON 的 x/y 范围 ----------------
    check(3, "X 轴标签含 JSON 最小/最大值",
          cv._fmt(data.xmin) in viewer._xtick_labels
          and cv._fmt(data.xmax) in viewer._xtick_labels,
          f"X 刻度: {viewer._xtick_labels}")
    check(3, "Y 轴标签含 JSON 最小/最大值",
          cv._fmt(data.ymin) in viewer._ytick_labels
          and cv._fmt(data.ymax) in viewer._ytick_labels,
          f"Y 刻度: {viewer._ytick_labels}")

    # ---------------- 要求 (4)：悬停显示最近网格点坐标与数值 ---------------
    i0, j0 = 10, 20
    gx, gy = data.x(i0), data.y(j0)
    v0 = data.matrix[j0][i0]
    send_move(viewer, viewer.data_to_screen(gx, gy))
    msg = win.statusBar().currentMessage()
    expect = f"x = {gx:.4f}"
    ok = (expect in msg and f"y = {gy:.4f}" in msg
          and f"高度 = {v0:.4f}" in msg)
    check(4, "悬停已知网格点，状态栏坐标与数值正确(误差<0.001)", ok, msg)

    # ---------------- 要求 (5)：移出网格范围 ------------------------------
    send_move(viewer, QPointF(5, 5))  # 左边距区域
    msg_out = win.statusBar().currentMessage()
    check(5, "移出范围仅提示，无坐标与数值",
          "超出网格范围" in msg_out and "x =" not in msg_out
          and "高度" not in msg_out, msg_out)

    # ---------------- 要求 (9) 前半：记录缩放前悬停文本 -------------------
    iz, jz = 40, 30  # 位于待框选区域内的点
    pz = (data.x(iz), data.y(jz))
    send_move(viewer, viewer.data_to_screen(*pz))
    hover_before = win.statusBar().currentMessage()

    # ---------------- 要求 (6)：拖拽框选缩放 ------------------------------
    x0z, y0z, x1z, y1z = 2.0, -1.0, 5.0, 3.0
    drag(viewer, viewer.data_to_screen(x0z, y0z), viewer.data_to_screen(x1z, y1z))
    vx0, vx1, vy0, vy1 = viewer.view
    ok = (abs(vx0 - x0z) < 1e-6 and abs(vx1 - x1z) < 1e-6
          and abs(vy0 - y0z) < 1e-6 and abs(vy1 - y1z) < 1e-6)
    check(6, "框选后视图缩放到矩形区域", ok,
          f"视图=({vx0:.4f},{vx1:.4f},{vy0:.4f},{vy1:.4f})")
    win.grab().save(os.path.join(RESULTS, "02_zoomed.png"))
    check(6, "轴刻度同步更新为框选边界",
          cv._fmt(x0z) in viewer._xtick_labels
          and cv._fmt(x1z) in viewer._xtick_labels
          and cv._fmt(y0z) in viewer._ytick_labels
          and cv._fmt(y1z) in viewer._ytick_labels,
          f"X 刻度: {viewer._xtick_labels} Y 刻度: {viewer._ytick_labels}")

    # ---------------- 要求 (9) 后半：缩放后悬停一致 ------------------------
    send_move(viewer, viewer.data_to_screen(*pz))
    hover_after = win.statusBar().currentMessage()
    check(9, "缩放后悬停同一网格点显示完全一致",
          hover_after == hover_before,
          f"前: {hover_before} | 后: {hover_after}")

    # ---------------- 要求 (10)：颜色冷→暖且缩放前后不变 -------------------
    colors_after = [viewer._colors[lev] for lev in viewer._levels_sorted]
    hues = [c.hue() for c in colors_after]
    check(10, "级别颜色按数值从冷到暖排列且每级唯一",
          all(hues[k] > hues[k + 1] for k in range(len(hues) - 1)),
          f"色相序列: {hues}")
    check(10, "缩放前后同一级别颜色保持不变",
          all(a.rgb() == b.rgb() for a, b in zip(colors, colors_after)))

    # ---------------- 要求 (7)：框选过小取消缩放并提示 ---------------------
    view_saved = list(viewer.view)
    cx, cy = 3.0, 1.0
    tiny_to = (cx + 0.5 * data.dx, cy + 0.5 * data.dy)
    drag(viewer, viewer.data_to_screen(cx, cy), viewer.data_to_screen(*tiny_to))
    msg_tiny = win.statusBar().currentMessage()
    check(7, "框选小于一个网格间距：视图不变并提示区域太小",
          viewer.view == view_saved and "区域太小" in msg_tiny, msg_tiny)

    # ---------------- 要求 (8)：重置视图 ----------------------------------
    send_move(viewer, viewer.data_to_screen(*pz))  # 先制造悬停信息
    win.act_reset.trigger()
    check(8, "重置恢复完整范围并清除悬停信息",
          viewer.view == viewer.full_view
          and win.statusBar().currentMessage() == "",
          f"视图={viewer.view}")

    # ---------------- 要求 (11)：超范围级别不绘制但列表显示 -----------------
    win2 = make_window("sample_levels_out_of_range.json")
    d2 = win2.viewer.data
    oor = [lev for lev in d2.levels if lev < d2.zmin or lev > d2.zmax]
    ok_no_line = all(len(win2.viewer._segments[lev]) == 0 for lev in oor)
    legend_texts = [win2.legend.item(k).text()
                    for k in range(win2.legend.count())]
    ok_listed = all(any(cv._fmt(lev) in t for t in legend_texts) for lev in oor)
    check(11, "超范围级别不绘制对应线条", ok_no_line,
          f"超范围级别: {oor}")
    check(11, "级别列表仍显示超范围的值", ok_listed,
          f"图例末两项: {legend_texts[-2:]}")
    win2.grab().save(os.path.join(RESULTS, "03_levels_out_of_range.png"))

    # ---------------- 要求 (12)：NaN 单元格 -------------------------------
    win3 = make_window("sample_nan.json")
    d3 = win3.viewer.data
    inan, jnan = 34, 24  # 位于 NaN 空洞内
    assert math.isnan(d3.matrix[jnan][inan])
    send_move(win3.viewer, win3.viewer.data_to_screen(d3.x(inan), d3.y(jnan)))
    msg_nan = win3.statusBar().currentMessage()
    check(12, "悬停 NaN 网格点显示无数据", "无数据" in msg_nan, msg_nan)
    # NaN 空洞内的单元格不产生任何级别的线段
    lev0 = win3.viewer._levels_sorted[len(win3.viewer._levels_sorted) // 2]
    hole_segs = [s for s in win3.viewer._segments[lev0]
                 if all(d3.x(30) - 1e-9 <= p[0] <= d3.x(37) + 1e-9
                        and d3.y(20) - 1e-9 <= p[1] <= d3.y(27) + 1e-9
                        for p in s)]
    check(12, "NaN 空洞内无等值线穿过", len(hole_segs) == 0,
          f"空洞内线段数={len(hole_segs)}")

    # ---------------- 要求 (13)：混合异常下其余内容完整可用 -----------------
    win4 = make_window("sample_mixed.json")
    d4 = win4.viewer.data
    in_range = [lev for lev in d4.levels if d4.zmin <= lev <= d4.zmax]
    ok_draw = all(len(win4.viewer._segments[lev]) > 0 for lev in in_range)
    iok, jok = 10, 10  # 正常区域网格点
    send_move(win4.viewer, win4.viewer.data_to_screen(d4.x(iok), d4.y(jok)))
    msg_ok = win4.statusBar().currentMessage()
    ok_hover = f"高度 = {d4.matrix[jok][iok]:.4f}" in msg_ok
    # 缩放交互仍可用
    drag(win4.viewer, win4.viewer.data_to_screen(1.0, -2.0),
         win4.viewer.data_to_screen(4.0, 2.0))
    ok_zoom = abs(win4.viewer.view[0] - 1.0) < 1e-6
    check(13, "混合异常样例中正常区域线条、悬停、缩放均可用",
          ok_draw and ok_hover and ok_zoom,
          f"悬停: {msg_ok}")
    win4.grab().save(os.path.join(RESULTS, "04_mixed.png"))

    # ---------------- 要求 (14)：窗口关闭干净退出 ---------------------------
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)  # 真实窗口运行
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "contour_viewer.py"),
         os.path.join(SAMPLES, "sample_normal.json"), "--quit-after", "800"],
        capture_output=True, text=True, timeout=120, env=env)
    check(14, "窗口关闭后进程干净结束，无错误",
          r.returncode == 0, f"退出码={r.returncode}")

    for w in (win, win2, win3, win4):
        w.close()

    n_fail = sum(1 for _, ok in RESULTS_LIST if not ok)
    print(f"\n共 {len(RESULTS_LIST)} 项检查，"
          f"通过 {len(RESULTS_LIST) - n_fail} 项，失败 {n_fail} 项。")
    print(f"截图已保存到: {RESULTS}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
