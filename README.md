# FlightFusion

面向多旋翼飞控姿态融合的本地领域组件与完整示例。项目从固定种子生成陀螺仪、加速度计、磁力计序列和真值姿态四元数，覆盖：

- v1 竖线位置字段、v2 命名字段、v3 嵌套结构三种传感器描述的互操作。
- v1↔v2↔v3 相邻版本迁移，逐项记录补全来源、展平映射和降级丢弃/对角近似。
- 去偏、标定、轴序重排和确定性的时间对齐。
- 带显式增益、加速度计/磁力计权重、重力常数和陀螺零偏估计窗口的互补滤波。
- 有向无环计算图、内容寻址缓存、缓存键溯源和局部失效重算。
- 描述解析、预处理、融合、输出四阶段异常注入与上游业务码传播。
- 单调版本化整体快照、校验和、非阻塞并发提交控制和三类快照错误。
- 纯标准库本地 Web 诊断台，无 Docker、无外部服务、无未授权网络访问。

## 快速开始

环境：Python 3.10+、NumPy、pytest。

```bash
python3 -m flightfusion generate-data --force
python3 -m flightfusion demo --output artifacts/demo_result.json
pytest -q
python3 -m flightfusion serve --host 127.0.0.1 --port 8765
```

浏览器访问 `http://127.0.0.1:8765`。页面包含计算图拓扑、缓存命中/重算状态、三格式原文、迁移补全与丢弃明细、姿态与残差曲线、快照事件和最近业务错误码，并提供“运行完整演示”和“四阶段异常演示”按钮。

## 项目结构

```text
flightfusion/
  formats.py       三格式分派、读取、行号/字段级错误
  models.py        统一描述、数据集、融合配置、快照状态
  migration.py     v1↔v2↔v3 迁移与补全/丢弃明细
  preprocess.py    轴序、标定、时间对齐和确定性索引
  fusion.py        互补滤波、按传感器贡献、输出统计
  graph.py         DAG、缓存键、局部失效和节点清单
  cache.py         本地内容寻址缓存与按键查询
  injection.py     四阶段运行参数注入框架
  snapshot.py      整体替换快照与非阻塞提交
  demo.py          完整验收演示编排
  app.py           本地 HTTP 应用
  web/index.html   零外部依赖诊断面板
data/
  flight_imu.json  固定种子传感器序列与真值四元数
  formats/         v1/v2/v3 三份描述样本
  fusion_config.json
  invalid/         未知版本、字段数、缺字段、非数值、轴序、量程、损坏 JSON
tests/             40 项确定性回归测试
artifacts/         演示与验收结果
```

## 确定性样本

`data/flight_imu.json` 由默认种子 `20261123` 生成，时长 4 秒：

| 数据 | 采样率 | 样本数 |
|---|---:|---:|
| gyro | 200 Hz | 801 |
| accel | 100 Hz | 401 |
| mag | 50 Hz | 201 |
| truth quaternion | 400 Hz | 801 |

默认活动描述格式为 `gyro=v1`、`accel=v2`、`mag=v3`，从而在同一融合任务中并存三种格式。

融合配置 `data/fusion_config.json` 显式包含：

```json
{
  "config_version": "fusion-1.0.0",
  "complementary_gain": 0.075,
  "accel_weight": 0.55,
  "mag_weight": 0.22,
  "gravity_constant": 9.80665,
  "gyro_bias_window": 24,
  "output_rate_hz": 100.0,
  "mag_reference": [24.0, 0.0, 42.0],
  "downgrade_policy": {
    "axis_coupling": "diag",
    "noise_model": "drop",
    "temperature_compensation": "drop"
  }
}
```

## 三格式与统一模型

### v1

```text
# FORMAT=v1
gyro|2000|200|x,y,z|3
accel|16|100|y,x,z|2
mag|4900|50|x,z,y|4
```

字段固定为 `sensor_id|range|sample_rate|axis_order|calibration_version`。读取器同时支持显式 `# FORMAT=v1` 头和按竖线字段集自动分派。

### v2

```text
# FORMAT=v2
sensor_id,range,sample_rate,axis_order,calibration_version
gyro,2000,200,"x,y,z",3
```

除五个基础字段外，读取器支持标定偏置/比例、9 个轴耦合字段、噪声参数和温度系数的扁平可选列，因此 v3 可以无损展平为 v2。

### v3

