# medvol-raycast：确定性医学体数据光线投射组件

这是一个面向医学可视化集成的本地体数据光线投射组件。它提供 NumPy 参考实现和 OpenGL 3.3 离屏 FBO 实现，输入为本地生成的 `uint16` 三维标量体数据，不依赖 DICOM、PACS 或任何外部医学影像数据。

核心交付包括：

- 精确 AABB 光线求交、三线性插值、分段线性传输函数、从前到后 RGBA 合成。
- 固定步长与传输函数梯度驱动的自适应步长。
- 基于解析三线性导数与传输函数 LUT 导数的逐光线误差上界。
- 固定采样顺序、无抖动、`float64` 内部累加、单次 `float32` 输出转换。
- 隐藏窗口 OpenGL 3.3 Core 上下文、RGBA32F FBO、体积纹理上传与回读。
- 结构化业务错误码、SHA-256 缓冲校验、PNG/JSON 演示产物。

## 环境安装

### Windows 10/11

要求 Python 3.10+、支持 OpenGL 3.3 Core 的显卡驱动。Windows 上使用 GLFW 创建不可见窗口和 WGL/OpenGL 上下文，实际绘制目标是 FBO，不会显示窗口。

```powershell
py -3.10 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

如需确认驱动能力，直接运行示例；上下文或 shader 创建失败会以 `E_GL_CONTEXT` 结构化上报。

### Linux/WSL2（本次实际验证环境）

```bash
python3 -m venv .venv
# 如果系统没有 ensurepip，可先执行 bootstrap get-pip.py 后再安装依赖
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

本次验证使用 WSL2 + Mesa 23.2.1，OpenGL 4.2 Core，渲染器为 `D3D12 (AMD Radeon(TM) Graphics)`。代码本身使用跨平台 GLFW 隐藏窗口，Windows 路径使用同一套 FBO/回读流程。

## 快速运行

```bash
# 1. 生成本地 128^3 uint16 球体/柱状结构样本
.venv/bin/python scripts/generate_volume.py \
  --output artifacts/demo/volume_128.npy

# 2. 完整演示：固定/自适应、CPU 参考、OpenGL FBO、两次运行校验
.venv/bin/python examples/render_demo.py --resolution 256

# 3. 自动化测试
.venv/bin/python -m pytest -q
```

Windows 将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。`--skip-gl` 可只跑 CPU 参考路径；`--require-gl` 要求 OpenGL 路径必须成功。

演示输出位于 `artifacts/demo/`：

- `volume_128.npy`、`volume_128.json`：合成体数据与元数据。
- `transfer_function.json`：确定性传输函数描述。
- `fixed_cpu.png`、`adaptive_cpu.png`：CPU 参考图像。
- `fixed_gl.png`、`adaptive_gl.png`：OpenGL FBO 回读图像。
- `fixed_cpu_report.json`、`adaptive_cpu_report.json`：完整渲染报告。
- `fixed_gl_report.json`、`adaptive_gl_report.json`：FBO 报告，附带 CPU 参考误差分析。
- `report.json`：全流程摘要、哈希、误差统计和验收结论。

## 组件结构

```text
src/medvol/
  volume.py            三维体数据校验、NPY 加载、确定性元数据哈希
  camera.py            透视相机、像素中心光线生成、零方向校验
  transfer_function.py JSON 校验、分段线性映射、不透明度曲线、预构建 LUT
  raycast.py           AABB slab 求交、解析三线性插值及梯度、sRGB 编码
  renderer.py          CPU 参考管线、固定/自适应步长、误差报告、SHA-256
  gl_renderer.py       隐藏 GLFW 上下文、3D/LUT 纹理、RGBA32F FBO、回读
  shaders.py           GLSL 3.30 片元着色器光线投射
  synthetic.py         本地确定性 CT-like phantom
scripts/generate_volume.py
examples/render_demo.py
tests/
```

## 外部 UI 集成接口

```python
from medvol import Camera, OffscreenGLRenderer, SamplingConfig, TransferFunction, Volume

volume = Volume.load_npy("artifacts/demo/volume_128.npy")
tf = TransferFunction.from_file("artifacts/demo/transfer_function.json")
camera = Camera.look_at((240.14, 181.26, 240.14), volume.center, fov_y_deg=38.0)
config = SamplingConfig(base_step=0.8, adaptive=False, error_threshold=2.0)

with OffscreenGLRenderer(volume, camera, tf, config, width=512, height=512) as renderer:
    result = renderer.render()
    rgba_linear = result.rgba_linear          # float32 top-to-bottom RGBA
    rgba_sha256 = result.rgba_linear_sha256   # SHA-256 of raw float32 bytes
    texture_id = renderer.color_texture_id    # valid while this context is current
```

集成方式有两种：

1. 直接使用 `result.rgba_linear`，这是与 OpenGL 上下文解耦的确定性 NumPy 回读数组。
2. 在同一 OpenGL 上下文中使用 `color_texture_id`，避免再次上传纹理；调用 `make_current()` 后由外部 UI 绑定。

