# -*- coding: utf-8 -*-
"""
无头验收测试: 在 SDL dummy 视频驱动下逐条验证 12 项验收要求。

运行: python run_acceptance_tests.py
全部通过时退出码 0, 任一失败退出码 1。
"""

import json
import os
import re
import shutil
import sys

# Windows 控制台默认 GBK, 统一切到 UTF-8 输出, 避免中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 必须在 import pygame 前指定 dummy 驱动, 实现无界面运行
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

import pygame  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# -I 隔离模式下某些发行版 (如 Anaconda) 不自动注入脚本目录, 显式补上
sys.path.insert(0, HERE)
import texture_viewer as tv  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
WORK = os.path.join(HERE, "test_work")

RESULTS = []


def check(no, title, ok, detail=""):
    RESULTS.append((no, title, bool(ok), detail))


def make_viewer(texture_name="test_2048x1024.png", config=None, state=None):
    cfg = config if config is not None else tv.load_config(None)
    return tv.TextureViewer(os.path.join(SAMPLES, texture_name), cfg, state)


def key_event(key):
    return pygame.event.Event(pygame.KEYDOWN, key=key)


# ------------------------------------------------------------ 验收 1: 加载与标题
def test_01_load_and_title():
    v = make_viewer()
    v.draw()  # 绘制一帧, 确认渲染链路不报错
    title = pygame.display.get_caption()[0]
    ok = (v.texture is not None
          and (v.tex_w, v.tex_h) == (2048, 1024)
          and "test_2048x1024.png" in title
          and "2048" in title and "1024" in title)
    # 默认 1 倍下视口 (800x570) 居中, 可见范围完全落在纹理内
    x1, y1, x2, y2 = v.visible_rect()
    ok = ok and 0 <= x1 and x2 <= 2048 and 0 <= y1 and y2 <= 1024
    check(1, "加载PNG完整显示, 标题含文件名与2048x1024", ok,
          "title=%r, rect=(%.1f,%.1f,%.1f,%.1f)" % (title, x1, y1, x2, y2))


# ------------------------------------------------------------ 验收 2: 缩放边界
def test_02_zoom_bounds():
    v = make_viewer()
    for _ in range(60):
        v.zoom_at(400, 300, 1)
    at_max = v.visible_rect()
    ok_up = abs(v.scale - tv.MAX_SCALE) < 1e-9
    for _ in range(20):  # 达 8x 后继续滚动
        v.zoom_at(400, 300, 1)
    after_more = v.visible_rect()
    ok_stuck = abs(v.scale - tv.MAX_SCALE) < 1e-9 and at_max == after_more

    v2 = make_viewer()
    for _ in range(60):
        v2.zoom_at(400, 300, -1)
    at_min = v2.visible_rect()
    ok_down = abs(v2.scale - tv.MIN_SCALE) < 1e-9
    for _ in range(20):
        v2.zoom_at(400, 300, -1)
    ok_low_stuck = abs(v2.scale - tv.MIN_SCALE) < 1e-9 and at_min == v2.visible_rect()
    check(2, "滚轮缩放限定1/8~8倍, 触界后视图不变",
          ok_up and ok_stuck and ok_down and ok_low_stuck,
          "max=%.4f stuck=%s min=%.4f low_stuck=%s"
          % (v.scale, ok_stuck, v2.scale, ok_low_stuck))


# ------------------------------------------------------------ 验收 3: 平移边界
def test_03_pan_clamp():
    v = make_viewer()
    # 先拖向右下进入纹理内部 (内容跟随鼠标, 视图中心增大); 用较小拖拽避免直接到边
    v.pan_by_pixels(300, 200)
    x1, y1, x2, y2 = v.visible_rect()
    ok_inside = (x1 > 1 and y1 > 1 and x2 < 2048 and y2 < 1024)

    # 再猛拖向左上 (负值), 必须停在纹理左上角, 且继续拖不动
    v.pan_by_pixels(-9000, -9000)
    x1, y1, x2, y2 = v.visible_rect()
    ok_corner = (abs(x1) < 1e-6 and abs(y1) < 1e-6
                 and x2 <= 2048 + 1e-6 and y2 <= 1024 + 1e-6)
    before = v.visible_rect()
    v.pan_by_pixels(-3000, -3000)  # 到边后继续向左上拖
    ok_stop = before == v.visible_rect()

    # 反方向猛拖到右下角, 同样不越界
    v.pan_by_pixels(9000, 9000)
    x1, y1, x2, y2 = v.visible_rect()
    ok_br = (abs(x2 - 2048) < 1e-6 and abs(y2 - 1024) < 1e-6 and x1 >= -1e-6
             and y1 >= -1e-6)

    # 小纹理 (256x128) 小于视口: 居中且拖拽不产生纹理外偏移
    vs = make_viewer("test_256x128.png")
    cx_before = (vs.view_x, vs.view_y)
    vs.pan_by_pixels(500, 500)
    ok_center = cx_before == (vs.view_x, vs.view_y)
    check(3, "拖拽平移不越出纹理边界, 到边停止无空白",
          ok_inside and ok_corner and ok_stop and ok_br and ok_center,
          "inside=%s topleft=(%.2f,%.2f,%.2f,%.2f) stop=%s br=%s small=%s"
          % (ok_inside, x1, y1, x2, y2, ok_stop, ok_br, ok_center))


