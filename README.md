# PanoWeave

面向本地多视角图像拼接的确定性领域组件与完整示例。项目只依赖 Python 3.10+ 与 NumPy，不使用 Docker、容器、外部服务或网络目标。

核心链路：

`v1/v2/v3 参数解析 → 统一内部模型 → 几何对齐与重采样 → 曝光/颜色增益 → 重叠区羽化融合 → 全景/掩码/权重输出`

同时提供：

- 三版本双向迁移、补全来源、丢弃/展平明细
- 语义漂移与往返可表达字段裁决
- 四阶段异常注入、业务错误归因和降级链路
- 内容寻址缓存、资源上限、参数快照并发提交控制
- 静态但可交互的本地诊断页面
- 固定种子样本、逐字节确定性测试和自检报告

## 快速开始

### Windows

在项目根目录执行：

```bat
py -3.10 -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
py -m pip install -e .
py -m panoweave demo --samples samples --out artifacts --clean
start "" artifacts\diagnostic.html
```

也可以不安装项目，直接运行：

```bat
run_demo.bat
```

`run_demo.bat` 会把 `src` 加入 `PYTHONPATH`，然后执行一次完整演示。

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
python -m panoweave demo --samples samples --out artifacts --clean
```

零安装运行：

```bash
PYTHONPATH=src python -m panoweave demo --samples samples --out artifacts --clean
```

## 常用命令

```bash
# 生成固定种子样本、三版本参数、清单和配置
python -m panoweave sample --out samples --seed 20261123 --clean

# 渲染任一版本；缓存按内容哈希命名
python -m panoweave render \
  --params samples/params_v1.txt \
  --samples samples \
  --out artifacts/render_v1 \
  --clean-cache

# 迁移并写出迁移报告
python -m panoweave migrate \
  --input samples/params_v3.json \
  --to v2 \
  --out artifacts/migrated_v2.txt

# 单次异常注入
python -m panoweave inject \
  --params samples/params_v3.json \
  --samples samples \
  --out artifacts/inject_geometry \
  --stage geometry --kind singular_homography --view 0

# 完整诊断与自检
python -m panoweave diagnose \
  --samples samples \
  --out artifacts \
  --clean-cache
