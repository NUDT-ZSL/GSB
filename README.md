# skinned-renderer — 2D 蒙皮网格骨骼动画 → PNG 帧序列渲染器

面向独立游戏团队的小工具：把自建 JSON 描述的 2D 蒙皮网格与骨骼关键帧动画渲染成逐帧 PNG 序列，供游戏引擎使用。**仅使用 Python 标准库**（3.10+），无第三方依赖。

## 文件

| 文件 | 说明 |
|---|---|
| `skinned_renderer.py` | 渲染器本体（CLI + 可导入模块） |
| `make_sample.py` | 确定性生成演示输入 `sample_character.json` |
| `run_tests.py` | 15 项验收测试（覆盖全部 9 条验收标准） |

## 快速开始（可复现）

```bash
python --version            # 需 3.10+
python make_sample.py       # 生成 sample_character.json
python skinned_renderer.py sample_character.json -o frames
python run_tests.py         # 运行全部验收测试
```

成功输出示例：

```
总帧数: 8
首帧: frame_0001.png
末帧: frame_0008.png
输出目录: E:\Company\demo\projects\project24\frames
```

退出码：`0` 成功；`2` 输入非法或超限（此时不生成任何 PNG）。

## 输入 JSON 格式

```json
{
  "mesh": {
    "vertices":  [[x, y], ...],
    "triangles": [[i0, i1, i2], ...],
    "weights":   [[[boneIndex, weight], ...], ...]
  },
  "bones": [
    {"name": "root", "parent": null,
     "rest": {"translate": [tx, ty], "rotate": 角度}}
  ],
  "animation": {
    "keyframes": [
      {"time": 0.0, "bones": {"root": {"translate": [tx, ty], "rotate": 角度}}}
    ]
  },
  "output": {
    "width": 128, "height": 128, "frames": 8,
    "start_time": 0.0, "end_time": 4.0,
    "color": [r, g, b, a],
    "directory": "frames"
  }
}
```

- 顶点坐标即输出图像的像素坐标；三角形纯色填充，背景全透明（RGBA，8 位）。
- `weights` 与顶点一一对应；每个顶点权重和须在 **[0.99, 1.01]** 内（自动归一化到 1），越界报错并指出顶点索引。
- 骨骼层级最多 **3 层**，父骨骼须先于子骨骼声明；全局变换按父子顺序累积（`G = G_parent · T · R`）。
- 关键帧按 `time` 线性插值（平移、旋转分别插值）；超出时间范围取最近关键帧；关键帧中未出现的骨骼回退到静止姿态。
- 蒙皮采用线性混合蒙皮（LBS）：`v' = Σ wᵢ · (G_animᵢ · G_restᵢ⁻¹) · v`。
- 限制：帧数 ≤ **100**，单帧像素 ≤ **4,000,000**，超限拒绝处理并提示限制值。

## 验收标准实测结果（`python run_tests.py`，15/15 通过）

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 权重和 1.02 报错且消息含顶点索引；1.005 归一化成功 | PASS |
| 2 | 父旋转 90°+子平移，顶点 `(2,1)→(-6,2)`；三层累积 `→(-6,5)`，与手算一致 | PASS |
| 3 | 两关键帧中间时刻顶点 x 线性过渡 `2→7→12`；超范围取最近关键帧 | PASS |
| 4 | 双骨骼各 0.5 权重，输出 `(6,6)` = 两变换坐标 `(11,1)`/`(1,11)` 的均值 | PASS |
| 5 | 单三角形中心像素 `(255,0,0,255)`，外部及斜边外像素 alpha=0 | PASS |
| 6 | 导出 3 帧 `frame_0001..0003.png`，递增、非空、无跳号重名 | PASS |
| 7 | 缺 `vertices` 字段 → 退出码非零、明确错误、无 PNG 生成；非数值坐标同样报错 | PASS |
| 8 | 请求 101 帧报错并提及上限 100；2100×2000 像素报错并提及 4000000 | PASS |
| 9 | 摘要含 `总帧数: 3`、`首帧: frame_0001.png`、末帧与输出目录绝对路径 | PASS |
| 附加 | 四层骨骼报错并提及上限 3 层 | PASS |

端到端演示（`make_sample.py` + 渲染 8 帧）：帧大小随动画对称变化（196→296→196 字节），首帧不透明像素 1800 = 网格矩形面积 3×30×20，与理论值一致；中间帧目视为正确弯曲的机械臂姿态。

## 业务操作样例

```bash
# 1) 渲染自带演示（两节机械臂，三层骨骼，8 帧）
python make_sample.py
python skinned_renderer.py sample_character.json -o frames

# 2) 渲染自有动画（输出目录也可写在 JSON 的 output.directory 里）
python skinned_renderer.py my_character.json

# 3) 错误处理演示：非法输入不产出任何文件，退出码 2
python skinned_renderer.py bad_input.json -o out; echo $?
```
