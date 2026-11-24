# GSB 本地体积离屏渲染器

面向游戏光照预计算与影视预览的确定性 CPU 体积渲染首版。场景由轴对齐介质盒和点光源组成，输出 8 位灰度图、世界单位深度图和可复核的定点误差溯源报告。实现不依赖 GPU、渲染库、容器、外部资源或网络访问。

## 快速开始

目标运行环境为 Python 3.11/3.12；代码保持 Python 3.10+ 兼容。本仓库实际验证环境为 CPython 3.10.12 + NumPy 1.26.4。

```bash
python -m pip install "numpy>=1.26,<3"
python scripts/check_environment.py
python scripts/generate_sample.py --output-dir samples --seed 20261125
python -m volumetric_engine validate --scene samples/scene.json
python -m volumetric_engine render --scene samples/scene.json
```

也可以安装为本地包后使用 `gsb-render`：

```bash
python -m pip install -e .
gsb-render render --scene samples/scene.json
```

自动化测试：

```bash
python -m unittest discover -v
```

## 输入

`samples/scene.json` 引用同目录下的 `media.json`、`lights.json` 与 `jitter.json`。相对路径按场景文件所在目录解析，`state_dir` 和 `output_dir` 同样相对于场景文件解析。

核心字段：

```json
{
  "schema_version": 1,
  "bounds": {"min": [-6, -2, -10], "max": [6, 8, 12]},
  "data": {
    "media": "media.json",
    "lights": "lights.json",
    "jitter": "jitter.json"
  },
  "camera": {
    "position": [0, 2, -9],
    "forward": [0, 0, 1],
    "up": [0, 1, 0],
    "vertical_fov_deg": 47,
    "aspect_ratio": 1.5
  },
  "render": {
    "width": 48,
    "height": 32,
    "samples_per_pixel": 2,
    "step_size": 0.28,
    "max_steps": 240,
    "transmittance_cutoff": 0.08,
    "fixed_scale": 8192,
    "transmittance_tolerance": 0.0002,
    "tile": {"width": 16, "height": 16},
    "max_workers": 4,
    "inflight_memory_limit_bytes": 16777216,
    "state_dir": "../state",
    "output_dir": "../output"
  }
}
```

介质支持标量或三分量消光系数/反照率。三分量数据按 Rec.709 亮度权重合成为灰度路径的有效消光与散射系数。

## 输出

| 文件 | 说明 |
| --- | --- |
| `output/grayscale.png` | 仅由定点路径生成的 8 位灰度图 |
| `output/depth.npy` | `float32` 小端世界单位深度；首个介质入射距离，未命中为 `-1.0` |
| `output/error_report.json` | 汇总、逐像素首超限位置、介质归因、稳定性结果、恢复信息 |
| `output/error_trace.jsonl.gz` | 每个采样点的 float64/定点透射率、绝对差、光学厚度与步序号 |
| `output/manifest.json` | 输出版本、文件大小、SHA-256 与尺寸；作为多文件提交的完成标记 |

`error_trace.jsonl.gz` 首行是列名，之后每行是一条采样记录。报告中的 `first_exceed` 给出每个像素最早超过透射率容差的位置，并归因到介质、光学厚度和步序号。

## 关键行为

- 介质区间按 AABB 求交后建立世界坐标事件链；每段归属由段中点判定。
- 重叠归属优先级：折射率较高者优先；折射率相同时声明顺序较早者优先。
- 定点透射率使用整数乘法舍入：
  `T_fixed=(T_fixed*round(exp(-tau)*scale)+scale//2)//scale`。
- 最终颜色与像素裁定只看定点路径；float64 路径只进入诊断报告。
- 步长/阈值稳定性评估默认执行；`--no-stability` 可关闭。
- 分块按索引顺序提交，在途采样预算不足时后续块等待，不降低超采样或步长精度。
- 相同版本复用已完成块；块摘要/像素数异常时返回恢复告警 `E_DIGEST` 并补渲。
- `SIGINT`/`SIGTERM` 只在分块边界生效，分别以 `130`/`143` 退出，并写 `state/interrupted.json`。
- 相同输出版本且摘要/尺寸有效时跳过替换；版本不同或同版本文件损坏时通过临时文件修复。

## 业务码与退出码

非法输入统一输出 JSON：`{"ok":false,"code":"E_xxx","message":"..."}`。常见业务码包括：

`E_JSON`、`E_SCHEMA`、`E_MEDIA_SIZE`、`E_EXTINCTION`、`E_ALBEDO`、`E_IOR`、`E_LIGHT_POS`、`E_LIGHT_INTENSITY`、`E_FOV`、`E_ASPECT`、`E_FIXED_SCALE`、`E_RESOLUTION`、`E_SAMPLES`、`E_STEP`、`E_MAX_STEPS`、`E_CUTOFF`、`E_TOLERANCE`、`E_TILE`、`E_CONCURRENCY`、`E_MEMORY_BUDGET`、`E_PATH`。

运行期：`E_DIGEST`（3）、`E_IO/E_STATE/E_OUTPUT/E_INTERNAL`（4）、`E_INTERRUPTED`（130 或 143）。

## 状态目录

```text
state/
  meta.json                    # 当前活动渲染版本
  blocks/<version>/block_*.json
  interrupted.json             # 中断标记（成功完成后移除）
  completed.json               # 完成标记、块摘要、累计调度统计
```

块文件包含颜色、参考色、深度、逐像素误差、完整采样轨迹、误差统计、像素数、版本标识和内容摘要。块先写同目录临时文件并 fsync，再原子替换。

详细设计与验收记录见 [TECHNICAL.md](TECHNICAL.md) 和 [VALIDATION.md](VALIDATION.md)。
