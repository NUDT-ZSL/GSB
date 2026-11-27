# 手语导览 SDF 手部渲染与核验内核

本工作区提供一个可集成、仅依赖 NumPy 与 Python 标准库的确定性手语手部渲染组件。它读取骨骼关键帧工程，完成姿态采样、线性蒙皮、SDF 球面追踪、Lambert 着色、跨骨骼穿插检测和蒙皮形变核验，并按工程版本持久化全部产物。

## 快速运行

环境为 Linux / WSL Ubuntu，Python 3.10+：

```bash
python3 -m pip install -r requirements.txt
python3 tools/generate_demo_project.py
python3 run_demo.py --output-dir output/demo
```

等价的内核命令：

```bash
python3 -m signhand.cli run \
  --project examples/generated/sign_actions.json \
  --key-moments examples/generated/key_moments.json \
  --output-dir output/demo
```

示例中的输入和输出均为工作区相对路径。运行前需保证 `output/demo` 可写，入口会自动创建目录。

主要产物位于 `output/demo/versions/guide-v1/`：

- `render/frame_0000.png` 至 `render/frame_0008.png`：逐帧 PNG 渲染图。
- `skinning/frame_XXXX.json`：每帧每根骨骼的世界变换、全部 SDF 基元世界变换和蒙皮后顶点。
- `trace/frame_XXXX_*.npy`：逐像素交点参数、业务状态码和单位表面法线。
- `intersections.json`：重点时刻的跨骨骼 SDF 基元穿插清单。
- `deformation_report.json`：指定指节的蒙皮表面距离与近似体积核验。
- `render_manifest.json`、`repeatability.json`、`run_report.json`：渲染统计、重复性核验和总报告。
- `artifact_manifest.json`：版本内全部文件的 SHA-256 清单。
- `output/demo/index.json` 与 `latest.json`：供导览终端发现和取回版本。

## 实现说明

### 骨骼、插值与蒙皮

工程 JSON 的 `skeleton.joints` 定义唯一根节点、父子关系、绑定姿态局部平移和 Euler 旋转。`animation.tracks` 支持 `linear`、`step`、`cubic` 三种逐关节曲线；局部旋转为 `R_rest @ R_animation`，再沿父链得到世界矩阵。`SkinnedHand` 使用 `world @ inverse_bind` 和权重归一化后的线性混合蒙皮，输出 `SkinResult.world_matrices` 与 `skinned_vertices`。

### 统一 SDF 与球面追踪

`SDFScene` 支持 `sphere`、`capsule`、`ellipsoid` 三种基元，按所属骨骼刚体变换到世界空间。椭球径向距离使用保守近似；渲染组合可配置 `smooth_union_radius` 平滑拼接关节。每个采样点的距离和中心差分梯度均为纯确定性计算。

`SphereTracer` 从相机近平面沿单位射线自适应步进：

- 步长取 `safety * distance`，再限制在 `[min_step, max_step]` 和剩余射线距离内。
- 首次达到表面容差后，用前后两次距离插值求交点。
- 法线由统一 SDF 的中心差分梯度归一化得到。
- 用环境项与单方向 Lambert 漫反射着色。

逐像素业务状态码：

| code | 名称 | 含义 |
|---:|---|---|
| 0 | `MISS` | 射线在场景/远平面内未命中表面 |
| 1 | `HIT` | 找到首次表面交点 |
| 2 | `ERR_TRACE_MAX_STEPS` | 达到最大步数仍未终止，不静默丢弃 |
| 3 | `ERR_HIT_BEYOND_FAR_PLANE` | 交点位于相机远平面外 |

错误状态会在统计、`run_report.json` 和诊断着色中显式出现。

### 穿插检测

在 `key_moments.json` 指定的帧，仅比较属于不同骨骼的基元对。设两基元距离为 `dA`、`dB`，交集距离场使用 `max(dA, dB)`；其负区域恰好是二者内部重叠区，区域极小值取负即为 `max_penetration_depth`。检测器使用确定性的包围盒网格、基元表面 Fibonacci 采样和固定模式搜索细化，可处理基元完全重叠。清单给出帧号、重点时刻、涉事基元、所属骨骼、最大穿插深度、重叠点和两个基元在该点的距离。

示例工程含中性开掌和若干诊断性贴合/压力手势。`neutral_open_hand` 为零穿插基线；后续重点手势用于覆盖指间贴合、拇指与掌部接触以及极端腕部旋转。当前示例检出穿插是核验结果，不应直接作为发布动画。

### 形变核验

`deformation_report.json` 对每个指定指节比较：

- 绑定姿态顶点在其静止 SDF 壳上的距离。
- 蒙皮后顶点在对应姿态 SDF 壳上的距离。
- 完全刚性参考距离与 LBS 相对刚性参考的 RMS 偏差。
- 由闭合三角网格计算的蒙皮前、蒙皮后近似体积及体积比。

默认判定阈值为：体积比 `< 0.92` 标记 `VOLUME_SHRINK`，体积比 `> 1.12` 标记 `ABNORMAL_EXPANSION`，指定分位（默认 P95）的绝对 SDF 距离 `> 0.012` 标记 `SDF_SURFACE_DRIFT`，任一三角面翻转标记 `TRIANGLE_INVERSION`。阈值均写入报告，可在工程 `deformation_verification` 中调整。

### 确定性、重复性与版本

生成器不使用随机数；采样方向、搜索顺序和排序规则固定。`repeatability.json` 对首个重点时刻重新执行完整渲染和穿插检测，并比较状态、交点参数、法线、图像字节和穿插清单。默认交点和法线容差为 `1e-10`，示例实测差值为 0。

每个版本先写入 `.staging/<version>`，完整后原子替换到 `versions/<version>`，并写入全文件 SHA-256 清单。同一输入重复运行会得到相同文件清单；不同 `project_version` 可并存。

```bash
python3 -m signhand.cli versions --output-dir output/demo
python3 -m signhand.cli inspect --output-dir output/demo --version guide-v1
```

Python 终端按版本取回：

```python
from signhand.store import VersionedArtifactStore

store = VersionedArtifactStore("output/demo")
version_dir = store.resolve("guide-v1")
manifest = store.load_manifest("guide-v1")
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖绑定姿态蒙皮一致性、关键帧确定性、球体距离/梯度、命中与法线、远平面和最大步数状态码、完全重叠检测、非重叠过滤，以及端到端版本清单和重复运行产物一致性。

## 代码结构

```text
signhand/
  project.py       JSON 格式解析、校验与插值曲线
  animation.py     骨架采样与线性蒙皮
  sdf.py           世界空间基元、统一距离场与梯度
  tracer.py        相机射线、球面追踪、状态码与着色
  collision.py     跨骨骼基元重叠和深度细化
  deformation.py   表面距离、体积与形变阈值判定
  pipeline.py      逐帧完整流水线与核验汇总
  store.py         版本目录、原子提交与 SHA-256 清单
  cli.py           本地运行、版本列举和检查命令
tools/
  generate_demo_project.py
tests/
examples/generated/
run_demo.py
```

