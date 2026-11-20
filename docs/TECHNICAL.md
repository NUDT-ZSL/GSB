# 技术说明：本地图表数值读取引擎

## 范围与约束

- 全部流程本地运行，不访问任何外部服务或账号；不使用 Docker。
- 运行环境：Python 3.10+（目标 3.11，代码不使用 3.11 独有特性）、NumPy、Pillow。
- 所有文件读写均为 UTF-8、`\n` 换行；Windows 与 POSIX 下命令行入口一致
  （`python -m chart_reader ...`，Windows 另附 `chart_reader.bat` 与
  `scripts/run_baseline.bat`）。

## 样本生成（确定性）

- `chart_reader/generator.py`：固定种子 + 固定画布（默认 640x480），
  `random.Random(seed * 100003 + chart_index)` 派生每张图的数据。
- 坐标轴为纯黑 1px 直线；刻度为轴左侧 5px 短横线；折线折点为 5x5 实心
  方块标记，柱体为纯色填充矩形。Pillow 基本图元无抗锯齿，整数坐标渲染，
  同种子重生成图像字节级一致（已验证，见"验证结果"）。
- 真值清单 `samples/truth.jsonl` 与图像同源产出，字段含 `chart_id`、
  `chart_kind`、`canvas_shape`、`series`、`points`（含 `pixel_x`、
  `pixel_y`、`value`、`axis_min`、`axis_max`、`tick_pixels`）。

## 精度与口径声明（关键）

- 像素口径：`integer_pixel_top_left`，原点左上，y 向下增长。
- 标定求解：闭式正规方程 `slope = Sxy / Sxx`，`intercept = vy - slope * px`，
  IEEE-754 binary64；刻度配对约定为"像素升序对刻度值降序"（像素 y 与数值
  反向单调）。
- 反算：`value = slope * pixel_y + intercept`，**永不取整或截断**；唯一的
  后处理是当 `clip_to_axis_domain=true` 时裁剪到 `[axis_min, axis_max]`，
  每个裁剪事件逐点记录（`clipped` 字段）。
- 误差统计口径：观测口径为真值 `value`；相对偏差分母下限
  `sys.float_info.epsilon`；报告中误差统计按 `error_stats_precision`
  （默认 12 位有效数字）做声明式舍入，仅用于展示，不回灌计算链路。
- 门限判定：严格策略为精确 `>=`；容差带策略为
  `>= threshold - max(配置容差, ulps_tolerance * ulp(操作数))`，
  `math.ulp(0.0)` 参与取底，覆盖次正规数区间；判定前不对反算值做任何
  舍入，因此重复运行不会翻转。

## 阶段与业务码

| 阶段 | 失败业务码 |
| --- | --- |
| tick_localization | E_TICK_LOC_INVALID, E_TICK_COUNT_MISMATCH |
| calibration | E_AXIS_RANGE_INVALID, E_CALIB_DEGENERATE |
| point_localization | E_POINT_OUT_OF_CANVAS, E_SERIES_INVALID |
| inversion | E_DUPLICATE_SERIES |
| error_propagation | E_ERROR_UNTRACEABLE |
| config | E_CONFIG_INVALID（指明字段） |
| snapshot | E_SNAPSHOT_ROLLBACK, E_SNAPSHOT_CORRUPT |
| self_check | E_INCONSISTENT（指明类别） |

## 故障注入与归因

注入完全由 `config/faults.example.json` 参数驱动，管道在阶段边界修改
该阶段的**输入**（刻度像素、轴范围、候选点坐标、序列号），不修改源码。
四类注入点：tick_pixel_out_of_bounds、axis_range_non_positive、
point_outside_canvas、duplicate_series_id。每次注入产生带
`stage / code / chart_id` 的记录；下游阶段输出
`{status: degraded, degradation: stage_skipped, triggered_by: <上游业务码>}`；
未被归因的异常汇总到报告 `unattributed_anomalies`。

## 快照

`config` / `ground_truth` / `error_stats` 三类快照，单调递增版本号 +
规范 JSON 的 SHA-256 校验和；任何变化整体原子替换（临时文件 + rename），
不做增量修补；旧版本覆盖尝试返回 E_SNAPSHOT_ROLLBACK；加载校验和不符
返回 E_SNAPSHOT_CORRUPT 并指明类别与版本号。

## 自检与复现

自检校验：各阶段 chart_id 集合一致；反算值可由标定参数与像素坐标在
`recompute_atol` 内重算一致；误差区间可追溯至偏差明细且放大比可由明细
重算。报告哈希 = 去除 `report_sha256` 字段后规范 JSON 的 SHA-256。

复现步骤：

```bash
pip install -r requirements.txt
bash scripts/run_baseline.sh        # Windows: scripts\run_baseline.bat
python -m unittest discover -s tests
```

## 验证结果（本机实测）

- 6 张样本（3 折线 + 3 柱状）基线：自检 ok，业务码 0，未归因异常 0。
- 刻度定位偏差 0 px；标定残差 <= 0.193；点定位偏差折线 0 px、
  柱状 0.5 px（半像素中心）；反算最大绝对偏差 <= 0.285（轴量程 250）。
- 四类注入各产生对应业务码与下游降级归因，报告记录 4 条注入、4 条业务码。
- 固定种子重跑两次：报告字节级一致，
  `report_sha256=ebf4ba55a13e613c4446c6af684a9673fd147375fe0f04673c0732a2a350614b`。
- 同种子重新生成样本：图像 SHA-256 逐一相同。
- `python -m unittest discover -s tests`：19 个测试全部通过。
