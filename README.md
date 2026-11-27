# 客厅立体显示评估与增量重算内核

这是一个仅依赖 Python 标准库的本地命令行项目。它读取客厅陈设工程，
逐个观看位姿、逐个屏幕分块计算双目水平视差、重构深度、舒适度与
深度失真，核对关键家具间距和声学采样对齐，并支持参数修改后的
依赖裁剪增量重算、全量一致性比对、版本化快照查询和快照重载。

仓库内提供固定种子 `20261128` 的确定性样本：

- `examples/living_room.json`：基准方案。
- `examples/living_room.adjusted.json`：移动茶几、落地灯和一把座椅后的调参方案。
- `snapshots/living-room-stereo-demo/v0001.json`：基准方案的 v0001 快照。

环境要求：Linux/WSL、Python 3.10+，无需安装第三方包，无需网络服务。

## 快速开始

```bash
# 全量测试
python3 -m unittest discover -s tests -v

# 校验工程
python3 -m livingroom_stereo validate examples/living_room.json

# 全量评估，JSON 输出到 stdout
python3 -m livingroom_stereo evaluate examples/living_room.json

# 全量评估，写入文件
python3 -m livingroom_stereo evaluate examples/living_room.json -o artifacts/evaluation.json

# 增量重算、保存比较结果和下一轮状态
python3 -m livingroom_stereo incremental \
  examples/living_room.json \
  examples/living_room.adjusted.json \
  --write-state artifacts/adjusted.state.json \
  -o artifacts/incremental.json

# 保存快照
python3 -m livingroom_stereo snapshot save \
  examples/living_room.json \
  --store snapshots \
  --version v0001

# 按位姿和分块行/列闭区间查询
python3 -m livingroom_stereo snapshot query \
  --store snapshots \
  --project-id living-room-stereo-demo \
  --version v0001 \
  --pose seat.sofa.center \
  --rows 0:1 \
  --cols 2:4

# 从快照重载基准并做增量，同时验证与全量一致
python3 -m livingroom_stereo incremental \
  examples/living_room.json \
  examples/living_room.adjusted.json \
  --restore-snapshot snapshots \
  --snapshot-version v0001 \
  -o artifacts/incremental-from-snapshot.json
```

也可以直接运行 `bin/lsr3d`，参数与 `python3 -m livingroom_stereo`
相同。所有成功结果都是 UTF-8、键排序、浮点规范化后的稳定 JSON；
输出不包含运行时间等非确定字段。

## 工程格式

完整示例见 `examples/living_room.json`。核心字段如下：

```json
{
  "schema_version": 1,
  "project_id": "living-room-stereo-demo",
  "seed": 20261128,
  "room": {"width_m": 5.8, "depth_m": 6.2, "height_m": 2.8},
  "screen": {
    "id": "screen.main",
    "center_m": [2.9, 5.95, 1.3],
    "width_m": 2.9,
    "height_m": 1.5,
    "resolution": {"width_px": 1600, "height_px": 900},
    "blocks": {"cols": 8, "rows": 5},
    "samples_per_block": {"cols": 3, "rows": 3},
    "right": [1.0, 0.0, 0.0],
    "up": [0.0, 0.0, 1.0],
    "normal": [0.0, -1.0, 0.0],
    "design_viewing_distance_m": 4.4,
    "zero_parallax_distance_m": 4.15
  },
  "stereo": {"interocular_distance_m": 0.064},
  "assessment": {
    "comfort_parallax_px": {"min": -16.0, "max": 10.0},
    "depth_distortion_threshold": 0.1,
    "score_weights": {
      "overflow_ratio": 0.65,
      "overflow_magnitude": 0.35
    },
    "min_comfortable_block_ratio": 0.3,
    "viewing_zone_radius_m": 1.35
  },
  "viewing_poses": [],
  "furniture": [],
  "relationships": [],
  "acoustic_samples": []
}
```