# ------------------------------------------------------------ 验收 4: 状态栏坐标
def test_04_status_bar():
    v = make_viewer()
    text0 = v.status_text()
    m = re.search(r"x1=([-\d.]+) y1=([-\d.]+) x2=([-\d.]+) y2=([-\d.]+) \| ([\d.]+)x",
                  text0)
    ok = m is not None
    if m:
        got = tuple(float(g) for g in m.groups()[:4])
        exp = v.visible_rect()
        ok = all(abs(a - b) < 0.06 for a, b in zip(got, exp)) and abs(
            float(m.group(5)) - 1.0) < 1e-6
    v.zoom_at(400, 300, 1)
    v.pan_by_pixels(120, 80)
    text1 = v.status_text()
    m1 = re.search(r"x1=([-\d.]+) y1=([-\d.]+) x2=([-\d.]+) y2=([-\d.]+) \| ([\d.]+)x",
                   text1)
    ok = ok and text1 != text0 and m1 is not None
    if m1:
        got = tuple(float(g) for g in m1.groups()[:4])
        exp = v.visible_rect()
        ok = ok and all(abs(a - b) < 0.06 for a, b in zip(got, exp))
    check(4, "状态栏实时显示可见纹理坐标范围与缩放倍数", ok,
          "before=%r after=%r" % (text0, text1))


# ------------------------------------------------------------ 验收 5: 网格对齐
def _capture_grid_lines(viewer):
    calls = []
    orig = pygame.draw.line

    def spy(surface, color, p1, p2, width=1):
        calls.append((tuple(color), tuple(p1), tuple(p2)))
        return orig(surface, color, p1, p2, width)

    pygame.draw.line = spy
    try:
        viewer._draw_grid()
    finally:
        pygame.draw.line = orig
    return calls


def test_05_grid_alignment():
    v = make_viewer()
    spacing = v.grid_spacing
    x1, y1, _, _ = v.visible_rect()
    calls = _capture_grid_lines(v)
    vert_tex = sorted({round(x1 + p1[0] / v.scale, 3)
                       for color, p1, p2 in calls if p1[0] == p2[0]})
    # 1 倍下所有竖线都落在 spacing 的整数倍纹理坐标上, 且 256 处确有网格线
    ok = all(abs(g - round(g / spacing) * spacing) < 0.02 for g in vert_tex)
    ok = ok and any(abs(g - 256.0) < 0.02 for g in vert_tex)

    # 放大到 4 倍并移动视图, 让 256 这条网格线仍落在可见范围内
    # 4 倍下视口宽 800px = 200 纹理像素, 取中心 300 => 可见 x 范围约 [200,400]
    v.scale = 4.0
    v.view_x, v.view_y = 300.0, 256.0
    v._clamp_view()
    x1b, y1b, x2b, y2b = v.visible_rect()
    calls_b = _capture_grid_lines(v)
    vert_tex_b = sorted({round(x1b + p1[0] / v.scale, 3)
                         for color, p1, p2 in calls_b if p1[0] == p2[0]})
    # 4 倍下同样全部对齐到 spacing 整数倍
    ok = ok and all(abs(g - round(g / spacing) * spacing) < 0.02
                    for g in vert_tex_b)
    # 同一纹理坐标 256 在两种缩放下都存在网格线
    has_256_a = any(abs(g - 256.0) < 0.02 for g in vert_tex)
    has_256_b = any(abs(g - 256.0) < 0.02 for g in vert_tex_b)
    # 两个视图交集内的网格线集合一致 (同一纹理坐标 => 同一组线)
    overlap_lo = max(x1, x1b)
    overlap_hi = min(x1 + 800, x2b)
    common_a = sorted(g for g in vert_tex if overlap_lo - 1 < g < overlap_hi + 1)
    common_b = sorted(g for g in vert_tex_b if overlap_lo - 1 < g < overlap_hi + 1)
    same_lines = (len(common_a) == len(common_b)
                  and all(abs(a - b) < 0.05 for a, b in zip(common_a, common_b)))
    ok = ok and has_256_a and has_256_b and same_lines
    check(5, "网格线按纹理像素间距对齐, 缩放后坐标不变", ok,
          "s1=%s s4=%s line256=(%s,%s) same=%s"
          % (vert_tex[:4], vert_tex_b[:4], has_256_a, has_256_b, same_lines))


