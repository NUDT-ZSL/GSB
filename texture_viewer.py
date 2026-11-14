# -*- coding: utf-8 -*-
"""
本地纹理浏览工具 (Local Texture Viewer)

功能:
  - 加载 PNG 纹理并在窗口中完整显示, 标题包含文件名与像素尺寸
  - 滚轮缩放 (1/8 ~ 8 倍), 缩放以鼠标位置为中心, 到达边界后视图不再变化
  - 左键拖拽平移, 视图不越出纹理边界 (纹理小于视口时居中, 余部填充背景)
  - 状态栏实时显示可见纹理坐标范围 (x1,y1,x2,y2) 与缩放倍数
  - G 键切换网格, 网格线按配置的纹理像素间距对齐
  - M 键在视图中心对应的纹理坐标添加红色关注点; 视图未变化时再按 M 移除最近标记
  - 关注点以纹理像素坐标保存, 缩放/平移后仍固定在同一纹理位置
  - 配置缺项/越界时回退默认值 (800x600, 1.0, 128)
  - 纹理损坏时窗口显示含文件名的错误说明并保持运行
  - 退出时保存视图状态 (缩放、视图左上角纹理坐标、关注点), 下次启动恢复
  - 保存文件损坏/缺字段时按默认视图启动并在状态栏提示

用法:
  python texture_viewer.py <texture.png> [--config config.json] [--state view_state.json]
"""

import argparse
import atexit
import json
import math
import os
import re
import sys

import pygame

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

# ---------------------------------------------------------------- 常量 / 默认值
MIN_SCALE = 1.0 / 8.0
MAX_SCALE = 8.0
DEFAULT_WINDOW_WIDTH = 800
DEFAULT_WINDOW_HEIGHT = 600
DEFAULT_INITIAL_SCALE = 1.0
DEFAULT_GRID_SPACING = 128
STATUS_H = 30
MAX_TEXTURE_SIZE = 4096
MARKER_HIT_TOLERANCE = 8.0          # 纹理像素: 视图中心命中已有标记的容差
ZOOM_STEP = 1.2                     # 每格滚轮的缩放倍率

BG_COLOR = (24, 26, 30)
STATUS_BG = (12, 13, 16)
STATUS_FG = (228, 231, 236)
GRID_COLOR = (0, 229, 255)
MARKER_COLOR = (235, 40, 40)
ERROR_COLOR = (255, 107, 107)
WARNING_TEXT = "已忽略无效视图状态"