顶层携带 `version: 3`，每个传感器包含嵌套 `calibration`、`axis_coupling`、`noise_model` 和 `temperature_compensation`。

三种读取路径最终都生成不可变 `SensorDescription`，统一包含：

- `sensor_id`、`sensor_type`、量程、采样率。
- 轴序、轴映射（canonical axis → raw index）。
- 标定版本、偏置、比例。
- 轴耦合矩阵、噪声模型、温度补偿系数。
- 来源字段集合与逐字段 provenance。

### 错误规则

| 条件 | 业务码 | 定位 |
|---|---|---|
| 未知格式版本 | `E_VERSION_UNSUPPORTED` | 行号、`format_version`/`version` |
| v1 竖线字段数不符 | `E_FORMAT_CORRUPT` | 行号、`row` |
| 必填字段缺失 | `E_FORMAT_CORRUPT` | 行号/字段路径、字段名 |
| 数值不可解析 | `E_FORMAT_CORRUPT` | 行号、字段名 |
| 量程非正 | `E_FORMAT_CORRUPT` | 行号、`range` |
| 轴序不是 x/y/z 的排列 | `E_AXIS_MAPPING_INVALID` | 行号、`axis_order` |

## 迁移语义

- `v1 → v2`：补充命名字段和标定来源，逐项记录 `v1.positional_field`、`calibration_registry`。
- `v2 → v3`：缺失轴耦合补单位阵，缺失噪声模型和温度系数补声明默认值；每个矩阵元素、噪声字段和温度系数都记录补全来源。
- `v3 → v2`：轴耦合、噪声和温度补偿展平为 v2 可选列，不丢失可表达数据。
- `v2 → v1`：v1 无法表达轴耦合和噪声模型。默认对耦合使用 `diagonal_approximation`，对噪声使用 `drop`，并输出字段、动作、原因和原值；支持显式切换耦合 `drop`/`diag`。温度补偿和自定义标定覆盖同样记录丢弃。
- 迁移后的行为指纹覆盖传感器类型、轴序/映射、偏置、比例、标定版本和采样率；测试验证相邻及多跳迁移的标定/去偏行为一致。

## 计算图与缓存

```text
raw sensor digest + description
            │
       parse:<sensor>
            │
     preprocess:<sensor>
            │
   fusion:<sensor>  (每传感器贡献)
            │
      fusion:merge
            │
     output:attitude
```

每个节点写入 `.cache/flightfusion-demo/<cache-key>.json`，条目包含：

- `key`、`node_id`、`node_type`、`content_hash`。
- `depends_on` 上游节点清单。
- `key_material` 完整键材料。
- 可重放 payload。

键材料按节点语义纳入：

- 传感器原始序列摘要。
- 描述格式版本、标定版本、轴序映射、描述内容哈希。
- 上游产物哈希。
- 时间网格摘要。
- 融合配置版本和配置内容哈希（合并/输出节点）。
- 每传感器实际消费的权重、重力常数或磁参考（贡献节点）。
- 仅作用于对应阶段的运行时注入摘要。

同一键必须复用缓存；不同键重算。API 查询入口：

```bash
python3 -m flightfusion cache-get <key>
# GET /api/cache?key=<key>
```

## 增量失效

演示和测试覆盖四类更新：

| 更新 | 受影响传感器 | 典型重算节点 |
|---|---|---|
| 磁力计噪声模型调整 | `mag` | `parse:mag` → `preprocess:mag` → `fusion:mag` → merge → output |
| 加速度计轴序修正 | `accel` | `parse:accel` → `preprocess:accel` → `fusion:accel` → merge → output |
| 陀螺仪标定版本 v3→v4 | `gyro` | `parse:gyro` → `preprocess:gyro` → `fusion:gyro` → merge → output |
| 仅加速度计融合权重变化 | `accel` | 仅 `fusion:accel`、`fusion:merge`、`output:attitude` |

权重变化时，所有 parse/preprocess 节点以及 `fusion:gyro`、`fusion:mag` 都复用；局部增量输出与强制全量重算逐字节一致。

## 时间对齐与融合

预处理将各传感器按原始时间排序，重复时间戳按样本序号升序选择最小索引，并以配置的 `output_rate_hz` 建立共同时间网格。对齐采用确定性零阶保持；同时间戳的选择顺序固定为时间、传感器编号、样本序号。

融合步骤：