坐标约定：`x` 为房间宽度方向，`y` 为进深方向，`z` 为高度方向。
屏幕局部坐标由 `right`、`up`、`normal` 定义；分块和采样均为
行优先、从左到右、从屏幕下沿到上沿，分块 `row=0` 位于下沿。

必须满足：

- 房间和屏幕尺寸为正数，屏幕完全位于房间内，屏幕法线朝向房内。
- 家具包围体 `min < max` 且位于房间内。
- 观看位姿位于房间内，`forward`/`up` 为单位正交向量，并朝向屏幕。
- 每个观看位姿的 `furniture_id` 引用已有家具。
- 关键关系引用已有家具，或使用 `screen:<screen-id>` 引用屏幕。
- 声学采样点位于房间平面坐标内，可选引用已有观看位姿。
- 水平分辨率可被分块列数整除，垂直分辨率可被分块行数整除。
- 采样网格不超过显示分辨率，舒适区间包含 0 且下限小于上限。

校验错误统一输出：

```json
{
  "valid": false,
  "error_count": 1,
  "issues": [
    {
      "code": "E_GRID_RESOLUTION_MISMATCH",
      "path": "$.screen.blocks.cols",
      "message": "horizontal resolution must be divisible by block columns"
    }
  ]
}
```

错误清单按 `(path, code, message)` 稳定排序。主要错误码包括
`E_REQUIRED`、`E_TYPE`、`E_RANGE`、`E_REFERENCE`、
`E_GRID_RESOLUTION_MISMATCH`、`E_SCREEN_BASIS` 等。

## 投影与舒适度模型

对每个观看位姿和每个屏幕采样点：

1. 从眼间中心向屏幕采样点发射视线，对家具 AABB 和房间边界做射线
   求交，取最近命中点作为该采样点的标称场景点。观看者所坐家具自身
   不参与遮挡，避免座椅包围体吞掉视线起点。
2. 用 `±IPD/2` 的两只眼把场景点投影回屏幕平面，得到右眼、左眼投影
   的屏幕水平坐标差；再减去零视差平面对应的投影差：

   ```text
   p_raw = (x_right - x_left) - (x_right,zero - x_left,zero)
   p_px  = p_raw / (screen_width_m / horizontal_resolution_px)
   ```

3. 显示屏离散像素视差另行输出为 `display_parallax_px`，按远离零的
   半像素规则量化。
4. 在正视、对齐的常见情况下：

   ```text
   p_m = b * D * (1/Z0 - 1/Z)
   Z_rec = 1 / (1/Z0 - p_m / (b_effective * D))
   ```

   其中 `b` 为双目间距，`D` 为观看位姿到屏幕平面的垂距，`Z0` 为
   零视差平面距离，`Z` 为标称场景深度。

符号约定：

- **负视差**：交叉/会聚方向，重构点在零视差平面之前。
- **正视差**：非交叉/发散方向，重构点在零视差平面之后。

深度失真率为 `abs(Z_rec - Z_nominal) / Z_nominal`。分块掩码同时标记
视差越界和深度失真超阈值：

- `parallax_overlimit_mask`：视差越界采样掩码。
- `depth_overlimit_mask`：深度失真掩码。
- `overlimit_mask`：二者并集。
- `overlimit`：该分块任一样本超限。

舒适度评分范围为 0 到 100，100 最舒适：

```text
score = 100 * (1 - (
    w_ratio * combined_overlimit_sample_ratio
  + w_magnitude * mean_combined_normalised_excess
))
```

超出量对视差按舒适区间宽度归一化，对深度按阈值归一化；权重和必须
为 1。`primary_cause` 只由视差越界方向和累计越界幅度决定，取值为
`within_comfort`、`convergence_overflow`、`divergence_overflow` 或
`mixed_overflow`。深度失真另列在 `limit_causes` 中。

## 布局比例与声学对齐

`layout` 包含：

- `relationships`：沙发到电视柜、茶几到屏幕等关键间距。每个观看位姿
  输出标称距离、立体感知距离、偏差、相对偏差以及贡献分块。
- `furniture_proportions`：每个观看位姿下每个家具包围体的重构尺度、
  标称/感知三轴尺寸、三轴比例和贡献分块。
