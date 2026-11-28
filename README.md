# 叙事高斯透明合成工作台

本项目是一个可本地运行、确定性可复现的 Python 工作台，用于在接入游戏引擎前检查
半透明叙事元素的 schema 归一、相机投影、透明排序、alpha 合成与排序违例。场景不依赖
外部资产或网络，所有工程、相机轨道和投影参考均由固定种子生成。

## 环境与启动

要求 Linux/WSL Ubuntu、Python 3.10+ 和 NumPy：

```bash
python3 -m pip install numpy
python3 run_workbench.py
```

无子命令时等价于完整流水线：

```bash
python3 run_workbench.py run-all
```

也可使用模块入口或安装后的命令：

```bash
python3 -m narrative_workbench run-all
python3 -m pip install -e .
narrative-workbench run-all
```

常用命令：

```bash
# 只生成固定种子场景、相机和参考椭圆
python3 run_workbench.py generate --seed 20261128 --frames 12

# 归一三份 schema，输出归一模型和字段/语义问题
python3 run_workbench.py normalize

# 只渲染 v1，一次最多提交 3 帧；再次执行自动从第 4 帧续算
python3 run_workbench.py render --versions v1 --max-frames 3

# 独立有限差分投影、独立排序与独立混合复算
python3 run_workbench.py verify

# 三版本归一与逐帧渲染对照
python3 run_workbench.py compare

# 查看持久化提交位置
python3 run_workbench.py status
```

## 默认场景

- 种子：`20261128`
- 相机轨道：12 帧，围绕叙事中心从 `-54°` 到 `+54°` 采样
- 画面：`160 x 96`，垂直视场角 `50°`，近/远裁剪面 `0.08 / 20`
- 有效元素：15 个，包含背景光晕、彩窗玻璃、烟雾、贴片粒子、前景光晕和一个用于
  展示剔除证据的远离场景光探针
- 叙事图层：`backdrop_glow`、`stained_glass`、`smoke`、`particles`、`light_glow`
- 每个版本额外带 1 条故意损坏的审计记录，用于验证诊断：
  - v1：缺少 `center`
  - v2：四元数只有 3 个分量
  - v3：引用未声明的 `layer_id`

损坏审计记录不会进入可渲染集合；它们作为 schema 审计问题单独报告，不与有效元素的
归一差异混合。

## 结果文件

完整运行后，`results/` 中包含：

| 路径 | 内容 |
| --- | --- |
| `scene/scene_v1.json` | v1：Euler 角度 + 三轴缩放 |
| `scene/scene_v2.json` | v2：四元数 + 三轴缩放、对象字段名、hex 颜色 |
| `scene/scene_v3.json` | v3：六分量协方差、RGBA、数字图层 id |
| `scene/camera.json` | 逐帧轨道相机 |
| `scene/reference_ellipses.json` | 与生成原语一致的屏幕椭圆参考参数 |
| `normalized/v1.json` … | 统一中心、旋转、缩放、协方差、颜色、不透明度、深度曲线、图层 |
| `normalized/issues_*.json` | 字段缺失、编码错误、图层不可映射等诊断 |
| `normalized/normalization_diff.json` | 三版本有效归一模型差异与独立 schema 审计问题 |
| `frames/<version>/frame_XXXX.json` | 投影、裁剪、绘制顺序、可疑对、违例像素对、检查项 |
| `frames/<version>/frame_XXXX.npz` | `rgba`、逐像素参考、图层近似、覆盖/违例掩码 |
| `frames/<version>/frame_XXXX.png` | straight-alpha RGBA 合成图 |
| `frames/<version>/checkpoint.json` | 已提交帧、哈希、排序摘要和续算位置 |
| `verification.json` | 独立复算结果 |
| `comparison.json` | 三版本逐帧像素、顺序、违例与归因 |
| `workbench_summary.json` | 完整流水线汇总 |

`frame_XXXX.npz` 的键：

- `rgba`：按主排序链路逐原语由远及近的 straight-alpha 合成
- `pixel_reference_rgba`：按逐像素中心平面深度排序的审计参考
- `layer_approximation_rgba`：层内逐像素排序、层间按平均质心深度排序的近似
- `coverage_mask`：实际 alpha 覆盖
- `sorting_violation_mask`：逐原语顺序与逐像素深度顺序冲突的像素

## Schema 归一

| 语义 | v1 | v2 | v3 |
| --- | --- | --- | --- |
| 元素数组 | `elements` | `primitives` | `items` |
| 标识 | `id` | `uid` | `key` |
| 中心 | `center` | `position.{x,y,z}` | `mu` |
| 旋转/协方差 | `rotation_deg` + `scale` | `rotation_quaternion_wxyz` + `sigmas` | `covariance` |
| 协方差编码 | `R diag(s²) Rᵀ` | `R diag(s²) Rᵀ` | 可声明 `xx,yy,zz,xy,xz,yz` |
| 基础不透明度 | `opacity` | `alpha` | `base_opacity` |
| 颜色 | `rgb` | `tint` hex | `rgba` |
| 深度曲线 | `depth_curve.control_points` | `attenuation.keys` | `depth_alpha.points` |
| 图层 | 名称/对象 | `story_layer` 名称 | `layer_id` 数字 |

