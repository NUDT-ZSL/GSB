"""本地确定性视频预测与运动时序渲染诊断工作台。

仅依赖 NumPy 与 Pillow，全部计算由固定种子派生，离线可复现。
"""

__all__ = ["errors", "hashing", "config", "samples", "embedding", "latents", "denoise", "scheduler", "snapshot", "pipeline", "report", "samplegen", "cli"]
__version__ = "1.0.0"
