"""无第三方依赖的 PNG 画布（仅 Windows）。

实现方式：
- 通过 ctypes 调用 GDI 在 32 位顶向下 DIB 上绘制（支持中文，使用系统微软雅黑/黑体）；
- 色块填充与斜纹直接写像素缓冲，文字与直线走 GDI；
- PNG 文件用标准库 zlib/struct 手工编码（8 位 RGB）。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

try:
    import ctypes
    from ctypes import wintypes
except ImportError:  # pragma: no cover - 非 Windows 平台
    ctypes = None
    wintypes = None


# GDI 常量
_DIB_RGB_COLORS = 0
_BI_RGB = 0
_TRANSPARENT = 1
_PS_SOLID = 0
_DEFAULT_CHARSET = 1
_OUT_TT_PRECIS = 4
_CLIP_DEFAULT_PRECIS = 0
_CLEARTYPE_QUALITY = 5
_DEFAULT_PITCH = 0
_FF_DONTCARE = 0
_GDI_ERROR = 0xFFFFFFFF


class GdiUnsupportedError(RuntimeError):
    """当前平台不支持 GDI 画布时抛出。"""


class _BitmapInfoHeader(ctypes.Structure if ctypes else object):
    _fields_ = [
        ("biSize", wintypes.DWORD if wintypes else None),
        ("biWidth", wintypes.LONG if wintypes else None),
        ("biHeight", wintypes.LONG if wintypes else None),
        ("biPlanes", wintypes.WORD if wintypes else None),
        ("biBitCount", wintypes.WORD if wintypes else None),
        ("biCompression", wintypes.DWORD if wintypes else None),
        ("biSizeImage", wintypes.DWORD if wintypes else None),
        ("biXPelsPerMeter", wintypes.LONG if wintypes else None),
        ("biYPelsPerMeter", wintypes.LONG if wintypes else None),
        ("biClrUsed", wintypes.DWORD if wintypes else None),
        ("biClrImportant", wintypes.DWORD if wintypes else None),
    ]


class _BitmapInfo(ctypes.Structure if ctypes else object):
    _fields_ = [
        ("bmiHeader", _BitmapInfoHeader),
        ("bmiColors", wintypes.DWORD * 3 if wintypes else None),
    ]


class _Size(ctypes.Structure if ctypes else object):
    _fields_ = [
        ("cx", wintypes.LONG if wintypes else None),
        ("cy", wintypes.LONG if wintypes else None),
    ]


def _colorref(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


class GdiCanvas:
    """带内存 DC 的位图画布，坐标原点在左上角。"""

    def __init__(self, width: int, height: int, background: tuple[int, int, int]):
        if ctypes is None or not hasattr(ctypes, "windll"):
            raise GdiUnsupportedError(
                "PNG 绘制依赖 Windows GDI，当前平台不可用；请在 Windows 上运行本工具"
            )
        self.width, self.height = width, height
        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
        self._gdi32, self._user32 = gdi32, user32
        self._configure_prototypes(gdi32, user32)
        self._objects: list[int] = []

        screen_dc = user32.GetDC(0)
        self._hdc = gdi32.CreateCompatibleDC(screen_dc)
        user32.ReleaseDC(0, screen_dc)
        if not self._hdc:
            raise MemoryError("CreateCompatibleDC 失败")

        info = _BitmapInfo()
        hdr = info.bmiHeader
        hdr.biSize = ctypes.sizeof(_BitmapInfoHeader)
        hdr.biWidth = width
        hdr.biHeight = -height  # 负数 = 顶向下 DIB，原点在左上
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = _BI_RGB
        bits = ctypes.c_void_p()
        self._hbitmap = gdi32.CreateDIBSection(
            self._hdc, ctypes.byref(info), _DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        if not self._hbitmap:
            self._destroy()
            raise MemoryError("CreateDIBSection 失败")
        self._bits = bits.value
        self._stride = width * 4
        # 先初始化全部像素为背景色（BGRA）
        bg = bytes((background[2], background[1], background[0], 255))
        ctypes.memmove(self._bits, bg * (width * height), width * height * 4)

        self._old_bitmap = gdi32.SelectObject(self._hdc, self._hbitmap)
        self._font = None
        self.set_font(15)
        gdi32.SetBkMode(self._hdc, _TRANSPARENT)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _configure_prototypes(gdi32, user32) -> None:
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        user32.GetDC.restype = wintypes.HDC
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
        ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.SetBkMode.restype = ctypes.c_int
        gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.SetTextColor.restype = wintypes.COLORREF
        gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
        gdi32.CreatePen.restype = wintypes.HPEN
        gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
        gdi32.MoveToEx.restype = wintypes.BOOL
        gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        gdi32.LineTo.restype = wintypes.BOOL
        gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateFontW.restype = wintypes.HFONT
        gdi32.CreateFontW.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPCWSTR,
        ]
        gdi32.TextOutW.restype = wintypes.BOOL
        gdi32.TextOutW.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int,
        ]
        gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL
        gdi32.GetTextExtentPoint32W.argtypes = [
            wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(_Size),
        ]

    # ------------------------------------------------------------------ #
    # 像素级操作（直接写 DIB 缓冲，BGRA 顺序）
    # ------------------------------------------------------------------ #
    def _set_pixel(self, x: int, y: int, rgb: tuple[int, int, int]) -> None:
        offset = y * self._stride + x * 4
        ctypes.memmove(self._bits + offset, bytes((rgb[2], rgb[1], rgb[0], 255)), 4)

    def fill_rect(self, x: int, y: int, w: int, h: int, rgb: tuple[int, int, int]) -> None:
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + w)
        y1 = min(self.height, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        row = bytes((rgb[2], rgb[1], rgb[0], 255)) * (x1 - x0)
        for yy in range(y0, y1):
            ctypes.memmove(self._bits + yy * self._stride + x0 * 4, row, len(row))

    def stroke_rect(self, x: int, y: int, w: int, h: int,
                    rgb: tuple[int, int, int], thickness: int = 1) -> None:
        self.fill_rect(x, y, w, thickness, rgb)
        self.fill_rect(x, y + h - thickness, w, thickness, rgb)
        self.fill_rect(x, y, thickness, h, rgb)
        self.fill_rect(x + w - thickness, y, thickness, h, rgb)

    def hatch_rect(self, x: int, y: int, w: int, h: int,
                   rgb: tuple[int, int, int], step: int = 6) -> None:
        """45 度斜纹（色觉障碍/打印场景的第二编码）。"""
        for k in range(-h, w, step):
            for t in range(min(w, h) + 1):
                px, py = x + k + t, y + t
                if x <= px < x + w and y <= py < y + h:
                    self._set_pixel(px, py, rgb)

    def draw_line(self, x1: int, y1: int, x2: int, y2: int,
                  rgb: tuple[int, int, int], width: int = 1) -> None:
        pen = self._gdi32.CreatePen(_PS_SOLID, width, _colorref(rgb))
        old = self._gdi32.SelectObject(self._hdc, pen)
        self._gdi32.MoveToEx(self._hdc, x1, y1, None)
        self._gdi32.LineTo(self._hdc, x2, y2)
        self._gdi32.SelectObject(self._hdc, old)
        self._gdi32.DeleteObject(pen)

    # ------------------------------------------------------------------ #
    # 文字
    # ------------------------------------------------------------------ #
    def set_font(self, size_px: int, bold: bool = False, face: str | None = None) -> None:
        if face is None:
            face = "Microsoft YaHei"
        font = self._gdi32.CreateFontW(
            -size_px, 0, 0, 0, 700 if bold else 400,
            0, 0, 0, _DEFAULT_CHARSET, _OUT_TT_PRECIS, _CLIP_DEFAULT_PRECIS,
            _CLEARTYPE_QUALITY, _DEFAULT_PITCH | _FF_DONTCARE, face,
        )
        if not font:  # 字体缺失时回退系统默认字体
            font = self._gdi32.CreateFontW(
                -size_px, 0, 0, 0, 700 if bold else 400,
                0, 0, 0, _DEFAULT_CHARSET, _OUT_TT_PRECIS, _CLIP_DEFAULT_PRECIS,
                _CLEARTYPE_QUALITY, _DEFAULT_PITCH | _FF_DONTCARE, None,
            )
        old_obj = self._gdi32.SelectObject(self._hdc, font)
        if self._font is not None:
            # 上一支自定义字体已被换出，安全释放；首次进入时 old_obj 是系统字体，不释放
            self._gdi32.DeleteObject(self._font)
        self._font = font

    def text_size(self, text: str) -> tuple[int, int]:
        size = _Size()
        ok = self._gdi32.GetTextExtentPoint32W(self._hdc, text, len(text), ctypes.byref(size))
        if not ok:
            return (0, 0)
        return size.cx, size.cy

    def draw_text(self, x: int, y: int, text: str, rgb: tuple[int, int, int]) -> None:
        self._gdi32.SetTextColor(self._hdc, _colorref(rgb))
        self._gdi32.TextOutW(self._hdc, x, y, text, len(text))

    # ------------------------------------------------------------------ #
    # PNG 编码与资源释放
    # ------------------------------------------------------------------ #
    def save_png(self, path: str | Path) -> None:
        raw_buf = ctypes.string_at(self._bits, self.width * self.height * 4)
        raw = bytearray()
        src = 0
        for _y in range(self.height):
            raw.append(0)  # filter type 0
            row_start = len(raw)
            for _x in range(self.width):
                b, g, r, _a = raw_buf[src], raw_buf[src + 1], raw_buf[src + 2], raw_buf[src + 3]
                raw.extend((r, g, b))
                src += 4
            assert len(raw) - row_start == self.width * 3
        compressed = zlib.compress(bytes(raw), 9)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(
            ">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0,
        )  # 8-bit, color type 2 = truecolor RGB
        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", ihdr)
        png += chunk(b"IDAT", compressed)
        png += chunk(b"IEND", b"")
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(png)

    def _destroy(self) -> None:
        gdi32 = self._gdi32
        if self._hdc:
            if getattr(self, "_hbitmap", None) and getattr(self, "_old_bitmap", None):
                gdi32.SelectObject(self._hdc, self._old_bitmap)
            if getattr(self, "_font", None):
                gdi32.DeleteObject(self._font)
            if getattr(self, "_hbitmap", None):
                gdi32.DeleteObject(self._hbitmap)
            gdi32.DeleteDC(self._hdc)

    def close(self) -> None:
        self._destroy()

    def __enter__(self) -> "GdiCanvas":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
