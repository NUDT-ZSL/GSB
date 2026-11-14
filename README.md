# 2D 骨骼动画预览与诊断工具

面向动画师的本地工具：用 JSON 描述 2D 骨骼动画，在导入引擎前发现
**预览跳变、旋转方向错误、父子层级错位、异常关键帧**等问题。支持时间线
播放/逐帧/拖动定位、骨骼高亮、曲线诊断、姿态报告导出。

- Python 3.10+ / Pygame 2.5+，纯本地运行，不访问网络、不使用容器
- 样例动画 JSON 由代码生成，无需额外数据文件
- 单条动画关键帧规模 ≤ 1000

## 1. 环境准备

```powershell
python --version                 # 需 3.10 或更高
pip install -r requirements.txt  # 即 pygame>=2.5
```

## 2. 本地启动方式

在项目根目录 `project75/` 下执行：

```powershell
# 先生成样例动画（输出到 samples/，可重复执行）
python -I tools/generate_samples.py

# 启动 GUI 并直接载入样例
python -m app.main samples/basic.json

# 不传路径启动 -> 显示“文件不可用 / 选择文件”界面，可在窗口内重新选择
python -m app.main
```

> 也可使用 `python -I app/main.py samples/basic.json`，脚本已兼容 `-I` 隔离模式。

### 操作方式

| 操作 | 按钮 | 快捷键 |
| --- | --- | --- |
| 播放 / 暂停 | 播放 | `空格` |
| 逐帧前进 / 后退（固定步长 1/30 秒） | 后一帧 / 前一帧 | `→` / `←` |
| 切换选中骨骼 | 点击左侧层级面板 | `↑` / `↓` |
| 切换曲线诊断视图 | 曲线诊断 | `C` |
| 重新加载当前文件 | 重新加载 | `R` |
| 导出当前姿态报告 JSON | 导出报告 | `E` |
| 退出 | 窗口关闭按钮 | `Esc` |

时间线滑块可直接鼠标拖动；绿色三角为选中骨骼的正常关键帧，
红色三角为被跳过的异常关键帧。

## 3. 动画 JSON 格式

```json
{
  "name": "动画名（可选）",
  "duration": 2.0,
  "bones": [
    {
      "name": "root",
      "parent": null,
      "length": 90,
      "keyframes": [
        {"time": 0.0, "translation": [0, 0], "rotation": 0,   "scale": [1, 1]},
        {"time": 1.0, "rotation": 90},
        {"time": 2.0, "translation": [40, 0], "rotation": 0, "scale": [1.2, 1.2]}
      ]
    }
  ]
}
```

- 关键帧缺失字段按 **平移 (0,0)、旋转 0、缩放 (1,1)** 填充
- `duration` 可省略，缺省取所有关键帧的最大时间
- 平移/缩放相邻关键帧线性插值；**旋转按最短有向角度路径插值**
  （0°→350° 的中间帧为 355°，而不是 175°）
- 时间超出首末关键帧范围时保持最近关键帧值（clamp）

## 4. 错误处理规则

| 异常 | 处理 |
| --- | --- |
| 文件不存在 / 无法读取 / JSON 损坏 | 窗口提示“文件不可用”，保留“选择文件”入口，选有效文件后恢复预览 |
| 同一骨骼出现重复时间关键帧 | 状态区提示骨骼名与重复时间，保留首条、跳过重复条，其他骨骼正常预览 |
| 关键帧时间为负 / 超出总时长 / 时间倒序 | 仅标记该骨骼异常并跳过该帧；该骨骼保持最后正常姿态，状态区列出被跳过帧 |
| 单条关键帧字段格式错误 | 跳过该条或按默认值填充并提示，同骨骼其余关键帧仍可用 |

## 5. 姿态报告

在任意时间点点击“导出报告”，输出 JSON（默认存 `reports/`），含每根骨骼的
世界坐标、世界旋转角、世界缩放与末端坐标，**数值均保留两位小数**。
示例（t=1.0s，root 旋转 90° 带动 child 联动）：

```json
{
  "time": 1.0,
  "bones": [
    {"name": "root",  "world": {"x": 0.0, "y": 0.0, "rotation": 90.0, "scale_x": 1.0, "scale_y": 1.0}},
    {"name": "child", "world": {"x": 0.0, "y": 90.0, "rotation": 45.0, "scale_x": 1.0, "scale_y": 1.0}}
  ]
}
```

重新加载文件或退出前，若姿态已变化且报告未导出，会先弹窗确认是否放弃。

## 6. 自动化验证（无头可复现）

```powershell
python -I tools/verify.py
```

该脚本在 SDL dummy 视频驱动下运行，包含 **42 条断言**（覆盖全部 11 项验收
需求）并输出 9 张界面截图到 `screenshots/`，明细写入 `VERIFICATION.md`，
全部通过时退出码为 0。

### 样例文件与用途

| 文件 | 覆盖场景 |
| --- | --- |
| `samples/basic.json` | 父子联动、缺失字段默认值、0°/350° 最短路径插值、clamp |
| `samples/duplicate.json` | 重复时间关键帧提示 + 其余骨骼正常预览 |
| `samples/bad_time.json` | 负时间 / 倒序 / 超总时长关键帧跳过与警示带 |
| `samples/malformed_keyframe.json` | 单条关键帧格式错误 |
| `samples/broken.json` | 文件级 JSON 损坏 → 文件不可用界面 |

## 7. 目录结构

```
project75/
├── app/
│   ├── model.py      # 加载校验 / 插值 / 层级合成 / 报告导出（无 GUI 依赖，可单测）
│   ├── ui.py         # Pygame 界面：骨架、时间线、曲线诊断、弹窗
│   └── main.py       # 入口
├── tools/
│   ├── generate_samples.py   # 样例 JSON 生成器
│   └── verify.py             # 42 项无头验证 + 截图取证
├── samples/          # 生成的样例动画
├── reports/          # 导出的姿态报告
└── screenshots/      # 验证截图
```
