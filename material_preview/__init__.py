"""本地材质预览工具包。

读取场景 JSON，按光照方向离线渲染球体材质对照图（PNG）并产出核对 JSON。
所有数值均来自场景配置，渲染过程确定性、可重复。
"""

from .errors import ConfigError

__all__ = ["ConfigError"]
__version__ = "1.0.0"
