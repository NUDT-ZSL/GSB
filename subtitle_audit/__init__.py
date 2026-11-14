"""对白镜头字幕校验工具（仅标准库）。

模块划分：
- errors:  清单级错误类型
- model:   清单读取、字段校验、归属/越界/重叠判定
- pngcanvas: 基于 Windows GDI(ctypes) 的无依赖 PNG 画布
- report:  CSV 明细与 timeline.png 对照图
- cli:     命令行入口
"""

__version__ = "1.0.0"