所有有效记录都会归一为按特征值降序的确定性主轴表示：

- 中心、3x3 协方差、旋转矩阵、规范化 `wxyz` 四元数、三轴标准差
- 颜色 RGB、基础不透明度、叙事图层
- 深度曲线在 `0, 0.25, 0.5, 0.75, 1` 的采样
- 最薄主轴方向，用作逐像素中心平面深度

四元数先归一化并规范为 `w >= 0`；协方差特征分解后主轴符号和行列式也固定，避免同一
几何因等价编码产生伪差异。

## 投影与裁剪

相机视图坐标为 `x` 右、`y` 上、`z` 向前。像素中心 `(x+0.5, y+0.5)` 的透视投影
Jacobian 为：

```text
u = W/2 + W*x/(2*tan_x*z)
v = H/2 - H*y/(2*tan_y*z)
Σ_screen = J (R Σ_world Rᵀ) Jᵀ
```

对 2x2 `Σ_screen` 做特征分解，输出：

- 椭圆中心
- 半长轴/半短轴 `sqrt(eigenvalue)`
- 完整轴长、主轴方向、`[0,180)` 朝向角
- 3-sigma 支撑包围盒
- 视口裁剪边和裁剪依据

近裁剪策略是保守且定义的：质心必须在近/远平面之间，且以深度方向 3-sigma 延伸也完全
位于近/远平面内；否则整个元素不进入混合。部分越过视口的元素只对落入像素中心的区域
采样。每帧记录可见、剔除和部分越界的原因。

## 排序、合成与校验

主绘制顺序：

```text
(-centroid_depth, layer_index, stable_index, element_id)
```

即全局由远及近；深度完全相同时按图层和源稳定序号打破平局。每个可见元素按深度归一值
查询其分段线性曲线，实际不透明度为 `base_opacity * curve_gain`，再按高斯二次型
`exp(-q/2)` 形成像素 alpha，并裁掉 `q > 9` 的尾部。

合成使用 straight-alpha、非预乘 RGBA：

```text
out_a = src_a + dst_a * (1 - src_a)
out_rgb = (src_rgb*src_a + dst_rgb*dst_a*(1-src_a)) / out_a
```

每帧还会按中心平面深度逐像素重排作为审计参考。若某像素上两个显著覆盖原语的实际深度
顺序与逐原语绘制顺序相反，就会：

- 将像素写入 `sorting_violation_mask`
- 记录相关原语对、冲突深度范围、深度差、违例包围盒与像素样例
- 输出深度接近、协方差深度区间重叠、投影相交、逐像素反转等可疑原因

当逐原语顺序无法同时满足逐像素顺序时，生成图层近似图并记录其与逐像素参考的差异像素
数、包围盒、比例和最大通道误差。主 `rgba` 仍保持可审计的逐原语绘制顺序，不静默替换。

## 三版本对照与归因

`compare` 分别输出：

- `normalized_differences`：有效归一元素字段差异
- `schema_audit_issues`：字段缺失、编码错误、图层不可映射等交付问题
- `render_differences`：逐帧 RGBA 差异
- `ordering_differences`：逐帧绘制顺序差异
- `sorting_strategy_differences`：归一模型一致但顺序不同的差异
- `validation_differences`：可疑对或违例对集合差异

分类规则明确：归一模型的语义差异是上游根因；若同时出现顺序差异，顺序项标记为
`normalization_upstream`，不会和独立的 `sorting_strategy` 混为一项。

## 中断续算

每帧的 JSON、NPZ、PNG 先完成原子写入，之后才以临时文件 + `os.replace` 原子提交
`checkpoint.json`。检查点保存：

- 已提交帧索引与文件 SHA-256
- RGBA 数组摘要与排序摘要
- `next_frame`
- 续算次数和启动帧

NPZ 使用固定 ZIP 时间戳和排序后的数组条目写入，因此同一帧显式重放会得到字节一致的
JSON、NPZ、PNG 和检查点文件，而不仅是数值一致。

重启时只从 `next_frame` 开始。崩溃后可能留下尚未进入检查点的孤儿帧文件，但不会被
视为已提交；下次渲染会安全覆盖。可使用：

```bash
python3 run_workbench.py render --versions v2 --max-frames 1
python3 run_workbench.py render --versions v2 --max-frames 1
python3 run_workbench.py status
```

`--reset-version` 才会显式丢弃所选版本已提交帧。

## 独立复算与测试

`verify` 不调用主投影/合成函数，而是从归一 JSON 重新读取原语，用中心有限差分
Jacobian 重算屏幕协方差与椭圆，独立重排质心深度顺序，独立执行 straight-alpha
混合与逐像素深度排序，并独立扫描显著 alpha 重叠对的顺序反转。

运行自动测试：

```bash
python3 -m unittest discover -v
```

测试覆盖协方差分解、三 schema 有效模型等价、字段/图层审计、参考椭圆一致性、
36 帧独立复算、三版本像素一致、断点续算以及未提交孤儿帧覆盖。