```

## 样本与产物

`panoweave sample` 在 `samples/` 生成：

- `views/view_000.npy`、`views/view_001.npy`、`views/view_002.npy`
- 对应的可查看 PNG 预览
- `params_v1.txt`、`params_v2.txt`、`params_v3.json`
- `manifest.json`：固定种子、文件 SHA-256、视角图像映射、语义哈希
- `config.json`：解析默认值和资源上限
- `injections.json`：四阶段注入示例
- `ground_truth.png`：生成纹理参考

`panoweave demo` 在 `artifacts/` 生成：

- `assets/v1|v2|v3/panorama.png`
- `assets/v1|v2|v3/valid_mask.png`
- `assets/v1|v2|v3/weight_map.png`、`weight_heatmap.png`
- 原始 `panorama.npy`、`valid_mask.npy`、`weight_sum.npy`、`weight_map.npy`
- `cache/`：按内容哈希命名的重采样中间产物
- `snapshots/`：参数、迁移、全景结果快照
- `diagnostic.html`：本地交互诊断页
- `diagnostic_report.json`、`validation_report.json`：完整机器可读结果

## 三版本参数格式

### v1：无字段名行式

```text
# PSM-V1
0|300|159.5|89.5|1,0,0,-0.28,0,1,0,-0.01,0,0,1,1|0.015,-0.003|1.06
```

固定 7 列：

`camera_id|focal|principal_x|principal_y|extrinsic[12]|k1,k2|exposure_gain`

约定：

- 外参为行主序 `3x4`，即 `[r00,r01,r02,tx, r10,r11,r12,ty, r20,r21,r22,tz]`
- 世界平面为 `Z=0`
- 由内参、外参和画布尺寸确定性推导单应矩阵
- v1 不携带的画布、羽化半径、颜色空间、重叠权重、曝光模式等从 `config.json` 的 `parse_defaults` 补全
- 文件头可省略；无文件头时按 7 列字段集推断

### v2：字段名键值块

```text
# PSM-V2
canvas_width=320
canvas_height=180
feather_radius=8
color_space=linear_rgb
[view]
camera_id=0
focal=300
principal_x=159.5
principal_y=89.5
extrinsic=...
homography=...
distortion_k1=0.015
distortion_k2=-0.003
exposure_gain=1.06
exposure_mode=linear
color_gain=1,1,1
overlap_weight=1
```

v2 显式携带单应矩阵、逐视角重叠权重、曝光增益模式、逐通道颜色增益和场景级羽化半径。

### v3：结构化 JSON

```json
{
  "version": "v3",
  "canvas": {"width": 320, "height": 180, "color_space": "linear_rgb"},
  "feather_radius": 8.0,
  "views": [
    {
      "camera_id": 0,
      "intrinsics": {"focal": 300.0, "principal": [159.5, 89.5]},
      "extrinsic": [1, 0, 0, -0.28, 0, 1, 0, -0.01, 0, 0, 1, 1],
      "homography": [1, 0, 84, 0, 1, 3, 0, 0, 1],
      "overlap_weight": 1.0,
      "distortion_chain": [
        {"model": "radial", "params": {"k1": 0.015, "k2": -0.003}}
      ],
      "exposure": {
        "curve": {"mode": "linear_gain", "gain": 1.06, "gamma": 1.0},
        "color_gain": [1.0, 1.0, 1.0]
      }
    }
  ]
}
```

v3 的畸变链支持：

- `radial(k1,k2)`
- `scale(sx,sy)`
- `tangential(p1,p2)`

曝光曲线当前支持线性增益、单位增益和 gamma 参数；额外曲线参数会被保留并在降级时明确丢弃。

## 统一内部表示与规范化

三个解析器最终都产生 `SceneParameters` / `ViewParameters`：

- 画布、羽化半径、颜色空间、插值口径、权重舍入容差
- 相机编号、焦距、主点、`3x4` 外参、单应矩阵
- `k1/k2`、完整畸变链
- 曝光增益、曝光模式、颜色增益、曝光曲线
- 重叠权重

所有浮点数按 `12` 位小数规范化，并把 `-0.0` 归一为 `0.0`。语义哈希使用排序键、紧凑分隔符的规范 JSON。

## 迁移与裁决

### 升级补全

- v1 → v2：补全 `homography`、`overlap_weight`、`exposure_mode`、`color_gain`、`feather_radius`、`color_space`
- v1/v2 → v3：继续补全逐视角 `distortion_chain` 与 `exposure_curve`
- 每个补全项写入 `MigrationItem(action="filled", field_path, source, reason, value)`

v1 单应矩阵来源为：

```text
derived:v1-extrinsic+intrinsics+canvas
```

### 降级展平/丢弃

v3 → v2：

- 畸变链保留“第一个 radial 阶段”的 `k1/k2`
- 其余畸变阶段逐项丢弃
- 曝光曲线保留标量 gain；gamma 和额外参数逐项丢弃

v3/v2 → v1：

- 继续应用上述展平规则
- 显式单应矩阵若与相机参数推导值不同则丢弃，因为 v1 重新推导
- 非默认重叠权重、逐通道颜色增益、非默认羽化半径、非 `linear_rgb` 色域均丢弃
- 每项写入 `dropped_fields` 与 `MigrationItem`

### 语义漂移与往返

- 同一语义场景写成 v1/v2/v3 时，解析后的内部表示、全景、掩码必须完全一致
- v1 无法表达的字段导致差异时返回 `E_SEMANTIC_DRIFT`，列出不可还原字段路径
- 往返迁移只比较目标格式可表达的字段；若变化返回 `E_ROUNDTRIP_MISMATCH`，指出首个差异字段

## 几何、增益与融合

### 几何对齐

对每个全景像素 `p`：

1. 用单应矩阵逆变换得到未畸变视角坐标
2. 归一化到相机坐标
3. 按畸变链得到畸变坐标
4. 在源视角 RGB 上做双线性采样

约定：

- 插值口径显式固定为 `bilinear`
- 源图 `x∈[0,W-1]`、`y∈[0,H-1]` 之外为无效
- 中间产物使用 `float64`
- 视角按 `camera_id` 升序处理；同坐标/同权重场景由固定视角顺序和确定性浮点运算裁决

### 增益补偿

- 线性模式：`corrected = clip(input,0,1) / gain`
- gamma：`input^(1/gamma)`
- 颜色增益逐通道应用
- 零增益、负增益、非有限值、非法模式触发 `E_GAIN_INVALID`
- 下游策略：单位增益 + `[1,1,1]` 颜色增益，并记录 `unit_gain_fallback`

### 羽化融合

每个视角权重：

```text
weight = max(clip(source_border_margin / feather_radius, 0, 1), 1e-12)
weight *= overlap_weight
```

然后逐像素归一化，使有效像素权重和为 `1`。检查项：

- 有视角覆盖但总权重为零：`E_OVERLAP_GAP`
- 无效像素 RGB 非零：`E_INVALID_FILLED`
- 有效像素权重和超出容差：`E_WEIGHT_SUM`

三个错误均在 `details.positions` 输出位置样例，并给出计数/容差。

## 四阶段异常注入

注入通过 `injections.json` 或 CLI 参数改变输入/参数，不修改源码。

| 阶段 | 注入 | 典型业务码 | 降级 |
|---|---|---|---|
| parse | 外参少一维、未知版本、数值不可解析 | `E_FORMAT_CORRUPT` / `E_VERSION_UNSUPPORTED` | 无有效参数时终止 |
| geometry | 单应矩阵置零 | `E_HOMOGRAPHY_SINGULAR` | 跳过该视角 `skip_view` |
| gain | 曝光增益置零/负值 | `E_GAIN_INVALID` | 回退单位增益 `unit_gain_fallback` |
| blend | 羽化半径负值 | `E_FEATHER_INVALID` | 半径归零 `radius_zero_fallback` |

每次注入和阶段业务码均产生记录：

- 阶段名
- 业务码
- 视角编号
- 字段路径
- 上下文快照
- 降级类别与 `upstream_code`

无法归因到业务阶段的意外异常进入 `unattributed`，整体标记 `all_attributed=false`。

## 缓存、资源与并发

### 内容寻址缓存

- 键由场景画布、视角语义参数、插值口径、源图像内容 SHA-256 组成
- 缓存文件名为内容键哈希
- `index.json` 记录确定性插入顺序
- 达到 `max_cache_entries` 时淘汰最旧条目并记录 `cache_entry_evicted`
- 损坏条目按缓存未命中处理

### 资源上限

`config/default.json` 可配置：

- `max_memory_bytes`：超限返回 `E_RESOURCE_LIMIT`
- `max_cache_entries`：超限淘汰并标记降级
- `max_parallel_tasks`：请求超出时按 `degrade` 策略串行回退，或返回 `E_RESOURCE_LIMIT`

### 快照

`parameters`、`migration`、`render` 三类快照分别持久化：

- 单调递增版本号
- 整体 payload SHA-256
- 每个顶层字段的校验和

规则：

- 新版本整体原子替换旧文件
- 版本回退：`E_SNAPSHOT_ROLLBACK`
- 同版本同内容：复用，不重写
- 同版本不同内容或文件被篡改：`E_SNAPSHOT_CORRUPT`，报告版本与不一致字段
- 提交锁非阻塞；冲突立即返回 `E_STAGE_BUSY`

## 诊断应用

打开 `artifacts/diagnostic.html`，页面提供：

- 总览与最近业务错误码
- v1/v2/v3 参数原文、全景、有效掩码、重叠权重图
- 迁移补全/展平/丢弃逐项明细
- 往返核对、漂移探针、往返不一致探针
- 四阶段注入记录与阶段过滤
- 资源限制、快照复用/替换/回退/损坏/并发状态
- 自检通过项与差异明细

页面不依赖 CDN 或外部 JavaScript。

## 错误码

| 业务码 | 含义 |
|---|---|
| `E_VERSION_UNSUPPORTED` | 未知参数版本 |
| `E_FORMAT_CORRUPT` | 字段数、必填字段、数值或维度错误；带字段路径与偏移 |
| `E_SEMANTIC_DRIFT` | v1 无法完整表达语义，附不可还原字段 |
| `E_ROUNDTRIP_MISMATCH` | 往返后可表达字段变化，附首个差异 |
| `E_HOMOGRAPHY_SINGULAR` | 单应奇异或不可逆 |
| `E_GAIN_INVALID` | 曝光/颜色增益无效 |
| `E_FEATHER_INVALID` | 羽化半径无效 |
| `E_OVERLAP_GAP` | 覆盖像素没有正权重来源 |
| `E_INVALID_FILLED` | 无效像素被填充 |
| `E_WEIGHT_SUM` | 有效像素权重和不为 1 |
| `E_SNAPSHOT_ROLLBACK` | 快照版本回退 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏、校验和或同版本内容冲突 |
| `E_RESOURCE_LIMIT` | 内存、缓存或并行任务超限 |
| `E_STAGE_BUSY` | 快照提交并发冲突 |
| `E_INCONSISTENT` | 跨模块自检不一致 |
| `E_UNATTRIBUTED` | 未归因意外异常 |

## 测试与实测

运行：

```bash
PYTHONPATH=src python -m pytest
```

当前测试集：`26 passed`。

验证环境：

- Python 3.10.12
- NumPy 1.26.4
- 验证工作区：Ubuntu 22.04 / WSL
- 代码只使用标准库与 NumPy，并为 Windows 提供 `.bat` 和等价命令

固定样本实测结果：

- 画布：`320x180`，3 视角，种子 `20261123`
- 语义哈希：`ed1340b37691ef092cd07d199c441a7a287e172dd99de8c985f1f83891c09717`
- 全景 RGB 原始哈希：`d0be4aa9f696d8031dedf0dd4923c652eb7e778dd91aa5ab009a145fd27982a8`
- 无效掩码哈希：`d982ba67451fb973016c5c3cbd728c9923e96df777f0cdcc316032a98f67d1f6`
- `panorama.png` SHA-256：`956d75449bc1d865faa60a96b47943ee5ace4ce7a03be1a10552da0aaaa1619c`
- v1/v2/v3 上述哈希完全一致
- 清空缓存后固定种子复跑，所有几何/全景/掩码/权重/PNG 哈希一致
- 有效像素 `57592`，无效边界像素 `8`，无效像素未填充
- 注入业务码：`E_FORMAT_CORRUPT`、`E_HOMOGRAPHY_SINGULAR`、`E_GAIN_INVALID`、`E_FEATHER_INVALID`
- 漂移探针：`E_SEMANTIC_DRIFT`，列出 `$.feather_radius` 与畸变链差异
- 往返不一致探针：`E_ROUNDTRIP_MISMATCH`，首个字段 `$.views[0].focal`
- 快照：同版本复用、版本 2 整体替换、回退、篡改、并发忙均按预期

完整结果见 `artifacts/validation_report.json`，可视化见 `artifacts/diagnostic.html`。

## 项目结构

```text
config/default.json             默认配置与资源上限
src/panoweave/formats.py        v1/v2/v3 解析与序列化
src/panoweave/model.py          统一内部模型与规范哈希
src/panoweave/migration.py      双向迁移、漂移、往返裁决
src/panoweave/geometry.py       单应推导、畸变与逆畸变
src/panoweave/pipeline.py       四阶段渲染与融合校验
src/panoweave/injections.py     注入、归因、降级账本
src/panoweave/cache.py          内容寻址缓存
src/panoweave/resources.py      内存/并行资源决策
src/panoweave/snapshots.py      版本化整体替换快照
src/panoweave/samples.py        固定种子样本生成
src/panoweave/diagnostics.py    全链路诊断与 HTML 报告
src/panoweave/cli.py            命令行入口
tests/                          26 个单元/集成测试
samples/                        固定种子样本与三版本参数
artifacts/                      诊断输出、缓存、快照与报告
```

## 设计边界

- 重采样插值当前只实现显式 `bilinear`；其他口径返回 `E_FORMAT_CORRUPT`
- v1 本身不携带画布、色域、羽化半径、重叠权重和逐通道增益，需由解析配置提供；这是 v1 表达能力的边界
- v3 的非径向畸变阶段和非线性曝光参数可以读取，但降级到 v1/v2 时按规则展平/丢弃并报告
- 资源中的“并行任务数”是确定性提交预算；当前实现按固定视角顺序串行计算，避免跨平台并行归约差异
- 确定性保证针对相同代码、Python/NumPy 环境和固定样本；跨 NumPy 大版本不承诺位级一致