- `distortion_regions`：相对偏差超过深度阈值的位姿与分块定位。

家具锚点从观看位姿沿径向按该家具的观测深度尺度缩放；屏幕锚点使用
`Z0 / D` 映射。每个关系条目都明确列出贡献位姿和分块。

`alignment` 把视觉可行区域与声学覆盖区域放到房间平面坐标比较：

- 视觉区域由非零的舒适分块比例聚合为各观看位姿的平面圆盘。
- 声学区域为各采样点位置与 `coverage_radius_m` 的圆盘并集。
- 在每个视觉圆盘的确定性探针上检查声学缺口，输出
  `visual_only`。
- 在每个声学采样点上检查视觉可达性，输出 `acoustic_only`。
- 每个失配均含采样点编号、类型、平面坐标和到另一区域边界的距离。

## 增量重算

`incremental` 自动对基准工程与变更工程做结构化 diff，并按依赖裁剪：

- 房间、屏幕、双目参数、舒适/深度阈值变化：全部位姿、全部分块。
- 观看位姿变化：该位姿全部分块；新增位姿全算；删除位姿从结果移除。
- 家具变化：把家具旧/新 AABB 从每个位姿投影到屏幕，加上一格边界，
  只重算投影覆盖的分块；投影不安全时保守回退为该位姿全算。
- 声学采样变化：不重算光学分块，只重建对齐摘要。
- 关系变化：不重算光学分块，只重建布局摘要。

结果中的 `affected` 列出变更路径、受影响位姿、分块行区间、原因、
重算数量和沿用数量。未受影响分块直接沿用基准结果对象，不重新计算。
`--state` 用于载入与命令行首个基准工程匹配的既有状态；
`--write-state` 则把本次变更工程及其结果写成下一轮可用的状态。

`consistency` 会把增量结果与变更工程的全量结果逐位姿、逐分块、逐字段
比较。若不一致，`first_divergence` 给出首个位置：

```json
{
  "pose_id": "seat.sofa.center",
  "block_row": 1,
  "block_col": 2,
  "field": "parallax_statistics.mean_px",
  "incremental": 3.1,
  "full": 3.2
}
```

CLI 默认执行全量校验；`--no-verify` 可关闭比较以获得更快的纯增量
结果。重复执行同一个基准/变更组合得到完全相同的评估和受影响清单。

## 快照

快照按方案版本存储在：

```text
<store>/<project_id>/<version>.json
<store>/<project_id>/index.json
```

每个版本文件持久化完整工程、舒适度评分、三类超限掩码、
`primary_cause`、布局摘要和声学对齐摘要。版本号和工程 ID 经过安全
路径校验。快照写入是原子的，重复写入同一版本和同一结果得到相同内容。

`snapshot query` 的 `--rows` 和 `--cols` 都使用包含端点的
`START:END` 语义。可用 `--pose` 重复筛选多个位姿。查询结果同时提供
按位姿分组的块和扁平 `blocks` 列表。

`incremental --restore-snapshot STORE --snapshot-version VERSION` 会从
快照恢复基准工程和完整评估，再执行增量。测试覆盖了“快照重载增量”、
“状态重载增量”和“未重载直接增量”三条路径，结果逐字段一致。

## 输出与测试

全量评估主要字段：

- `summary`：位姿数、分块数、舒适/超限分块数和平均评分。
- `overlimit_block_mask`：所有位姿聚合后的分块超限网格。
- `poses[].blocks[]`：逐位姿逐分块的视差/深度统计、掩码、评分、
  成因、家具命中与深度尺度。
- `layout`：关键间距、比例失真区域和贡献位姿/分块。
- `alignment`：视觉/声学圆盘、失配列表、类型与距离。

测试命令：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖确定性生成、路径化稳定错误、视差符号与公式、重构深度、
阈值掩码、评分范围、布局贡献、两类声学失配、增量沿用、全量一致性、
首个分歧定位、快照历史查询和快照重载增量一致性。
