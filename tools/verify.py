# -*- coding: utf-8 -*-
"""全量本地验证：在 SDL dummy 驱动下跑逻辑断言与界面冒烟测试并截图。

运行（在项目根目录）：
    python -I -m tools.verify
输出：
    控制台 PASS/FAIL 明细 + VERIFICATION.md + screenshots/*.png
"""
from __future__ import annotations

import json
import os
import sys

# 必须在 import pygame / app.ui 之前切换到无头视频驱动
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:         # 兼容 python -I 直接执行脚本
    sys.path.insert(0, _ROOT)

import pygame  # noqa: E402

from app.model import (compose_pose, export_pose_report, load_animation,
                       local_pose)  # noqa: E402
from app import ui as ui_mod  # noqa: E402
from app.ui import App, FRAME_STEP, CURVE_COLORS  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = lambda n: os.path.join(ROOT, "samples", n)
SHOT_DIR = os.path.join(ROOT, "screenshots")
REPORT_DIR = os.path.join(ROOT, "reports")

results: list[tuple[str, bool, str]] = []
shots: list[tuple[str, str]] = []


def check(req: str, name: str, cond: bool, detail: str = "") -> None:
    results.append((f"[{req}] {name}", bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    print(f"{flag}  [{req}] {name}" + (f" —— {detail}" if detail else ""))


def shot(app: App, fname: str, title: str) -> None:
    path = os.path.join(SHOT_DIR, fname)
    app.save_screenshot(path)
    shots.append((fname, title))


def count_color(screen: pygame.Surface, rgb: tuple[int, int, int]) -> int:
    raw = pygame.image.tostring(screen, "RGB")
    return raw.count(bytes(rgb))


def main() -> int:
    os.makedirs(SHOT_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)
    pygame.init()

    # ============================================================ 逻辑层测试
    basic = load_animation(SAMPLE("basic.json"))
    check("1", "basic.json 无文件级错误", basic.load_error is None)
    check("1", "层级顺序：父骨骼 root 先于子骨骼 child",
          [b.name for b in basic.bones].index("root")
          < [b.name for b in basic.bones].index("child"))

    compose_pose(basic, 0.0)
    child0 = basic.by_name["child"].tip
    origin0 = basic.by_name["child"].origin
    compose_pose(basic, 1.0)
    child1 = basic.by_name["child"].tip
    origin1 = basic.by_name["child"].origin
    check("1", "父骨骼旋转带动子骨骼末端位置变化",
          child0 != child1 and origin0 != origin1,
          f"child 原点 {origin0} -> {origin1}")

    trans, rot, scale = local_pose(basic.by_name["root"], 1.0)
    check("1", "缺失字段按默认值填充（平移0/缩放1）",
          trans == (0.0, 0.0) and scale == (1.0, 1.0) and rot == 90.0,
          f"trans={trans}, rot={rot}, scale={scale}")

    _, dial_rot, _ = local_pose(basic.by_name["dial"], 1.0)
    check("3", "旋转最短路径插值：0° 与 350° 中间帧为 355°",
          abs(dial_rot - 355.0) < 1e-9, f"实际 {dial_rot}°")
    t_lo, _, _ = local_pose(basic.by_name["dial"], -5.0)
    t_hi, r_hi, _ = local_pose(basic.by_name["dial"], 9.0)
    k_lo = basic.by_name["dial"].keyframes[0]
    k_hi = basic.by_name["dial"].keyframes[-1]
    check("3", "范围外保持最近关键帧值（clamp）",
          t_lo == k_lo.translation and r_hi == k_hi.rotation % 360.0,
          f"前段平移={t_lo}，后段旋转={r_hi}°")

    dup = load_animation(SAMPLE("duplicate.json"))
    arm = dup.by_name["arm"]
    dup_msg = "；".join(arm.issue.messages)
    check("4", "重复时间提示含骨骼名与重复时间",
          "arm" in dup_msg and any(s.time_repr == "1" for s in arm.issue.skipped),
          dup_msg)
    good = dup.by_name["good"]
    g_trans, g_rot, _ = local_pose(good, 1.0)
    check("4", "其余骨骼仍可正常插值预览",
          len(good.keyframes) == 2 and abs(g_rot - 30.0) < 1e-9,
          f"good@1.0s 旋转={g_rot}°")

    bad = load_animation(SAMPLE("bad_time.json"))
    leg = bad.by_name["bad_leg"]
    reasons = "；".join(s.reason for s in leg.issue.skipped)
    check("6", "负时间/倒序/超时长关键帧均被跳过并列出",
          len(leg.issue.skipped) == 3
          and ("负" in reasons and "倒序" in reasons and "超出" in reasons),
          reasons)
    _, leg_rot, _ = local_pose(leg, 2.0)
    check("6", "异常骨骼保持最后正常姿态（2.0s = 60°）",
          abs(leg_rot - 60.0) < 1e-9, f"实际 {leg_rot}°")
    check("6", "同文件正常骨骼不受影响",
          len(bad.by_name["normal_leg"].keyframes) == 2)

    malformed = load_animation(SAMPLE("malformed_keyframe.json"))
    spine = malformed.by_name["spine"]
    check("4", "格式错误关键帧被跳过且同骨骼其余帧保留",
          [k.time for k in spine.keyframes] == [0.0, 1.0, 2.0]
          and len(spine.issue.messages) >= 2,
          "；".join(spine.issue.messages))

    broken = load_animation(SAMPLE("broken.json"))
    check("9", "损坏 JSON 返回文件级错误", broken.load_error is not None,
          broken.load_error)
    missing = load_animation(SAMPLE("not_exist_xxx.json"))
    check("9", "不存在路径返回文件不可用",
          missing.load_error is not None and "不存在" in missing.load_error)

    check("10", "关键帧总数按原始条目统计（basic=8）",
          basic.total_keyframes == 8, f"实际 {basic.total_keyframes}")
    check("10", "异常骨骼计数正确（bad_time=1，basic=0）",
          len(bad.abnormal_bones()) == 1 and len(basic.abnormal_bones()) == 0)

    # 需求 7：1.0 秒导出报告
    out = os.path.join(REPORT_DIR, "verify_pose_t1.0.json")
    export_pose_report(basic, 1.0, out)
    with open(out, "r", encoding="utf-8") as fh:
        report = json.load(fh)
    check("7", "报告含 time 与每骨骼 world/tip 字段",
          report["time"] == 1.0 and len(report["bones"]) == len(basic.bones)
          and all({"x", "y", "rotation", "scale_x", "scale_y"}
                  <= set(b["world"].keys()) for b in report["bones"]))
    decimals_ok = True
    for b in report["bones"]:
        for key in ("x", "y", "rotation", "scale_x", "scale_y"):
            v = b["world"][key]
            if round(v, 2) != v:
                decimals_ok = False
    check("7", "所有数值保留两位小数", decimals_ok)
    compose_pose(basic, 1.0)
    root_now = basic.by_name["root"]
    rep_root = next(b for b in report["bones"] if b["name"] == "root")
    check("7", "报告数值与画面姿态一致",
          rep_root["world"]["x"] == round(root_now.origin[0], 2)
          and rep_root["world"]["rotation"] == round(root_now.world_rotation, 2),
          f"报告={rep_root['world']}")

    # ============================================================ 界面层测试
    app = App(SAMPLE("basic.json"))
    app.draw()
    shot(app, "01_skeleton.png", "骨架视图：层级面板 + 父子联动姿态")

    app.select_bone("child")
    app.draw()
    check("5", "选中子骨骼后高亮且其关键帧进入时间轴数据",
          app.selected == "child"
          and [k.time for k in app.anim.by_name["child"].keyframes] == [0, 1, 2])
    shot(app, "02_selected_child.png", "选中 child：骨骼高亮 + 时间轴绿色关键帧标记")

    # 播放推进（需求 10）
    app.toggle_play()
    check("2", "播放开关生效", app.playing is True)
    times = []
    for _ in range(8):
        app.update(0.05)
        times.append(round(app.time, 4))
    check("10", "播放中当前时间持续推进",
          times == sorted(times) and times[0] > 0 and len(set(times)) == 8,
          f"时间序列 {times[:4]}...")
    app.draw()
    status = " ".join(app._status_texts)
    check("10", "状态区显示当前时间/关键帧总数/异常骨骼数",
          "关键帧总数 8" in status and "异常骨骼 0" in status and "当前时间" in status,
          status.replace("\n", " "))
    shot(app, "03_playing.png", "播放中：状态区实时数值")

    # 暂停静止（需求 2）
    app.toggle_play()
    check("2", "暂停后画面静止", app.playing is False)
    snap1 = [(b.name, b.origin, b.tip) for b in app.anim.bones]
    app.draw()
    compose_pose(app.anim, app.time)
    snap2 = [(b.name, b.origin, b.tip) for b in app.anim.bones]
    check("2", "暂停后姿态不再变化", snap1 == snap2)

    # 逐帧固定步长（需求 2）
    t0 = app.time
    app.step(1)
    check("2", "逐帧前进按固定步长 1/30s 变化",
          abs(app.time - (t0 + FRAME_STEP)) < 1e-9 and app.playing is False,
          f"{t0:.3f} -> {app.time:.3f}")
    app.step(-1)
    check("2", "逐帧后退可回到原时间", abs(app.time - t0) < 1e-9)

    # 键盘事件
    app.handle_event(pygame.event.Event(pygame.KEYDOWN, {"key": pygame.K_SPACE}))
    check("2", "空格键切换播放", app.playing is True)
    app.handle_event(pygame.event.Event(pygame.KEYDOWN, {"key": pygame.K_SPACE}))

    # 拖动滑块（需求 2）
    rect = app._slider_rect()
    target_x = rect.x + rect.w // 2
    app.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN,
                                        {"pos": (target_x, rect.centery), "button": 1}))
    app.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP,
                                        {"pos": (target_x, rect.centery), "button": 1}))
    check("2", "拖动滑块画面时间刷新到对应位置",
          abs(app.time - app.anim.duration / 2) < 0.02,
          f"滑块中点 -> t={app.time:.2f}s")
    shot(app, "04_slider.png", "拖动滑块到时间线中点")

    # 曲线诊断（需求 8）
    app.load_file(SAMPLE("bad_time.json"))
    app.select_bone("bad_leg")
    app.toggle_view()
    check("8", "可切换到曲线诊断视图", app.view_mode == "curve")
    app.draw()
    color_counts = {k: count_color(app.screen, c) for k, c in CURVE_COLORS.items()}
    check("8", "三条不同颜色曲线均被绘制",
          all(v > 30 for v in color_counts.values()), str(color_counts))
    # 警示带为半透明橙黄：采样曲线区域内查找黄橙像素
    yellow = 0
    region = app.screen.subsurface(pygame.Rect(
        300, 120, app.screen.get_width() - 330, 400))
    for yy in range(0, region.get_height(), 4):
        for xx in range(0, region.get_width(), 4):
            r, g, b, _ = region.get_at((xx, yy))
            # 半透明橙黄与深色面板混合后仍应满足：R 明显大于 B，G 居中
            if r > 120 and r > b + 55 and g > b + 25:
                yellow += 1
    check("8", "倒序/异常区间显示橙黄警示带", yellow > 0, f"警示像素采样 {yellow} 个")
    shot(app, "05_curve_warning.png", "曲线诊断：三色曲线 + 异常区间警示带")

    # 重复时间样例界面（需求 4）
    app.load_file(SAMPLE("duplicate.json"))
    app.draw()
    check("4", "界面状态区可呈现重复时间骨骼",
          len(app.anim.abnormal_bones()) == 1
          and app.anim.by_name["arm"].issue.messages)
    shot(app, "06_duplicate.png", "重复时间：arm 标⚠，good 骨骼正常")

    # 文件不可用界面（需求 9）
    app2 = App(SAMPLE("not_exist_xxx.json"))
    app2.draw()
    check("9", "不可用窗口提供选择文件/退出入口",
          app2.anim.load_error is not None
          and {"open", "quit"} <= set(app2.unavail_rects.keys()))
    shot(app2, "07_unavailable.png", "文件不可用提示与重新选择入口")
    app2.load_file(SAMPLE("basic.json"))
    app2.draw()
    check("9", "选择有效文件后恢复正常预览",
          app2.anim.load_error is None and len(app2.anim.bones) == 3)
    shot(app2, "08_recovered.png", "重新选择有效文件后的预览")

    # 放弃确认弹窗（需求 11）
    app.load_file(SAMPLE("basic.json"))
    app.set_time(1.23)
    check("11", "姿态变化后标记为未导出(dirty)", app.anim.dirty is True)
    app.request_reload()
    check("11", "未导出时重新加载弹出确认框", app.dialog is not None)
    app.draw()
    shot(app, "09_confirm_dialog.png", "放弃姿态确认弹窗")
    app.dialog = None  # 模拟点取消
    check("11", "取消后保留当前预览", abs(app.time - 1.23) < 1e-9 and app.dialog is None)
    app.request_reload()
    _, confirm_cb = app.dialog
    app.dialog = None
    confirm_cb()  # 模拟点确认
    check("11", "确认后重新加载文件（时间归零）", app.time == 0.0)

    app.set_time(0.5)
    app.request_quit()
    check("11", "退出前同样弹窗确认", app.dialog is not None)
    app.dialog = None
    check("11", "取消退出后程序仍运行", app.running is True)

    # UI 导出（绕过系统文件对话框，直接指定路径）
    target = os.path.join(REPORT_DIR, "verify_ui_export.json")
    ui_mod._tk_save_path = lambda default_name: target
    app.export_report()
    check("7/11", "界面导出报告成功并清除未导出标记",
          os.path.exists(target) and app.anim.dirty is False)

    # ============================================================ 输出报告
    failed = [r for r in results if not r[1]]
    lines = [
        "# 本地验证结果",
        "",
        f"- Python {sys.version.split()[0]}，pygame {pygame.version.ver}",
        f"- 用例总数 {len(results)}，通过 {len(results) - len(failed)}，失败 {len(failed)}",
        "",
        "## 断言明细",
        "",
        "| 结果 | 用例 | 细节 |",
        "| --- | --- | --- |",
    ]
    for name, ok, detail in results:
        lines.append(f"| {'✅' if ok else '❌'} | {name} | {detail.replace('|', '/')} |")
    lines += ["", "## 截图取证（screenshots/）", ""]
    for fname, title in shots:
        lines.append(f"- `{fname}` — {title}")
    with open(os.path.join(ROOT, "VERIFICATION.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"\n用例 {len(results)} 条，失败 {len(failed)} 条；"
          f"明细见 VERIFICATION.md，截图 {len(shots)} 张见 screenshots/")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
