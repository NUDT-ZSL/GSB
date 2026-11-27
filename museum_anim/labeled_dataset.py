"""人工标注的展陈脚本验收集。

每条样本包含自然语言文本与人工标注的方向/幅度/周期/缓动期望值，
供 tests 与 CLI 核对工作台解析与生成结果是否符合脚本意图。
"""

LABELED_SCRIPT_SET = [
    {
        "text": "绕Z轴缓慢旋转一周，周期8秒，线性",
        "label": {
            "action_type": "ROTATE", "direction": "ROT_Z",
            "amplitude": 360.0, "period": 8.0, "easing": "LINEAR",
        },
    },
    {
        "text": "沿水平轴来回摆动，幅度45度，周期4秒，缓入缓出",
        "label": {
            "action_type": "SWING", "direction": "SWING_X",
            "amplitude": 45.0, "period": 4.0, "easing": "EASE_IN_OUT",
        },
    },
    {
        "text": "沿X轴平移0.3米，周期4秒，正弦",
        "label": {
            "action_type": "TRANSLATE", "direction": "MOVE_X",
            "amplitude": 0.3, "period": 4.0, "easing": "SINE",
        },
    },
    {
        "text": "绕Y轴快速旋转半周，周期2秒，缓入缓出",
        "label": {
            "action_type": "ROTATE", "direction": "ROT_Y",
            "amplitude": 180.0, "period": 2.0, "easing": "EASE_IN_OUT",
        },
    },
    {
        "text": "按材质分区展开外扩0.1米，周期6秒，线性",
        "label": {
            "action_type": "EXPAND", "direction": "EXPAND",
            "amplitude": 0.1, "period": 6.0, "easing": "LINEAR",
        },
    },
    {
        "text": "沿竖轴反向旋转90度，周期5秒，正弦",
        "label": {
            "action_type": "ROTATE", "direction": "ROT_Z",
            "amplitude": -90.0, "period": 5.0, "easing": "SINE",
        },
    },
]