# ------------------------------------------------------------ 验收 6: G 键开关
def test_06_grid_toggle():
    v = make_viewer()
    assert v.grid_enabled
    v.handle_event(key_event(pygame.K_g))
    off_text = v.status_text()
    ok_off = (not v.grid_enabled) and "网格:关" in off_text
    v.handle_event(key_event(pygame.K_g))
    on_text = v.status_text()
    ok_on = v.grid_enabled and "网格:开" in on_text
    check(6, "G键切换网格且状态栏同步显示开/关", ok_off and ok_on,
          "off=%r on=%r" % (off_text, on_text))


# ------------------------------------------------------------ 验收 7: M 键增删标记
def _marker_circle_count(viewer):
    count = 0
    orig = pygame.draw.circle

    def spy(surface, color, center, radius, width=0):
        nonlocal count
        if tuple(color) == tv.MARKER_COLOR:
            count += 1
        return orig(surface, color, center, radius, width)

    pygame.draw.circle = spy
    try:
        viewer.draw()
    finally:
        pygame.draw.circle = orig
    return count


def test_07_marker_add_remove():
    v = make_viewer()
    v.handle_event(key_event(pygame.K_m))   # 添加第 1 点
    v.pan_by_pixels(900, 600)               # 拖入纹理内部
    pos_after_pan = (v.view_x, v.view_y)
    assert pos_after_pan != (400.0, 285.0), "平移应改变视图中心"
    v.handle_event(key_event(pygame.K_m))   # 视图已变 => 添加第 2 点
    ok_two = len(v.markers) == 2
    v.handle_event(key_event(pygame.K_m))   # 视图未变 => 移除最近点
    ok_one = len(v.markers) == 1
    v.handle_event(key_event(pygame.K_m))   # 再移除
    ok_zero = len(v.markers) == 0
    ok_no_red = _marker_circle_count(v) == 0
    check(7, "M键添加关注点, 视图不变再按依次移除至0",
          ok_two and ok_one and ok_zero and ok_no_red,
          "two=%s one=%s zero=%s red_drawn=%d"
          % (ok_two, ok_one, ok_zero, _marker_circle_count(v)))


# ------------------------------------------------------------ 验收 8: 标记固定于纹理坐标
def test_08_markers_stick_to_texture():
    v = make_viewer()
    v.handle_event(key_event(pygame.K_m))
    tex_pos = tuple(v.markers[0])

    circles = []
    orig = pygame.draw.circle

    def spy(surface, color, center, radius, width=0):
        if tuple(color) == tv.MARKER_COLOR:
            circles.append(tuple(center))
        return orig(surface, color, center, radius, width)

    # 放大 4 倍并平移 (直接置位 4.0, 再向可移动方向平移)
    v.scale = 4.0
    v._clamp_view()
    v.pan_by_pixels(150, -120)
    x1, y1, _, _ = v.visible_rect()
    pygame.draw.circle = spy
    try:
        v._draw_markers()
    finally:
        pygame.draw.circle = orig
    ok = abs(v.scale - 4.0) < 1e-6 and len(circles) == 1
    back = None
    if ok:
        sx, sy = circles[0]
        back = (x1 + sx / v.scale, y1 + sy / v.scale)
        ok = abs(back[0] - tex_pos[0]) < 1.0 and abs(back[1] - tex_pos[1]) < 1.0
    check(8, "关注点保存于纹理坐标, 缩放平移后位置不变", ok,
          "tex=%s back=%s scale=%.2f" % (tex_pos, back, v.scale))


# ------------------------------------------------------------ 验收 9: 配置缺项/越界
def test_09_config_defaults():
    empty = tv.load_config(os.path.join(SAMPLES, "config_empty.json"))
    bad = tv.load_config(os.path.join(SAMPLES, "config_bad.json"))
    missing = tv.load_config(os.path.join(SAMPLES, "does_not_exist.json"))
    defaults = {"window_width": 800, "window_height": 600,
                "initial_scale": 1.0, "grid_spacing": 128}
    ok = empty == defaults and bad == defaults and missing == defaults
    valid = tv.load_config(os.path.join(SAMPLES, "config_valid.json"))
    ok = ok and valid == {"window_width": 1024, "window_height": 768,
                          "initial_scale": 2.0, "grid_spacing": 64}
    v = make_viewer(config=empty)
    ok = ok and (v.window_w, v.window_h) == (800, 600) and v.scale == 1.0
    check(9, "配置缺项/无效时回退800x600, 1.0, 128", ok,
          "empty=%s bad=%s valid=%s" % (empty, bad, valid))


