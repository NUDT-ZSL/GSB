"""博物馆数字展陈动画本地工作台。

模块组成：
  enums       预定义枚举（动作类型、方向、缓动）
  parser      展陈脚本文本语义解析（纯关键词规则，无外部依赖）
  geometry    OBJ 读取、包围盒与材质分区
  constraints 展柜边界约束求解与幅度压缩
  easing      缓动函数与 Catmull-Rom 样条插值
  trajectory  模型轨迹关键帧生成
  camera      同步相机环绕关键帧与视锥校验
  smoothness  速度/加速度平滑检查与时间重采样修正
  verify      独立动作语义核对（仅从关键帧反算）
  pipeline    端到端流水线（确定性输出）
"""

from .pipeline import PipelineError, UnparsedAction, run_pipeline

__all__ = ["PipelineError", "UnparsedAction", "run_pipeline"]
