# -*- coding: utf-8 -*-
"""Pygame 界面：骨架预览 / 时间线播放 / 曲线诊断 / 姿态报告导出。

界面布局（1180x760）：
┌──────────────────────────────────────────────┐
│ 顶栏：打开 重新加载 播放 逐帧 导出 视图切换      │
├────────────┬─────────────────────────────────┤
│ 骨骼层级    │  骨架视图 / 曲线诊断视图          │
│ 面板       │                                  │
│            ├─────────────────────────────────┤
│            │  时间线（滑块 + 关键帧标记）       │
├────────────┴─────────────────────────────────┤
│ 状态区：时间/关键帧总数/异常骨骼数/跳过帧明细    │
└──────────────────────────────────────────────┘
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import pygame

from .model import (Animation, Bone, compose_pose, export_pose_report,
                    load_animation, local_pose)

# ---------------------------------------------------------------- 外观常量

WINDOW_W, WINDOW_H = 1180, 760
TOP_H = 56
TREE_W = 230
STATUS_H = 96
TL_H = 92
CENTER_X0, CENTER_Y0 = TREE_W, TOP_H
CENTER_W = WINDOW_W - TREE_W
CENTER_H = WINDOW_H - TOP_H - STATUS_H - TL_H
TL_Y0 = TOP_H + CENTER_H
STATUS_Y0 = WINDOW_H - STATUS_H

FRAME_STEP = 1.0 / 30.0          # 逐帧按钮的固定步长（秒，需求 2）
PLAY_TIMER = 16                  # 播放定时器毫秒

C_BG = (24, 27, 34)
C_PANEL = (32, 36, 46)
C_PANEL2 = (40, 45, 57)
C_BORDER = (70, 78, 94)
C_TEXT = (226, 230, 238)
C_DIM = (150, 158, 174)
C_ACCENT = (96, 165, 250)
C_GOOD = (86, 200, 128)
C_WARN = (245, 178, 66)
C_BAD = (240, 96, 108)
C_BONE = (130, 170, 230)
C_SELECTED = (250, 214, 90)
C_TRACK = (60, 66, 82)
C_OVERLAY = (10, 12, 18)

CURVE_COLORS = {
    "translation_x": (96, 165, 250),    # 蓝：平移 X
    "rotation": (240, 96, 108),         # 红：旋转
    "scale_x": (86, 200, 128),          # 绿：缩放 X
}


@dataclass
class Button:
    rect: pygame.Rect
    label: str
    action: str
    kind: str = "normal"               # normal / primary / danger


class App:
    def __init__(self, path: Optional[str]):
        pygame.init()
        pygame.display.set_caption("2D 骨骼动画预览与诊断工具")
        self.screen = pygame.display.set_mode((WINDOW_W, WINDOW_H))
        self.clock = pygame.time.Clock()
        self.font = self._make_font(15)
        self.font_s = self._make_font(13)
        self.font_b = self._make_font(20, bold=True)
        self.font_m = self._make_font(17)

        self.running = True
        self.playing = False
        self.time = 0.0
        self.view_mode = "skeleton"          # skeleton / curve
        self.selected: Optional[str] = None
        self.dragging_slider = False
        self.dialog: Optional[Tuple[str, Callable[[], None]]] = None
        self.dialog_rects: Dict[str, pygame.Rect] = {}
        self.unavail_rects: Dict[str, pygame.Rect] = {}
        self.toast = ""
        self.toast_until = 0
        self._status_texts: List[str] = []
        self._bounds: Optional[Tuple[float, float, float, float]] = None

        self.buttons = self._build_buttons()
        self.anim: Animation = _failed_anim("尚未加载文件")
        if path:
            self.load_file(path)
        pygame.time.set_timer(pygame.USEREVENT + 1, PLAY_TIMER)

    # ------------------------------------------------------------ 初始化辅助
    @staticmethod
    def _make_font(size: int, bold: bool = False) -> pygame.font.Font:
        # 优先直接按文件名加载系统字体，绕开部分机器 pygame 2.6 注册表枚举崩溃
        candidates = [
            r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\simhei.ttf",    # 黑体
            r"C:\Windows\Fonts\simsun.ttc",    # 宋体
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    return pygame.font.Font(path, size)
                except Exception:
                    continue
        try:
            matched = pygame.font.match_font("arial")
            if matched:
                return pygame.font.Font(matched, size)
        except Exception:
            pass
        return pygame.font.Font(None, size + 3)

    def _build_buttons(self) -> List[Button]:
        specs = [
            ("打开文件", "open", "normal"),
            ("重新加载", "reload", "normal"),
            ("播放", "play", "primary"),
            ("|< 前一帧", "step_back", "normal"),
            ("后一帧 >|", "step_fwd", "normal"),
            ("导出报告", "export", "normal"),
            ("曲线诊断", "toggle_view", "normal"),
        ]
        buttons, x = [], 12
        for label, action, kind in specs:
            w = max(86, self.font.size(label)[0] + 24)
            buttons.append(Button(pygame.Rect(x, 12, w, 32), label, action, kind))
            x += w + 10
        return buttons

    # ------------------------------------------------------------ 文件加载
    def load_file(self, path: str) -> None:
        anim = load_animation(path)
        self.anim = anim
        self.time = 0.0
        self.playing = False
        self.view_mode = "skeleton"
        self.selected = anim.bones[0].name if anim.bones else None
        self._bounds = None if anim.load_error else self._compute_bounds(anim)
        if anim.load_error:
            self._status_texts = [anim.load_error]
        else:
            self._status_texts = [f"已加载 {os.path.basename(path)}，"
                                  f"共 {len(anim.bones)} 根骨骼"]
        compose_pose(anim, self.time)

    def _open_dialog(self) -> None:
        path = _tk_open_path()
        if path:
            self.load_file(path)

    def request_open(self) -> None:
        if self.anim.load_error is None and self.anim.dirty:
            self.dialog = ("姿态报告尚未导出，打开新文件将放弃当前姿态，是否继续？",
                           self._open_dialog)
        else:
            self._open_dialog()

    def request_reload(self) -> None:
        if not self.anim.file_path:
            self._open_dialog()
            return
        if self.anim.dirty:
            self.dialog = ("姿态报告尚未导出，重新加载将放弃当前姿态，是否继续？",
                           self._do_reload)
        else:
            self._do_reload()

    def _do_reload(self) -> None:
        if self.anim.file_path:
            self.load_file(self.anim.file_path)
            self._set_toast("已重新加载文件")

    def request_quit(self) -> None:
        if self.anim.load_error is None and self.anim.dirty:
            self.dialog = ("姿态报告尚未导出，退出将放弃当前姿态，是否继续？",
                           self._quit)
        else:
            self._quit()

    def _quit(self) -> None:
        self.running = False

    # ------------------------------------------------------------ 播放控制
    def toggle_play(self) -> None:
        if self.anim.load_error or not self.anim.bones:
            return
        if self.playing:
            self.playing = False
        else:
            if self.time >= self.anim.duration:
                self.set_time(0.0)
            self.playing = True

    def step(self, direction: int) -> None:
        if self.anim.load_error:
            return
        self.playing = False
        self.set_time(self.time + direction * FRAME_STEP)

    def set_time(self, value: float) -> None:
        t = max(0.0, min(self.anim.duration, float(value)))
        if abs(t - self.time) > 1e-9:
            self.time = t
            if self.anim.load_error is None:
                self.anim.dirty = True

    def select_bone(self, name: str) -> None:
        if name in self.anim.by_name:
            self.selected = name

    def toggle_view(self) -> None:
        if self.anim.load_error:
            return
        self.view_mode = "curve" if self.view_mode == "skeleton" else "skeleton"

    def export_report(self) -> None:
        if self.anim.load_error or not self.anim.bones:
            self._set_toast("当前无可用动画，无法导出")
            return
        default_name = f"pose_report_t{self.time:.2f}.json"
        out = _tk_save_path(default_name)
        if not out:
            out = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "reports", default_name)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        path = export_pose_report(self.anim, self.time, out)
        self.anim.dirty = False                # 报告已导出，清除未保存标记
        self._set_toast(f"姿态报告已导出：{path}")

    def _set_toast(self, text: str) -> None:
        self.toast = text
        self.toast_until = pygame.time.get_ticks() + 4000

    # ------------------------------------------------------------ 主循环
    def run(self) -> None:
        while self.running:
            dt = self.clock.tick(60) / 1000.0
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.request_quit()
                elif event.type == pygame.USEREVENT + 1 and self.playing:
                    self.update(dt)
                else:
                    self.handle_event(event)
            self.draw()
        pygame.quit()

    def update(self, dt: float) -> None:
        """推进播放时间；到达总时长自动暂停。"""
        if self.anim.load_error:
            self.playing = False
            return
        nxt = self.time + dt
        if nxt >= self.anim.duration:
            self.set_time(self.anim.duration)
            self.playing = False
        else:
            self.set_time(nxt)

    # ------------------------------------------------------------ 事件处理
    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.KEYDOWN:
            self._on_key(event)
            return
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._on_mouse_down(event.pos)
        elif event.type == pygame.MOUSEMOTION and self.dragging_slider:
            self._seek_to_x(event.pos[0])
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self.dragging_slider = False

    def _on_key(self, event: pygame.event.Event) -> None:
        if self.dialog:
            if event.key == pygame.K_ESCAPE:
                self.dialog = None
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                _, confirm = self.dialog
                self.dialog = None
                confirm()
            return
        if event.key == pygame.K_SPACE:
            self.toggle_play()
        elif event.key == pygame.K_LEFT:
            self.step(-1)
        elif event.key == pygame.K_RIGHT:
            self.step(1)
        elif event.key == pygame.K_r:
            self.request_reload()
        elif event.key == pygame.K_e:
            self.export_report()
        elif event.key == pygame.K_c:
            self.toggle_view()
        elif event.key in (pygame.K_UP, pygame.K_DOWN) and self.anim.bones:
            names = [b.name for b in self.anim.bones]
            idx = names.index(self.selected) if self.selected in names else 0
            idx = (idx + (1 if event.key == pygame.K_DOWN else -1)) % len(names)
            self.select_bone(names[idx])
        elif event.key == pygame.K_ESCAPE:
            self.request_quit()

    def _on_mouse_down(self, pos: Tuple[int, int]) -> None:
        if self.dialog:
            ok = self.dialog_rects.get("ok")
            cancel = self.dialog_rects.get("cancel")
            if ok and ok.collidepoint(pos):
                _, confirm = self.dialog
                self.dialog = None
                confirm()
            elif cancel and cancel.collidepoint(pos):
                self.dialog = None
            return
        if self.anim.load_error:
            for key, rect in self.unavail_rects.items():
                if rect.collidepoint(pos):
                    self._open_dialog() if key == "open" else self._quit()
            return
        for btn in self.buttons:
            if btn.rect.collidepoint(pos):
                self._dispatch(btn.action)
                return
        if self._tree_rect().collidepoint(pos):
            self._handle_tree_click(pos)
            return
        if self._slider_rect().inflate(0, 18).collidepoint(pos):
            self.dragging_slider = True
            self._seek_to_x(pos[0])

    def _dispatch(self, action: str) -> None:
        {
            "open": self.request_open,
            "reload": self.request_reload,
            "play": self.toggle_play,
            "step_back": lambda: self.step(-1),
            "step_fwd": lambda: self.step(1),
            "export": self.export_report,
            "toggle_view": self.toggle_view,
        }[action]()

    def _tree_rect(self) -> pygame.Rect:
        return pygame.Rect(0, TOP_H, TREE_W, CENTER_H + TL_H)

    def _slider_rect(self) -> pygame.Rect:
        return pygame.Rect(TREE_W + 18, TL_Y0 + 40, WINDOW_W - TREE_W - 36, 6)

    def _seek_to_x(self, mouse_x: int) -> None:
        rect = self._slider_rect()
        if self.anim.duration <= 0:
            self.set_time(0.0)
            return
        u = (mouse_x - rect.x) / rect.w
        self.set_time(max(0.0, min(1.0, u)) * self.anim.duration)

    def _handle_tree_click(self, pos: Tuple[int, int]) -> None:
        y = TOP_H + 12
        for bone in self.anim.bones:
            depth = self._bone_depth(bone)
            row = pygame.Rect(8, y, TREE_W - 16, 26)
            if row.collidepoint(pos):
                self.select_bone(bone.name)
                return
            y += 28

    def _bone_depth(self, bone: Bone) -> int:
        depth, cur = 0, bone
        seen = set()
        while cur.parent and cur.parent in self.anim.by_name and cur.name not in seen:
            seen.add(cur.name)
            cur = self.anim.by_name[cur.parent]
            depth += 1
        return depth

    # ------------------------------------------------------------ 绘制入口
    def draw(self) -> None:
        self.screen.fill(C_BG)
        self._draw_topbar()
        if self.anim.load_error:
            self._draw_unavailable()
        else:
            compose_pose(self.anim, self.time)
            self._draw_tree()
            if self.view_mode == "skeleton":
                self._draw_skeleton()
            else:
                self._draw_curves()
            self._draw_timeline()
            self._draw_status()
        self._draw_dialog()
        if self.toast and pygame.time.get_ticks() < self.toast_until:
            self._draw_toast()
        pygame.display.flip()

    def _text(self, s: str, x: int, y: int, color=C_TEXT,
              font: Optional[pygame.font.Font] = None,
              center: bool = False) -> pygame.Rect:
        font = font or self.font
        surf = font.render(s, True, color)
        rect = surf.get_rect()
        if center:
            rect.center = (x, y)
        else:
            rect.topleft = (x, y)
        self.screen.blit(surf, rect)
        return rect

    def _draw_topbar(self) -> None:
        pygame.draw.rect(self.screen, C_PANEL, (0, 0, WINDOW_W, TOP_H))
        pygame.draw.line(self.screen, C_BORDER, (0, TOP_H - 1),
                         (WINDOW_W, TOP_H - 1))
        for btn in self.buttons:
            label = btn.label
            if btn.action == "play":
                label = "暂停" if self.playing else "播放"
            if btn.action == "toggle_view":
                label = "骨架视图" if self.view_mode == "curve" else "曲线诊断"
            hover = btn.rect.collidepoint(pygame.mouse.get_pos())
            if btn.kind == "primary":
                base = C_ACCENT if not hover else (130, 190, 255)
                txt = (18, 22, 30)
            else:
                base = C_PANEL2 if not hover else (54, 61, 77)
                txt = C_TEXT
            pygame.draw.rect(self.screen, base, btn.rect, border_radius=6)
            pygame.draw.rect(self.screen, C_BORDER, btn.rect, 1, border_radius=6)
            self._text(label, btn.rect.centerx, btn.rect.centery, txt,
                       center=True)

    # ------------------------------------------------------------ 层级面板
    def _draw_tree(self) -> None:
        rect = self._tree_rect()
        pygame.draw.rect(self.screen, C_PANEL, rect)
        pygame.draw.line(self.screen, C_BORDER, (TREE_W - 1, rect.top),
                         (TREE_W - 1, rect.bottom))
        self._text("骨骼层级", 12, TOP_H + 10, C_DIM, self.font_s)
        y = TOP_H + 34
        for bone in self.anim.bones:
            depth = self._bone_depth(bone)
            row = pygame.Rect(8, y, TREE_W - 16, 26)
            if bone.name == self.selected:
                pygame.draw.rect(self.screen, (58, 72, 98), row, border_radius=5)
                pygame.draw.rect(self.screen, C_ACCENT,
                                 (row.x, row.y, 3, row.h), border_radius=2)
            prefix = ("    " * depth) + ("|- " if depth else "* ")
            color = C_BAD if bone.issue.messages else C_TEXT
            label = prefix + bone.name
            if bone.issue.messages:
                label += "  [!]"
            self._text(label, row.x + 8, row.centery - 8, color, self.font_s)
            y += 28

    # ------------------------------------------------------------ 骨架视图
    @staticmethod
    def _compute_bounds(anim: Animation) -> Tuple[float, float, float, float]:
        """采样整条时间线，得到所有骨骼关节点的包围盒，避免播放时画面抖动。"""
        xs, ys = [], []
        n = max(20, min(120, anim.total_keyframes * 8))
        for i in range(n + 1):
            t = anim.duration * i / n
            compose_pose(anim, t)
            for b in anim.bones:
                xs += [b.origin[0], b.tip[0]]
                ys += [b.origin[1], b.tip[1]]
        return min(xs), min(ys), max(xs), max(ys)

    def _view_transform(self) -> Callable[[Tuple[float, float]], Tuple[int, int]]:
        x0, y0, x1, y1 = self._bounds or (0.0, 0.0, 1.0, 1.0)
        w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
        area = pygame.Rect(CENTER_X0 + 24, CENTER_Y0 + 24,
                           CENTER_W - 48, CENTER_H - 48)
        scale = min(area.w / w, area.h / h)
        ox = area.centerx - (x0 + w / 2) * scale
        oy = area.centery - (y0 + h / 2) * scale

        def to_screen(p: Tuple[float, float]) -> Tuple[int, int]:
            return int(p[0] * scale + ox), int(p[1] * scale + oy)
        return to_screen

    def _draw_skeleton(self) -> None:
        self._center_frame("骨架预览")
        to_screen = self._view_transform()
        # 先画父子连线（骨骼本体）
        for bone in self.anim.bones:
            p0, p1 = to_screen(bone.origin), to_screen(bone.tip)
            selected = bone.name == self.selected
            width = 9 if selected else 5
            color = C_SELECTED if selected else C_BONE
            pygame.draw.line(self.screen, color, p0, p1, width)
            if selected:  # 高亮外发光
                pygame.draw.line(self.screen, (250, 214, 90, 70), p0, p1, 15)
        # 再画关节与名称
        for bone in self.anim.bones:
            p0, p1 = to_screen(bone.origin), to_screen(bone.tip)
            selected = bone.name == self.selected
            pygame.draw.circle(self.screen, C_TEXT, p0, 5 if selected else 3)
            pygame.draw.circle(self.screen, C_PANEL, p0, 2)
            mid = ((p0[0] + p1[0]) // 2, (p0[1] + p1[1]) // 2)
            label_surf = self.font_s.render(bone.name, True,
                                            (255, 236, 150) if selected else C_DIM)
            label_rect = label_surf.get_rect(midbottom=(mid[0] + 6, mid[1] - 10))
            if selected:
                bg = label_rect.inflate(10, 6)
                pygame.draw.rect(self.screen, (30, 33, 42), bg, border_radius=4)
            self.screen.blit(label_surf, label_rect)
        self._text(f"t = {self.time:.2f} s", CENTER_X0 + 18, CENTER_Y0 + CENTER_H - 28,
                   C_DIM, self.font_s)

    def _center_frame(self, title: str) -> pygame.Rect:
        rect = pygame.Rect(CENTER_X0, CENTER_Y0, CENTER_W, CENTER_H)
        pygame.draw.rect(self.screen, C_PANEL2, rect)
        pygame.draw.rect(self.screen, C_BORDER, rect, 1)
        self._text(title, CENTER_X0 + 14, CENTER_Y0 + 10, C_DIM, self.font_s)
        return rect

    # ------------------------------------------------------------ 曲线诊断
    def _draw_curves(self) -> None:
        self._center_frame("曲线诊断 (蓝=平移X  红=旋转  绿=缩放X)")
        bone = self.anim.selected_or_first(self.selected)
        if bone is None:
            return
        pad_l, pad_r = 70, 24
        top = CENTER_Y0 + 44
        plot_h = (CENTER_H - 64) // 3
        plot_w = CENTER_W - pad_l - pad_r - 10
        channels = [
            ("translation_x", lambda lp: lp[0][0], "平移 X (px)"),
            ("rotation", lambda lp: lp[1], "旋转 (°)"),
            ("scale_x", lambda lp: lp[2][0], "缩放 X"),
        ]
        n = 200
        for row, (key, getter, label) in enumerate(channels):
            px = CENTER_X0 + pad_l
            py = top + row * plot_h
            rect = pygame.Rect(px, py, plot_w, plot_h - 12)
            pygame.draw.rect(self.screen, C_PANEL, rect, border_radius=4)
            samples = []
            values = []
            for i in range(n + 1):
                t = self.anim.duration * i / n
                lp = local_pose(bone, t)
                v = getter(lp)
                samples.append((t, v))
                values.append(v)
            vmin, vmax = min(values), max(values)
            if math.isclose(vmin, vmax):
                vmin -= 1.0
                vmax += 1.0
            pad = (vmax - vmin) * 0.12
            vmin, vmax = vmin - pad, vmax + pad

            def x_of(t: float) -> float:
                if self.anim.duration <= 0:
                    return rect.x
                return rect.x + max(0.0, min(1.0, t / self.anim.duration)) * rect.w

            def y_of(v: float) -> float:
                return rect.bottom - (v - vmin) / (vmax - vmin) * rect.h

            # 网格 + 纵轴标签（首行刻度让位，通道名画在子图左侧居中）
            for g in range(4):
                gy = rect.y + rect.h * g / 3
                pygame.draw.line(self.screen, (52, 58, 72),
                                 (rect.x, gy), (rect.right, gy), 1)
                gv = vmax - (vmax - vmin) * g / 3
                if g > 0:
                    self._text(f"{gv:.1f}", rect.x - 48, gy - 8, C_DIM, self.font_s)
            self._text(label, CENTER_X0 + 8, rect.centery - 8,
                       CURVE_COLORS[key], self.font_s)
            # 插值异常警示带（需求 8）
            self._draw_warning_bands(bone, rect, x_of)
            # 曲线本体
            pts = [(x_of(t), y_of(v)) for t, v in samples]
            pygame.draw.lines(self.screen, CURVE_COLORS[key], False, pts, 2)
            # 正常关键帧锚点
            for kf in bone.keyframes:
                pygame.draw.circle(self.screen, CURVE_COLORS[key],
                                   (int(x_of(kf.time)), int(y_of(getter(
                                       local_pose(bone, kf.time))))), 3)
        # 异常原因汇总
        reasons = [f"{sk.time_repr}s: {sk.reason}" for sk in bone.issue.skipped]
        if reasons:
            self._text("警示带 -> " + "；".join(reasons[:3]),
                       CENTER_X0 + 14, CENTER_Y0 + CENTER_H - 16, C_WARN, self.font_s)

    def _draw_warning_bands(self, bone: Bone, rect: pygame.Rect,
                            x_of: Callable[[float], float]) -> None:
        band = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
        for sk in bone.issue.skipped:
            t = sk.raw_time
            if t < 0:                                   # 负时间：左边缘固定窄带
                rx0, rx1 = 0, 14
            elif t > self.anim.duration:                # 超总时长：右边缘固定窄带
                rx0, rx1 = rect.w - 14, rect.w
            else:
                # 倒序/重复：从上一个正常关键帧到异常时间点画警示带
                prev_t = 0.0
                for kf in bone.keyframes:
                    if kf.time <= t:
                        prev_t = kf.time
                if self.anim.duration <= 0:
                    rx0, rx1 = 0, rect.w
                else:
                    rx0 = int(prev_t / self.anim.duration * rect.w)
                    rx1 = int(t / self.anim.duration * rect.w)
                if rx1 <= rx0:
                    rx0, rx1 = max(0, rx1 - 10), min(rect.w, rx1 + 4)
            rx0 = max(0, min(rect.w, rx0))
            rx1 = max(0, min(rect.w, rx1))
            if rx1 <= rx0:
                continue
            pygame.draw.rect(band, (245, 178, 66, 110),
                             (rx0, 0, rx1 - rx0, rect.h))
            for hx in range(rx0, rx1, 8):               # 斜纹
                pygame.draw.line(band, (250, 190, 90, 210),
                                 (hx, rect.h), (hx + 10, 0), 1)
        self.screen.blit(band, rect.topleft)

    # ------------------------------------------------------------ 时间线
    def _draw_timeline(self) -> None:
        rect = pygame.Rect(TREE_W, TL_Y0, CENTER_W, TL_H)
        pygame.draw.rect(self.screen, C_PANEL, rect)
        self._text("时间线（拖动滑块定位，标记为选中骨骼的关键帧）",
                   rect.x + 14, rect.y + 8, C_DIM, self.font_s)
        track = self._slider_rect()
        pygame.draw.rect(self.screen, C_TRACK, track, border_radius=3)
        dur = max(self.anim.duration, 1e-9)
        # 刻度
        tick_step = 0.5 if dur <= 4 else 1.0
        t = 0.0
        while t <= dur + 1e-9:
            tx = int(track.x + t / dur * track.w)
            pygame.draw.line(self.screen, C_BORDER, (tx, track.bottom - 2),
                             (tx, track.bottom + 6), 1)
            self._text(f"{t:g}s", tx - 8, track.bottom + 8, C_DIM, self.font_s)
            t += tick_step
        # 选中骨骼的关键帧标记（需求 5）
        bone = self.anim.selected_or_first(self.selected)
        if bone:
            for kf in bone.keyframes:
                mx = int(track.x + kf.time / dur * track.w)
                pygame.draw.polygon(self.screen, C_GOOD,
                                    [(mx, track.y - 12), (mx - 5, track.y - 4),
                                     (mx + 5, track.y - 4)])
            for sk in bone.issue.skipped:
                u = max(0.0, min(1.0, sk.raw_time / dur))
                mx = int(track.x + u * track.w)
                pygame.draw.polygon(self.screen, C_BAD,
                                    [(mx, track.y - 12), (mx - 5, track.y - 4),
                                     (mx + 5, track.y - 4)])
        # 当前时间手柄
        hx = int(track.x + self.time / dur * track.w)
        pygame.draw.circle(self.screen, C_ACCENT, (hx, track.centery), 8)
        pygame.draw.circle(self.screen, C_TEXT, (hx, track.centery), 3)

    # ------------------------------------------------------------ 状态区
    def _draw_status(self) -> None:
        rect = pygame.Rect(0, STATUS_Y0, WINDOW_W, STATUS_H)
        pygame.draw.rect(self.screen, C_PANEL, rect)
        pygame.draw.line(self.screen, C_BORDER, (0, STATUS_Y0),
                         (WINDOW_W, STATUS_Y0))
        abnormal = self.anim.abnormal_bones()
        lines = [
            f"当前时间 {self.time:.2f}s / {self.anim.duration:.2f}s"
            f"    关键帧总数 {self.anim.total_keyframes}"
            f"    异常骨骼 {len(abnormal)} / {len(self.anim.bones)}",
        ]
        if self.selected and self.selected in self.anim.by_name:
            sel = self.anim.by_name[self.selected]
            lines.append(f"选中骨骼：{sel.name}（正常关键帧 {len(sel.keyframes)}，"
                         f"跳过 {len(sel.issue.skipped)}）")
            for sk in sel.issue.skipped[:2]:
                lines.append(f"  已跳过关键帧：{sk.time_repr}s —— {sk.reason}")
            for msg in sel.issue.messages[:2]:
                if "已跳过" not in msg:
                    lines.append("  " + msg)
        self._status_texts = lines
        y = STATUS_Y0 + 10
        for i, line in enumerate(lines[:4]):
            color = C_WARN if i > 0 else C_TEXT
            self._text(line, 14, y, color, self.font_s)
            y += 20

    # ------------------------------------------------------------ 弹窗 / 不可用
    def _draw_unavailable(self) -> None:
        box = pygame.Rect(0, 0, 560, 260)
        box.center = (WINDOW_W // 2, WINDOW_H // 2)
        pygame.draw.rect(self.screen, C_PANEL, box, border_radius=10)
        pygame.draw.rect(self.screen, C_BAD, box, 2, border_radius=10)
        self._text("! 文件不可用", box.centerx, box.y + 44, C_BAD,
                   self.font_b, center=True)
        msg = self.anim.load_error or "未知错误"
        # 长路径/长错误信息按宽度自动换行，避免硬截断
        self._wrap_center(msg, box.centerx, box.y + 96, box.w - 60, C_TEXT)
        self._text("请重新选择有效的动画 JSON 文件", box.centerx, box.y + 158,
                   C_DIM, self.font_s, center=True)
        btn_open = pygame.Rect(box.centerx - 150, box.y + 180, 140, 40)
        btn_quit = pygame.Rect(box.centerx + 10, box.y + 180, 140, 40)
        for rect, label, kind in ((btn_open, "选择文件", "primary"),
                                  (btn_quit, "退出", "normal")):
            base = C_ACCENT if kind == "primary" else C_PANEL2
            pygame.draw.rect(self.screen, base, rect, border_radius=6)
            pygame.draw.rect(self.screen, C_BORDER, rect, 1, border_radius=6)
            self._text(label, rect.centerx, rect.centery,
                      (18, 22, 30) if kind == "primary" else C_TEXT,
                      center=True)
        self.unavail_rects = {"open": btn_open, "quit": btn_quit}

    def _draw_dialog(self) -> None:
        if not self.dialog:
            self.dialog_rects = {}
            return
        overlay = pygame.Surface((WINDOW_W, WINDOW_H), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 150))
        self.screen.blit(overlay, (0, 0))
        box = pygame.Rect(0, 0, 480, 210)
        box.center = (WINDOW_W // 2, WINDOW_H // 2)
        pygame.draw.rect(self.screen, C_PANEL, box, border_radius=10)
        pygame.draw.rect(self.screen, C_WARN, box, 2, border_radius=10)
        self._text("确认操作", box.centerx, box.y + 38, C_WARN,
                   self.font_m, center=True)
        text, _ = self.dialog
        self._wrap_text(text, box.x + 30, box.y + 78, box.w - 60, C_TEXT)
        ok = pygame.Rect(box.centerx - 140, box.bottom - 58, 120, 38)
        cancel = pygame.Rect(box.centerx + 20, box.bottom - 58, 120, 38)
        for rect, label, kind in ((ok, "确认", "primary"), (cancel, "取消", "normal")):
            base = C_ACCENT if kind == "primary" else C_PANEL2
            pygame.draw.rect(self.screen, base, rect, border_radius=6)
            pygame.draw.rect(self.screen, C_BORDER, rect, 1, border_radius=6)
            self._text(label, rect.centerx, rect.centery,
                      (18, 22, 30) if kind == "primary" else C_TEXT,
                      center=True)
        self.dialog_rects = {"ok": ok, "cancel": cancel}

    def _wrap_text(self, text: str, x: int, y: int, width: int,
                   color: Tuple[int, int, int]) -> None:
        line = ""
        for ch in text:
            if self.font.size(line + ch)[0] > width:
                self._text(line, x, y, color)
                line = ch
                y += 24
            else:
                line += ch
        if line:
            self._text(line, x, y, color)

    def _wrap_center(self, text: str, cx: int, y: int, width: int,
                     color: Tuple[int, int, int]) -> None:
        """按宽度折行并逐行居中渲染。"""
        line = ""
        for ch in text:
            if self.font_m.size(line + ch)[0] > width:
                self._text(line, cx, y, color, self.font_m, center=True)
                line = ch
                y += 24
            else:
                line += ch
        if line:
            self._text(line, cx, y, color, self.font_m, center=True)

    def _draw_toast(self) -> None:
        surf = self.font_s.render(self.toast, True, C_TEXT)
        rect = surf.get_rect(center=(WINDOW_W // 2, WINDOW_H - STATUS_H - 16))
        bg = rect.inflate(24, 12)
        pygame.draw.rect(self.screen, (48, 54, 68), bg, border_radius=8)
        pygame.draw.rect(self.screen, C_BORDER, bg, 1, border_radius=8)
        self.screen.blit(surf, rect)

    def save_screenshot(self, path: str) -> str:
        pygame.image.save(self.screen, path)
        return os.path.abspath(path)


# ---------------------------------------------------------------- 模块级辅助

def _failed_anim(message: str) -> Animation:
    from .model import _failed
    return _failed(message)


def _tk_open_path() -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="选择动画 JSON 文件",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        root.destroy()
        return path or None
    except Exception:
        return None


def _tk_save_path(default_name: str) -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.asksaveasfilename(
            title="导出姿态报告",
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json")])
        root.destroy()
        return path or None
    except Exception:
        return None