# ---------------------------------------------------------------- 配置读取
def _finite_number(value):
    """返回有限数值, 否则 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _int_in_range(value, low, high):
    num = _finite_number(value)
    if num is None:
        return None
    if abs(num - round(num)) > 1e-9:
        return None  # 非整数浮点 (如 800.5) 不接受
    ivalue = int(round(num))
    if low <= ivalue <= high:
        return ivalue
    return None


def load_config(path):
    """读取 JSON 配置; 缺项/非法/文件不可读均回退默认值。"""
    cfg = {
        "window_width": DEFAULT_WINDOW_WIDTH,
        "window_height": DEFAULT_WINDOW_HEIGHT,
        "initial_scale": DEFAULT_INITIAL_SCALE,
        "grid_spacing": DEFAULT_GRID_SPACING,
    }
    data = None
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            data = None
    if not isinstance(data, dict):
        return cfg

    # 窗口尺寸: 支持 window_size: [w, h] 或 window_width/window_height
    win_w = win_h = None
    win_size = data.get("window_size")
    if isinstance(win_size, (list, tuple)) and len(win_size) == 2:
        win_w = _int_in_range(win_size[0], 320, 7680)
        win_h = _int_in_range(win_size[1], 240, 4320)
    if win_w is None:
        win_w = _int_in_range(data.get("window_width"), 320, 7680)
    if win_h is None:
        win_h = _int_in_range(data.get("window_height"), 240, 4320)
    if win_w is not None:
        cfg["window_width"] = win_w
    if win_h is not None:
        cfg["window_height"] = win_h

    scale = _finite_number(data.get("initial_scale"))
    if scale is not None and MIN_SCALE <= scale <= MAX_SCALE:
        cfg["initial_scale"] = scale

    spacing = _int_in_range(data.get("grid_spacing"), 1, 100000)
    if spacing is not None:
        cfg["grid_spacing"] = spacing

    return cfg


# ---------------------------------------------------------------- 纹理读取
def load_texture(path):
    """
    加载 PNG 纹理。
    成功返回 pygame.Surface; 失败抛出带文件名信息的 TextureLoadError。
    先用 Pillow 校验文件完整性, 再交给 pygame 解码。
    """
    filename = os.path.basename(path)

    def _short_reason(text):
        # PIL 异常消息里路径可能是双反斜杠转义形式, 直接把 "盘符:...文件名"
        # 这段路径 (含其中任意斜杠/反斜杠) 收敛为纯文件名, 避免错误屏过长
        message = str(text)
        pattern = r"[A-Za-z]:[^\w]*?.*?" + re.escape(filename)
        return re.sub(pattern, filename, message, flags=re.DOTALL)

    if not os.path.isfile(path):
        raise TextureLoadError(filename, "文件不存在")

    if Image is not None:
        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                im.load()
        except Exception as exc:  # PIL 的各类解码错误统一处理
            raise TextureLoadError(
                filename, "PNG 文件损坏或无法解码: %s" % _short_reason(exc))
    else:  # pragma: no cover - 依赖缺失时的兜底: 由 pygame 自行报错
        try:
            probe = pygame.image.load(path)
            width, height = probe.get_size()
        except Exception as exc:
            raise TextureLoadError(
                filename, "文件损坏或无法解码: %s" % _short_reason(exc))

    if Image is not None:
        with Image.open(path) as im:
            width, height = im.size

    if width <= 0 or height <= 0:
        raise TextureLoadError(filename, "图像尺寸非法 %dx%d" % (width, height))
    if width > MAX_TEXTURE_SIZE or height > MAX_TEXTURE_SIZE:
        raise TextureLoadError(
            filename,
            "纹理尺寸 %dx%d 超出 %dx%d 上限"
            % (width, height, MAX_TEXTURE_SIZE, MAX_TEXTURE_SIZE),
        )

    try:
        surface = pygame.image.load(path)
    except Exception as exc:
        raise TextureLoadError(filename, "pygame 解码失败: %s" % exc)
    if surface.get_width() != width or surface.get_height() != height:
        raise TextureLoadError(filename, "解码后尺寸与校验结果不一致")
    return surface.convert_alpha() if surface.get_flags() & pygame.SRCALPHA else surface.convert()


class TextureLoadError(Exception):
    def __init__(self, filename, reason):
        super().__init__("%s: %s" % (filename, reason))
        self.filename = filename
        self.reason = reason


# ---------------------------------------------------------------- 字体加载
def create_font(size=15):
    """
    字体工厂, 逐级兜底:
      1) 常见中文 TTF/OTF 文件直接加载 (可显示中文, 且绕开部分环境
         pygame.font.SysFont 枚举系统字体的缺陷);
      2) pygame.font.SysFont 按名称匹配;
      3) pygame 内置默认字体 (仅保证 ASCII)。
    """
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\simhei.ttf",    # 黑体
        r"C:\Windows\Fonts\simsun.ttc",    # 宋体
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return pygame.font.Font(path, size)
            except Exception:
                continue
    for name in ("microsoftyahei", "simhei", "notosanscjk",
                 "arialunicodems", "arial"):
        try:
            return pygame.font.SysFont(name, size)
        except Exception:
            continue
    return pygame.font.Font(None, int(size * 1.2))


# ---------------------------------------------------------------- 主视图类
class TextureViewer:
    def __init__(self, texture_path, config, state_path):
        self.texture_path = texture_path
        self.texture_name = os.path.basename(texture_path)
        self.config = config
        self.state_path = state_path

        self.window_w = int(config["window_width"])
        self.window_h = int(config["window_height"])
        self.grid_spacing = int(config["grid_spacing"])
        self.viewport_w = self.window_w
        self.viewport_h = self.window_h - STATUS_H

        self.screen = pygame.display.set_mode((self.window_w, self.window_h))
        pygame.display.set_caption("纹理浏览器 - %s" % self.texture_name)

        self.font = create_font(15)
        self.clock = pygame.time.Clock()

        # 纹理加载 (失败后进入错误模式, 窗口继续运行)
        self.texture = None
        self.tex_w = self.tex_h = 0
        self.load_error = None
        try:
            self.texture = load_texture(texture_path)
            self.tex_w, self.tex_h = self.texture.get_size()
            pygame.display.set_caption(
                "纹理浏览器 - %s (%dx%d)" % (self.texture_name, self.tex_w, self.tex_h)
            )
        except TextureLoadError as exc:
            self.load_error = exc
            pygame.display.set_caption(
                "纹理浏览器 - 加载失败 - %s" % self.texture_name
            )

        # 视图状态: scale 为缩放倍数; view_x/view_y 为视口中心对应的纹理像素坐标
        self.scale = float(config["initial_scale"])
        self.view_x = self.view_y = 0.0
        self.grid_enabled = True
        self.markers = []           # [[tex_x, tex_y], ...]
        self.state_warning = False
        self.running = True

        # 平移拖拽
        self.dragging = False
        self._last_mouse = (0, 0)

        # M 键状态机: 记录上次按 M 时的视图, 视图不变则再次按 M 执行移除
        self._last_m_view = None

        if self.texture is not None:
            self._clamp_view()
            self._restore_state()

    # ------------------------------------------------ 视图几何
    def visible_rect(self):
        """返回当前可见区域的纹理坐标 (x1, y1, x2, y2), 以纹理像素为单位。"""
        half_w = self.viewport_w / self.scale / 2.0
        half_h = self.viewport_h / self.scale / 2.0
        return (self.view_x - half_w, self.view_y - half_h,
                self.view_x + half_w, self.view_y + half_h)

    def _clamp_view(self):
        """约束视图中心, 使视口尽量不越出纹理; 纹理小于视口时该轴居中。"""
        max_x = max(0.0, self.tex_w - self.viewport_w / self.scale)
        max_y = max(0.0, self.tex_h - self.viewport_h / self.scale)
        # max 为 0 时中心固定在纹理中点 (居中显示)
        self.view_x = min(max(self.view_x, (self.tex_w - max_x) / 2.0),
                          (self.tex_w + max_x) / 2.0)
        self.view_y = min(max(self.view_y, (self.tex_h - max_y) / 2.0),
                          (self.tex_h + max_y) / 2.0)

    def zoom_at(self, mouse_x, mouse_y, direction):
        """以鼠标位置 (窗口坐标) 为中心缩放; direction>0 放大, <0 缩小。"""
        old_scale = self.scale
        factor = ZOOM_STEP if direction > 0 else 1.0 / ZOOM_STEP
        new_scale = min(max(old_scale * factor, MIN_SCALE), MAX_SCALE)
        if new_scale == old_scale:
            return  # 已达 1/8 或 8 倍边界: 视图保持不变
        # 保持鼠标下的纹理点不动
        tex_x = self.view_x + (mouse_x - self.viewport_w / 2.0) / old_scale
        tex_y = self.view_y + (mouse_y - self.viewport_h / 2.0) / old_scale
        self.scale = new_scale
        self.view_x = tex_x - (mouse_x - self.viewport_w / 2.0) / new_scale
        self.view_y = tex_y - (mouse_y - self.viewport_h / 2.0) / new_scale
        self._clamp_view()

    def pan_by_pixels(self, dx_px, dy_px):
        """按窗口像素拖拽量平移: 内容跟随鼠标, 向右拖则视图向右移动。"""
        self.view_x += dx_px / self.scale
        self.view_y += dy_px / self.scale
        self._clamp_view()

    def screen_to_texture(self, sx, sy):
        return (self.view_x + (sx - self.viewport_w / 2.0) / self.scale,
                self.view_y + (sy - self.viewport_h / 2.0) / self.scale)

    # ------------------------------------------------ 关注点
    def toggle_marker(self):
        """
        M 键:
          - 视图相对上次按 M 发生过缩放/平移: 在视图中心添加标记;
            若中心已落在已有标记容差内, 则移除该标记。
          - 视图未变化: 移除最近添加的关注点。
        """
        current_view = (self.view_x, self.view_y, self.scale)
        if self._last_m_view is not None and current_view == self._last_m_view:
            if self.markers:
                self.markers.pop()
        else:
            cx, cy = self.screen_to_texture(self.viewport_w / 2.0,
                                            self.viewport_h / 2.0)
            hit_idx = None
            best_dist = None
            for idx, (mx, my) in enumerate(self.markers):
                dist = math.hypot(mx - cx, my - cy)
                if dist <= MARKER_HIT_TOLERANCE and (best_dist is None or dist < best_dist):
                    best_dist = dist
                    hit_idx = idx
            if hit_idx is not None:
                self.markers.pop(hit_idx)
            else:
                self.markers.append([cx, cy])
        self._last_m_view = current_view

    def toggle_grid(self):
        self.grid_enabled = not self.grid_enabled

    # ------------------------------------------------ 状态持久化
    def save_state(self):
        if self.texture is None or not self.state_path:
            return
        x1, y1, _, _ = self.visible_rect()
        payload = {
            "scale": round(self.scale, 6),
            "view_left": round(x1, 4),    # 视图左上角对应的纹理 X 坐标
            "view_top": round(y1, 4),     # 视图左上角对应的纹理 Y 坐标
            "markers": [[round(x, 4), round(y, 4)] for x, y in self.markers],
        }
        try:
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass  # 状态保存失败不影响程序运行

    def _restore_state(self):
        """恢复上次视图; 文件损坏/字段缺失/越界则保持默认视图并提示。"""
        if not self.state_path or not os.path.isfile(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            self.state_warning = True
            return
        if not isinstance(data, dict):
            self.state_warning = True
            return

        scale = _finite_number(data.get("scale"))
        vleft = _finite_number(data.get("view_left"))
        vtop = _finite_number(data.get("view_top"))
        raw_markers = data.get("markers")
        if (scale is None or vleft is None or vtop is None
                or not MIN_SCALE <= scale <= MAX_SCALE
                or not (-self.tex_w <= vleft <= 2 * self.tex_w)
                or not (-self.tex_h <= vtop <= 2 * self.tex_h)
                or not isinstance(raw_markers, list)):
            self.state_warning = True
            return

        markers = []
        for point in raw_markers:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                self.state_warning = True
                return
            px, py = _finite_number(point[0]), _finite_number(point[1])
            if px is None or py is None:
                self.state_warning = True
                return
            if not (0.0 <= px <= self.tex_w and 0.0 <= py <= self.tex_h):
                self.state_warning = True
                return
            markers.append([px, py])

        # 由左上角还原视图中心
        self.scale = scale
        self.view_x = vleft + self.viewport_w / scale / 2.0
        self.view_y = vtop + self.viewport_h / scale / 2.0
        self.markers = markers
        self._clamp_view()

    # ------------------------------------------------ 事件
    def handle_event(self, event):
        if event.type == pygame.QUIT:
            self.running = False
            return
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                self.running = False
            elif event.key == pygame.K_g:
                self.toggle_grid()
            elif event.key == pygame.K_m:
                self.toggle_marker()
            return
        if self.texture is None:
            return  # 错误模式下不处理视图交互

        if event.type == pygame.MOUSEWHEEL:
            mx, my = pygame.mouse.get_pos()
            if my <= self.viewport_h:
                self.zoom_at(mx, my, event.y)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if event.pos[1] <= self.viewport_h:
                self.dragging = True
                self._last_mouse = event.pos
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self.dragging = False
        elif event.type == pygame.MOUSEMOTION and self.dragging:
            dx = event.pos[0] - self._last_mouse[0]
            dy = event.pos[1] - self._last_mouse[1]
            self._last_mouse = event.pos
            self.pan_by_pixels(dx, dy)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button in (4, 5):
            # 某些平台以 BUTTONDOWN 4/5 表达滚轮
            self.zoom_at(event.pos[0], event.pos[1],
                         1 if event.button == 4 else -1)

    # ------------------------------------------------ 绘制
    def draw(self):
        self.screen.fill(BG_COLOR)
        if self.texture is None:
            self._draw_error()
        else:
            self._draw_texture()
            if self.grid_enabled:
                self._draw_grid()
            self._draw_markers()
        self._draw_status_bar()
        pygame.display.flip()

    def _draw_texture(self):
        x1, y1, x2, y2 = self.visible_rect()
        # 与纹理区域求交, 得到需要采样的整数像素子矩形
        src_x1 = max(0, int(math.floor(x1)))
        src_y1 = max(0, int(math.floor(y1)))
        src_x2 = min(self.tex_w, int(math.ceil(x2)))
        src_y2 = min(self.tex_h, int(math.ceil(y2)))
        if src_x2 <= src_x1 or src_y2 <= src_y1:
            return  # 整屏都是纹理外区域, 保留背景色
        sub = self.texture.subsurface(
            pygame.Rect(src_x1, src_y1, src_x2 - src_x1, src_y2 - src_y1)
        )
        dst_w = max(1, int(round((src_x2 - src_x1) * self.scale)))
        dst_h = max(1, int(round((src_y2 - src_y1) * self.scale)))
        scaled = pygame.transform.smoothscale(sub, (dst_w, dst_h))
        dst_x = int(round((src_x1 - x1) * self.scale))
        dst_y = int(round((src_y1 - y1) * self.scale))
        self.screen.blit(scaled, (dst_x, dst_y))

    def _draw_grid(self):
        x1, y1, x2, y2 = self.visible_rect()
        spacing = self.grid_spacing
        first_x = max(0, math.ceil(x1 / spacing) * spacing)
        first_y = max(0, math.ceil(y1 / spacing) * spacing)
        gx = first_x
        while gx <= min(x2, self.tex_w):
            sx = int(round((gx - x1) * self.scale))
            pygame.draw.line(self.screen, GRID_COLOR,
                             (sx, 0), (sx, self.viewport_h - 1), 1)
            gx += spacing
        gy = first_y
        while gy <= min(y2, self.tex_h):
            sy = int(round((gy - y1) * self.scale))
            pygame.draw.line(self.screen, GRID_COLOR,
                             (0, sy), (self.viewport_w - 1, sy), 1)
            gy += spacing

    def _draw_markers(self):
        x1, y1, _, _ = self.visible_rect()
        radius = max(4, int(round(5 * min(1.0, self.scale))))
        for mx, my in self.markers:
            sx = int(round((mx - x1) * self.scale))
            sy = int(round((my - y1) * self.scale))
            if -radius <= sx < self.viewport_w and -radius <= sy < self.viewport_h:
                pygame.draw.circle(self.screen, MARKER_COLOR, (sx, sy), radius)
                pygame.draw.circle(self.screen, (255, 255, 255), (sx, sy), radius, 1)

    def _wrap_text(self, text, max_width):
        """按像素宽度对文本做简单换行 (优先在空格/标点处断行)。"""
        lines = []
        current = ""
        for ch in text:
            trial = current + ch
            if self.font.size(trial)[0] > max_width and current:
                lines.append(current)
                current = ch
            else:
                current = trial
        if current:
            lines.append(current)
        return lines or [""]

    def _draw_error(self):
        raw_lines = [
            "纹理加载失败 / Texture failed to load",
            "文件: %s" % self.texture_name,
            "原因: %s" % (self.load_error.reason if self.load_error else "未知错误"),
            "程序保持运行, 可关闭此窗口",
        ]
        max_width = self.window_w - 60
        lines = []
        for idx, raw in enumerate(raw_lines):
            wrapped = self._wrap_text(raw, max_width)
            for part in wrapped:
                lines.append((part, idx < 3))
        line_h = 26
        y = self.window_h // 2 - len(lines) * line_h // 2
        for idx, (line, is_error) in enumerate(lines):
            color = ERROR_COLOR if is_error else STATUS_FG
            surf = self.font.render(line, True, color)
            rect = surf.get_rect(center=(self.window_w // 2, y + idx * line_h))
            self.screen.blit(surf, rect)

    def status_text(self):
        """状态栏当前文案 (供测试与绘制共用)。"""
        if self.texture is not None:
            x1, y1, x2, y2 = self.visible_rect()
            text = ("x1=%.1f y1=%.1f x2=%.1f y2=%.1f | %.2fx | 网格:%s | 标记:%d"
                    % (x1, y1, x2, y2, self.scale,
                       "开" if self.grid_enabled else "关", len(self.markers)))
        else:
            text = "纹理加载失败: %s" % self.texture_name
        if self.state_warning:
            text = "[%s] %s" % (WARNING_TEXT, text)
        return text

    def _draw_status_bar(self):
        pygame.draw.rect(self.screen, STATUS_BG,
                         pygame.Rect(0, self.viewport_h, self.window_w, STATUS_H))
        surf = self.font.render(self.status_text(), True, STATUS_FG)
        self.screen.blit(surf, (8, self.viewport_h + 7))

    # ------------------------------------------------ 主循环
    def run(self):
        atexit.register(self.save_state)
        while self.running:
            for event in pygame.event.get():
                self.handle_event(event)
            self.draw()
            self.clock.tick(60)
        self.save_state()
        pygame.quit()


# ---------------------------------------------------------------- 入口
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="本地纹理浏览工具")
    parser.add_argument("texture", help="PNG 纹理文件路径 (不超过 4096x4096)")
    parser.add_argument("--config", help="JSON 配置 (窗口尺寸/初始缩放/网格间距)")
    parser.add_argument("--state", default="view_state.json",
                        help="视图状态 JSON 路径 (默认 view_state.json)")
    parser.add_argument("--smoke", action="store_true",
                        help="冒烟测试: 初始化并绘制一帧后退出")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    pygame.init()
    viewer = TextureViewer(args.texture, load_config(args.config), args.state)
    if args.smoke:
        viewer.draw()
        viewer.save_state()
        pygame.quit()
        return 0
    viewer.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