1. 从首帧加速度计/磁力计构造初始姿态。
2. 陀螺角速度转换为 rad/s 并积分四元数。
3. 加速度计和磁力计单位向量与预测方向叉积，按显式增益和权重修正姿态。
4. 滑动窗口平均修正量形成陀螺零偏估计，并用于后续积分。
5. 输出逐时刻四元数、欧拉角、方向残差、加速度计 m/s² 残差、磁残差和统计。

每个融合输出同时携带 `sensor_versions`（格式版本、标定版本、轴序、描述哈希）和 `config_version`。

## 四阶段异常注入

注入只通过 CLI/API 参数触发，不修改源码：

| 阶段 | 示例注入 | 业务码 | 降级 |
|---|---|---|---|
| parse | `parse.axis_missing@mag` | `E_AXIS_MAPPING_INVALID` | 轴序回退 identity |
| preprocess | `preprocess.calibration_rollback@gyro` | `E_CALIBRATION_ROLLBACK` | 保留活动标定 |
| preprocess | `preprocess.sample_rate_nonpositive@accel` | `E_SAMPLE_RATE_INVALID` | 使用序列采样率 |
| fusion | `fusion.gain_out_of_range` | `E_GAIN_OUT_OF_RANGE` | 增益裁剪到 `[0,1]` |
| fusion | `fusion.weight_out_of_range@accel` | `E_WEIGHT_OUT_OF_RANGE` | 权重裁剪到 `[0,1]` |
| output | `output.residual_missing` | `E_OUTPUT_FIELD_MISSING` | 从融合产物恢复 |

每个下游阶段会生成携带 `upstream_code` 的 `E_UPSTREAM_DEGRADED` 记录，因此可以从输出一直追溯到根因。

```bash
python3 -m flightfusion demo --inject parse.axis_missing@mag \
  --inject fusion.gain_out_of_range --output artifacts/injected_demo.json
```

## 快照与并发

`SnapshotStore` 保存不可变整体快照：

- 版本号必须单调递增。
- 校验和为 canonical payload 的 SHA-256。
- 更高版本到达时整体替换，不合并旧 payload。
- 同版本同校验和返回 `unchanged`。
- 旧版本或同版本不同内容返回 `E_SNAPSHOT_ROLLBACK`。
- 提供校验和与 payload 不符返回 `E_SNAPSHOT_CORRUPT`。
- 提交锁使用非阻塞获取；竞争失败立即返回 `E_STAGE_BUSY`，不等待。

演示依次创建 v1，尝试损坏提交、回退、并发冲突，再以 v2–v5 整体替换噪声、轴序、标定和权重的独立快照。

## 本地应用 API

- `GET /`：诊断面板。
- `GET /api/state`：当前完整演示结果。
- `POST /api/demo`：重新运行完整演示。
- `POST /api/inject`：带四阶段注入重新运行。
- `GET /api/cache?key=<key>`：按缓存键查询条目、内容哈希、键材料和 payload。
- `GET /healthz`：健康检查。

## 实际验证结果

执行环境：Python 3.10.12、NumPy 1.26.4、pytest 8.3.3。

```text
$ pytest -q
40 passed

$ python3 -m flightfusion demo --output artifacts/demo_result.json
seed=20261123
initial nodes=11
second_build_cache_all_reused=True
attitude_error_deg_mean=0.4732867656
attitude_error_deg_p95=0.7668303534
attitude_error_deg_max=0.8024645483
fusion_residual_mean=0.0000043869
incremental_equals_full=True
weight_change_sensor_isolation=True
four_stage_injection_chain=True
snapshot version=5
snapshot events: created, unchanged, E_SNAPSHOT_CORRUPT,
                 E_SNAPSHOT_ROLLBACK, E_STAGE_BUSY, replaced×4
```

完整结果见：

- `artifacts/demo_result.json`：节点、缓存键材料、三格式原文、迁移明细、增量清单、注入诊断、快照事件和曲线数据。
- `artifacts/acceptance_report.json`：验收摘要、各增量场景结果、业务码与返回码统计。
- `artifacts/cache_query_example.json`：按最终 `output:attitude` 缓存键查询到的完整条目。

## 约束

- 不使用 Docker 或其他容器环境。
- 运行时不访问外部服务；Web 页面不加载外部 CDN、字体或脚本。
- 所有样本、缓存、快照、异常注入均由本地输入或参数驱动。

