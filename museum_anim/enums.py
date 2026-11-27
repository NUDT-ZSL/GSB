"""预定义枚举值。方向与缓动类型只能取下列枚举，解析与核对均以此为准。"""

from enum import Enum


class ActionType(str, Enum):
    ROTATE = "ROTATE"        # 单向旋转
    SWING = "SWING"          # 绕轴来回摆动
    TRANSLATE = "TRANSLATE"  # 沿轴平移
    EXPAND = "EXPAND"        # 按材质分区沿法向外扩


class Direction(str, Enum):
    ROT_X = "ROT_X"
    ROT_Y = "ROT_Y"
    ROT_Z = "ROT_Z"
    SWING_X = "SWING_X"
    SWING_Y = "SWING_Y"
    SWING_Z = "SWING_Z"
    MOVE_X = "MOVE_X"
    MOVE_Y = "MOVE_Y"
    MOVE_Z = "MOVE_Z"
    EXPAND = "EXPAND"


class Easing(str, Enum):
    LINEAR = "LINEAR"                # 线性/匀速
    EASE_IN_OUT = "EASE_IN_OUT"      # 缓入缓出（smoothstep）
    SINE = "SINE"                    # 正弦


class ConstraintStatus(str, Enum):
    WITHIN_BOUNDS = "WITHIN_BOUNDS"  # 描述幅度可完整容纳
    CONSTRAINED = "CONSTRAINED"      # 触发边界压缩


AXIS_VECTOR = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
}

# 方向枚举 -> (动作类型, 轴向)
DIRECTION_PARTS = {
    Direction.ROT_X: (ActionType.ROTATE, "X"),
    Direction.ROT_Y: (ActionType.ROTATE, "Y"),
    Direction.ROT_Z: (ActionType.ROTATE, "Z"),
    Direction.SWING_X: (ActionType.SWING, "X"),
    Direction.SWING_Y: (ActionType.SWING, "Y"),
    Direction.SWING_Z: (ActionType.SWING, "Z"),
    Direction.MOVE_X: (ActionType.TRANSLATE, "X"),
    Direction.MOVE_Y: (ActionType.TRANSLATE, "Y"),
    Direction.MOVE_Z: (ActionType.TRANSLATE, "Z"),
    Direction.EXPAND: (ActionType.EXPAND, None),
}