CPU 参考路径同样支持 `VolumeRenderer.render(512, 512)`，可用于无 GPU 环境、诊断校验和误差分析。两种路径生成的 hash 分别稳定，但 CPU 与 GPU 使用不同的浮点执行精度，跨后端不承诺逐字节相同；同一个后端的重复运行逐字节一致。

## 渲染管线

### 光线几何与采样范围

相机为透视相机，使用像素中心生成归一化方向。体数据包围盒是体素中心范围：

```text
bounds_min = origin
bounds_max = origin + (shape_xyz - 1) * spacing
```

每条光线通过 slab 方法得到精确的 `[t_enter, t_exit]`。光线总长度被分成整数个等宽 block：

```text
block_count = max(1, ceil(ray_length / base_step))
block_width = ray_length / block_count
sample_t = t_enter + (block + 0.5) * block_width
```

因此最后一个采样仍位于包围盒内，且固定模式的实际 block 宽度不超过用户配置的基础步长。短于一个 block 的掠射光线也会保留精确几何，此时其实际步长可能明显小于基础步长，报告中通过 `actual_min_world` 如实记录。

### 三线性插值

体数据 NumPy 布局为 `(z, y, x)`，世界坐标为 `(x, y, z)`。对每个采样位置计算 8 个邻域体素、三线性权重和解析梯度：

```text
c = sum(w_i * v_i)
∂c/∂x, ∂c/∂y, ∂c/∂z 由三线性插值的解析导数直接计算
```

OpenGL 路径将体数据上传为 `R16UI`，着色器使用 `texelFetch` 手工三线性插值，避免 `R16F` 只有约 11 位尾数导致 `uint16` 精度丢失。

### 传输函数

传输函数由严格递增的控制点和 `[0,1]` 不透明度校正曲线组成。渲染前一次性构建 RGBA LUT，渲染循环只做整数索引查表，不解析 JSON。

```json
{
  "value_range": [0.0, 3811.0],
  "control_points": [
    {"value": 0.0, "color": [0.0, 0.0, 0.0], "opacity": 0.0},
    {"value": 1067.08, "color": [0.45, 0.08, 0.03], "opacity": 0.015},
    {"value": 3811.0, "color": [1.0, 1.0, 1.0], "opacity": 0.70}
  ],
  "opacity_curve": [[0.0, 0.0], [1.0, 1.0]],
  "opacity_gamma": 1.0,
  "lut_size": 4096
}
```

校验失败返回 `E_TF_RANGE`，包括控制点值不递增、值超出配置范围、颜色/不透明度越界、不透明度曲线非法等。

### 合成

按从前到后顺序：

```text
C_i = C_{i-1} + T_{i-1} * alpha_i * RGB_i
T_i = T_{i-1} * (1 - alpha_i)
A_out = 1 - T_final
```

内部颜色、透射率、误差均使用 `float64` 标量/数组顺序累加，最终一次转换为 `float32`。不启用抖动；`random_seed` 仅作为配置记录。`early_termination_threshold` 是剩余透射率阈值，采样时同时参与终止判断。

## 数值误差模型

误差定义版本为 `trilinear-gradient-lipschitz/1`。每个实际采样步长 `h` 执行以下计算：

1. 三线性解析导数给出体素值梯度 `∇c`。
2. 单步内相对半采样点的标量变化上界为 `0.5 * h * |∇c|`。
3. 传输函数 LUT 的 RGBA 导数 L1 范数把标量变化传播到输出色彩空间：

```text
local_error = ||d(TF)/dc||_1 * (0.5 * h * |∇c|_2)
```

4. 按合成透射率累积：

```text
accumulated_error += T_before * local_error
max_local_error   = max(max_local_error, local_error)
```

5. 输出另记录 `float64` 内部累加误差上界与 `float32` 转换误差上界：

```text
float64_accumulation_error_bound = eps_float64 * n_samples * sum(|contributions|)
float32_cast_error_bound ≈ 0.5 * eps_float32 * sum(|contributions|)
```

最终每像素误差为：

```text
pixel_error = accumulated_error + float64_error_bound + float32_cast_error_bound
```

报告给出 `total_error`（总误差）、`accumulated_error`（插值与传输函数传播误差）、`max_local_error`、`float32_cast_error_bound`、`float64_accumulation_error_bound` 的最大值、均值、95 分位。若设置了 `error_threshold` 且有像素超过阈值，渲染仍完成，但业务码为 `E_ERROR_BOUND`，同时报告 `exceeded_pixel_count` 与 `exceeded_pixel_ratio`。

### 自适应步长

每个基础 block 的中点先计算传输函数 LUT 梯度范数，并按全局最大梯度的归一化比例决定细分级别：

```text
ratio = ||d(TF)/dc|| / max||d(TF)/dc||
level = ceil(log2(ratio / gradient_threshold)), 限制在 [0, max_refinement]
step  = block_width / 2^level
```