# ------------------------------------------------------------ 验收 10: 纹理损坏
def test_10_broken_texture():
    v = make_viewer("broken.png")
    ok_broken = (v.texture is None and v.load_error is not None
                 and v.load_error.filename == "broken.png"
                 and "broken.png" in v.status_text())
    v.draw()  # 错误提示画面可绘制
    # 窗口仍响应关闭
    v.handle_event(pygame.event.Event(pygame.QUIT))
    ok_close = not v.running

    v2 = make_viewer("text_only.png")
    ok_text = v2.texture is None and v2.load_error.filename == "text_only.png"
    v2.draw()
    check(10, "纹理损坏显示含文件名的错误说明且程序保持运行",
          ok_broken and ok_close and ok_text,
          "error=%r close=%s" % (str(v.load_error), ok_close))


# ------------------------------------------------------------ 验收 11: 退出状态保存与恢复
def test_11_state_save_restore():
    state_dir = os.path.join(WORK, "state_roundtrip")
    if os.path.isdir(state_dir):
        shutil.rmtree(state_dir)
    os.makedirs(state_dir)
    state_path = os.path.join(state_dir, "view_state.json")

    v = make_viewer(state=state_path)
    v.scale = 4.0                        # 精确 4 倍
    v._clamp_view()
    v.pan_by_pixels(200, 130)            # 向纹理内部平移
    v.handle_event(key_event(pygame.K_m))
    v.pan_by_pixels(50, 40)              # 再次移动后加第二个标记
    v.handle_event(key_event(pygame.K_m))
    saved_rect = v.visible_rect()
    saved_markers = [tuple(m) for m in v.markers]
    v.save_state()

    with open(state_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    ok_fields = all(k in payload for k in ("scale", "view_left", "view_top", "markers"))
    ok_fields = ok_fields and abs(payload["scale"] - 4.0) < 1e-6
    # 保存的左上角就是可见矩形的左上角
    ok_fields = ok_fields and abs(payload["view_left"] - saved_rect[0]) < 0.02
    ok_fields = ok_fields and abs(payload["view_top"] - saved_rect[1]) < 0.02

    v2 = make_viewer(state=state_path)
    rect2 = v2.visible_rect()
    ok_rect = all(abs(a - b) < 0.02 for a, b in zip(rect2, saved_rect))
    ok_markers = (len(v2.markers) == len(saved_markers)
                  and all(abs(a - c) < 0.02 and abs(b - d) < 0.02
                          for (a, b), (c, d) in zip(v2.markers, saved_markers)))
    check(11, "退出保存缩放/左上角/关注点, 再次启动恢复一致",
          ok_fields and ok_rect and ok_markers,
          "fields=%s rect_delta=%s markers=%s"
          % (ok_fields,
             tuple(round(b - a, 3) for a, b in zip(rect2, saved_rect)),
             ok_markers))


# ------------------------------------------------------------ 验收 12: 状态文件损坏
def test_12_corrupt_state():
    state_path = os.path.join(WORK, "bad_state.json")
    with open(state_path, "w", encoding="utf-8") as fh:
        fh.write("{ this is : not valid json ,,, ")
    v = make_viewer(state=state_path)
    ok = (v.state_warning and v.scale == 1.0
          and tv.WARNING_TEXT in v.status_text())

    # 字段缺失 / 标记越界同样回退默认
    state2 = os.path.join(WORK, "missing_fields.json")
    with open(state2, "w", encoding="utf-8") as fh:
        json.dump({"scale": 2.0}, fh)
    v2 = make_viewer(state=state2)
    ok2 = v2.state_warning and v2.scale == 1.0

    state3 = os.path.join(WORK, "bad_markers.json")
    with open(state3, "w", encoding="utf-8") as fh:
        json.dump({"scale": 1.0, "view_left": 0, "view_top": 0,
                   "markers": [[99999, -5]]}, fh)
    v3 = make_viewer(state=state3)
    ok3 = v3.state_warning and v3.markers == []
    check(12, "保存文件损坏/缺字段时默认视图启动并状态栏提示",
          ok and ok2 and ok3,
          "corrupt=%s missing=%s bad_markers=%s text=%r"
          % (ok, ok2, ok3, v.status_text()))


def main():
    pygame.init()
    os.makedirs(WORK, exist_ok=True)
    tests = [obj for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    for test in tests:
        test()

    width = max(len(t) for _, t, _, _ in RESULTS)
    print("=" * (width + 18))
    passed = 0
    for no, title, ok, detail in RESULTS:
        flag = "PASS" if ok else "FAIL"
        passed += ok
        print("验收%02d  %s  %s" % (no, flag, title))
        if not ok:
            print("        细节: %s" % detail)
    print("=" * (width + 18))
    print("结果: %d/%d 通过" % (passed, len(RESULTS)))
    pygame.quit()
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