细分只改变 block 内的采样密度，光线出生点、方向、AABB 顶点、传输函数映射均不改变。示例与测试会比较同一基础步长下固定/自适应模式的 `max/mean/p95` 误差，并要求自适应不高于固定模式。

## 确定性保证

- 不启用随机抖动；若集成方将来增加抖动，配置中已保留固定 `random_seed` 字段。
- 全部光线按像素线性顺序生成，采样从前到后单线程累加，不使用多线程归约。
- CPU 内部 `float64` 累加，最终固定一次 `float32` 转换。
- LUT 和体数据哈希基于规范化 C-order 字节。
- `hash_rgba()` 对 C-order、little-endian `float32 RGBA` 原始字节计算 SHA-256。
- GPU 路径固定像素中心、固定循环上限、固定采样顺序；同一上下文/驱动下重复运行逐字节一致。

结构化报告中的 `determinism` 字段记录 seed、jitter 状态、累加精度、归约顺序与 hash 覆盖范围。

## 业务错误码

| 业务码 | 触发条件 |
|---|---|
| `E_VOLUME_SHAPE` | 体数据非三维、任一轴小于 2、浮点体数据含 NaN/Inf、spacing/origin 非法 |
| `E_TF_RANGE` | 控制点值不严格递增、点值超出配置范围、颜色/不透明度越界、校正曲线非法 |
| `E_STEP_TOO_SMALL` | 基础步长小于最小体素间距的 0.1 倍；或自适应细分会低于该下限 |
| `E_RAY_DIR` | 相机位置与目标重合、上方向与视线平行、生成的射线方向为零/NaN |
| `E_ERROR_BOUND` | 渲染完成，但像素误差超过用户阈值 |
| `E_GL_CONTEXT` | GLFW/OpenGL 3.3 上下文、shader、FBO 初始化失败 |
| `E_BUFFER_SIZE` | `glReadPixels` 回读字节数不等于 `width * height * 4 * sizeof(float32)` |

异常对象提供 `VolumeRenderError.to_dict()`；`VolumeRenderer.render_safe()` 直接返回结构化 envelope。示例中的 GL 失败也会写入 `gl_error.json`。

## 本次实际验证结果

验证环境：Python 3.11.0rc1、NumPy 2.4.6、PyOpenGL 3.10、WSL2/Mesa 23.2.1、AMD Radeon D3D12 后端。

自动化测试：`20 passed`。

128³ `uint16` 样本：

```text
shape=(128,128,128) dtype=uint16 value_range=[0, 3811]
volume_metadata_sha256=febecdac76ec85cccee2a08c685eeca4b80752e000d3351eae67a19a6af7d52
```

256×256 两次运行结果：

| 模式 | CPU RGBA SHA-256 | 采样数 | 总误差 max / mean / p95 | 超阈值像素比例 |
|---|---|---:|---|---:|
| 固定步长 | `9cfc518509e147bd55edf6f91cea96d64c9e89137ca88a21fd47a1aba0aaa55e` | 4,126,856 | 17.7823 / 0.4929 / 2.6942 | 0.09535 |
| 自适应 | `1af6a0c8478fe3f91baa3f7098361df19af240cabac0bc253200638ca2389ff4` | 6,586,593 | 8.2581 / 0.2902 / 1.7906 | 0.03592 |

两次 CPU 运行均满足：缓冲字节完全相等、SHA-256 相等、`error_analysis` 与 `sampling` 报告完全相等。示例阈值设为 2.0，因此两种模式均输出业务码 `E_ERROR_BOUND`；自适应模式超限像素比例从 9.535% 降到 3.592%。

OpenGL FBO 256×256 回读字节数 1,048,576，RGBA32F 附件完整：

```text
fixed GL run1=run2:   9059cb02d211a44213802916855065ea9b2d38a52a99d9a76170ecd8e7dd155a
adaptive GL run1=run2: dd2c978369d4c852c08c62208d17930c152e3ff7e2fdbf92409882f92f9f12cf
```

GPU 两次运行缓冲逐字节相等；CPU 与 GPU hash 不同，这是两套浮点执行路径的正常差异，不用于跨后端等价性断言。演示验收摘要全部为 `true`：两次运行哈希/误差统计一致、自适应误差不高于固定模式、自适应采样数不少于固定模式。

完整示例还在两个独立 Python 进程之间比较了全部 PNG、JSON 报告和哈希，输出文件 SHA-256 全部一致。

## 已知边界

- 本组件面向固定输入的离屏诊断/复核，不包含交互式相机控制、DICOM 解析或 GPU 实时传输函数编辑。
- GPU 浮点结果在同一驱动/设备上确定；跨 GPU 厂商或驱动版本不应假定逐字节一致。需要跨平台诊断等价性时使用 CPU 参考缓冲。
- OpenGL 离屏路径不显示窗口，但仍要求操作系统提供 OpenGL/WGL（Windows）或 EGL/GLX（Linux）驱动上下文；初始化失败返回 `E_GL_CONTEXT`，不会静默退化为可见窗口。
