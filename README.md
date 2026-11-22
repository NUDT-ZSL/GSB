# QuantTrace Lab

面向 LLM 推理部署前处理链路的本地可视化与诊断工作台，聚焦低精度量化、分块推理、误差传播归因，以及强制终止后的快照恢复。

项目不依赖真实模型文件、GPU、网络服务或 Docker。模型包清单、权重、校准输入和推理输入全部由固定种子在运行时确定性生成。

## 快速开始（Windows）

```bat
cd /d <project-path>
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

REM 完整运行 -> 第 1 层提交后强制退出 -> 恢复 -> 字节级比对
.venv\Scripts\python -m quantlab.cli demo --crash-after-layer 1

REM 启动工作台
.venv\Scripts\python -m quantlab.cli serve --port 8765
```

打开 `http://127.0.0.1:8765`。页面支持：

- 运行一次“完整运行 / 强杀 / 恢复 / 比对”演示
- 触发第 1 层权重版本升级的失效重算演示
- 查看逐层误差谱、主导来源、阶段快照、恢复报告、预算与降级记录、最近业务错误

也可以直接执行：

```bat
scripts\run_demo.bat
scripts\validate.bat
```

## 常用命令

```bat
REM 完整运行，清理旧快照
python -m quantlab.cli run --clean --json

REM 推理到指定层提交后强制退出（退出码 23）
python -m quantlab.cli crash-run --clean --crash-after-layer 1

REM 从最近已提交进度点恢复
python -m quantlab.cli resume --json

REM 第 1 层版本升级，计算受影响后缀并整体替换快照
python -m quantlab.cli revision-demo --layer-index 1 --revision 2 --json

REM 本地 HTTP 工作台
python -m quantlab.cli serve --port 8765

REM 自动化测试 + 完整演示 + 实际结果归档
python scripts/validate_local.py
```

默认目录：

- 状态与快照：`runtime/state`
- 临时原子写目录：`runtime/tmp`
- 缓存：`runtime/cache`
- 结果与仪表盘：`artifacts/`
- 模型包清单：`runtime/state/manifest.jsonl`

`run`、`crash-run`、`resume` 均支持 `--state-dir`、`--temp-dir`、`--cache-dir`、`--artifacts-dir`，便于隔离多组验收。

## 项目结构

```text
quantlab/
  config.py           运行配置、预算与容差
  deterministic.py    固定种子模型清单、权重、激活与校准输入
  manifest.py         JSONL 解析、链形校验、摘要
  quantization.py     分组缩放、定点舍入、反量化、独立复算
  calibration.py      校准输入统计与比较次数预算
  inference.py        显式升序分块 GEMM、激活、校验和
  snapshots.py        原子快照、提交锁、进度索引、防回退
  diagnostics.py      误差谱、归因、容差、定点独立校验
  consistency.py      跨模块一致性自检
  pipeline.py         四阶段流水线、恢复、失效替换
  dashboard.py        仪表盘数据组装
  demo.py             完整运行/强杀/恢复/字节比对编排
  server.py           本地 HTTP API 与静态页面
  static/             无外部依赖的可视化前端
tests/                29 个 unittest
scripts/              Windows 演示与统一验收入口
```

## 确定性模型与量化

清单每行字段为：

`model_tag, layer_id, layer_index, layer_shape, weight_revision, quant_dtype, scale_hint, zero_point, calib_seed, block_policy`

默认生成 4 层链式模型：

`[12, 8] -> [10, 12] -> [8, 10] -> [6, 8]`

权重生成种子由 `全局 seed + model_tag CRC + layer_index + weight_revision` 组成；校准输入由 `calib_seed + layer_index + weight_revision` 驱动。除显式配置和固定种子外不使用任何隐式状态。

支持 `int8`、`uint8`、`int4`、`uint4`。量化过程：

1. 按 `block_policy.group_size` 展平分组的权重。
2. 组缩放为 `scale_hint * (0.75 + 0.25 * group_abs_max / global_abs_max)`。
3. 以 `rint_half_even` 舍入并夹取到量化范围。
4. 生成定点码、每组 scale、反量化权重和误差统计。

业务错误：

| 业务码 | 触发条件 |
| --- | --- |
| `E_QUANT_DTYPE` | 未知 `quant_dtype`，记录 `layer_id` |
| `E_SCALE_INVALID` | `scale_hint <= 0`、非有限或 group size 非法 |
| `E_ZERO_POINT_RANGE` | `zero_point` 不在 dtype 范围 |
| `E_MANIFEST_INVALID` | JSONL 字段、形状链、索引或顺序非法 |
| `E_TOLERANCE_EXCEEDED` | 任一层误差超容差，含层号与首个越界元素索引 |

## 分块推理

每层执行：

```text
input_i = activation_{i-1}
linear_i = blocked_gemm(input_i, dequantized_weight_i, block_policy)
activation_i = tanh(linear_i)
```

归约顺序显式固定为：

```text
output_block_ascending
  -> input_block_ascending
  -> k_ascending
  -> partial_sum_ascending
```

块内与跨块均按升序显式累加，不依赖 BLAS 的隐式归约细节。`tanh` 只在完整归约结果上执行一次，不对单个分块应用归一化，因此分块边界不会引入新的归一化统计量。

推理只从 `quantization` 阶段的最新已提交快照反序列化权重。最终预测向量为最后一层激活在 batch 维上的均值。每层激活与最终预测都有 SHA-256 内容校验和。

## 四阶段进度与原子快照

四个阶段：

1. `model_validation`
2. `quantization`
3. `calibration`
4. `inference`

每个完整处理完的层可以形成一个进度点。进度点字段包括：

- 全局单调递增 `version`
- `completed_layers`，只包含完整处理完的层
- `stage_complete`
- `next_layer_index`
- 内容 `checksum`
- 当前清单摘要与阶段 payload

提交过程：

1. 非阻塞创建 `.commit.lock`，已存在时立即返回 `E_STAGE_BUSY`。
2. 将完整阶段视图写入同文件系统临时文件并 `fsync`。
3. 通过 `os.replace` 原子替换快照文件。
4. 原子替换 `progress.json` 进度索引。

进度索引是提交可见性的唯一权威视图。若进程在快照文件与索引之间中断，孤立快照不会被视为已提交。索引和快照均带校验和；读取损坏或摘要不符时返回 `E_SNAPSHOT_CORRUPT`，并携带 `stage` 与 `version`。

请求提交不大于当前版本的进度会返回 `E_SNAPSHOT_ROLLBACK`，旧版本不能覆盖新版本。

## 强制终止与恢复

`demo` 使用真实子进程：

1. 完整运行生成基准结果。
2. `crash-run` 在指定推理层提交完成后调用 `os._exit(23)`。
3. `resume` 读取最近已提交快照，从 `next_layer_index` 继续。
4. 比对激活、最终预测、定点码、缩放参数和反量化权重的原始字节。

恢复时：

- 已提交层不重新计算。
- 未提交的层结果不进入 `completed_layers`，从该层起点重建。
- 若没有任何已提交进度点，返回 `E_NO_CHECKPOINT`，不猜测续算位置。

实际演示恢复点为 `inference v14`，已完成 `layer_00, layer_01`；恢复复用这两层，只重算 `layer_02, layer_03`。

## 权重版本失效与整体替换

当某层 `weight_revision` 升高或校准种子变化：

- 受影响层集合为变化层及其全部下游层。
- 量化、校准、推理分别复用不受影响的前缀。
- 只重算受影响后缀。
- 新进度写为新的完整累计快照，不修补旧文件；旧版本无法回退覆盖。

输出结构包含：

- `affected_layers`
- `recomputed_layers`
- `reused_layers`
- `disjoint`
- `union_equals_all_layers`
- `covers_affected`
- 各阶段自己的重算/复用清单

自动化测试验证第 1 层升级后：

```text
affected   = layer_01, layer_02, layer_03
recomputed = layer_01, layer_02, layer_03
reused     = layer_00
```

校准统计更新单独测试为只重算 `layer_02, layer_03`，量化权重全部复用。

## 误差谱与归因

参考真值：当前量化快照中的 float64 反量化权重，全段 GEMM + `tanh`。

四个诊断级：

- `quantization_rounding`：原始 float64 权重路径与参考路径的差异
- `group_scaling`：单 scale 反量化路径与分组 scale 参考路径的差异
- `block_accumulation`：在参考输入上使用显式分块 GEMM 的差异
- `layer_cascade`：实际串联推理路径与参考路径的累计差异

每层每级输出：

- `max_abs`、`mean_abs`、`rmse`
- `mean_relative`、`max_relative`
- 合法误差区间 `[lower, upper]`
- 相对首层和相对上一层的误差放大比

主导来源取各级跨层平均绝对误差最大者。定点偏差可由定点码、scale、zero point、dtype 范围和 `rint_half_even` 独立重算，自检要求最大偏差为 0。

## 预算、降级与异常归因

可配置：

- `max_layers_per_stage`
- `max_resident_weight_bytes`
- `max_calibration_comparisons`
- `max_block_elements`

超过不可降级预算返回 `E_RESOURCE_LIMIT`；可分块或校准采样降级时记录：

- `block_size`
- `calibration_subsample`

所有阶段异常带 `stage` 与业务码。已知业务异常保留原码；未归因异常包装为 `E_UNATTRIBUTED`，并写入 `last_error.json` 与 `error_log.jsonl`，不静默吞掉。

## 一致性自检

每次运行完成后检查：

- 四阶段层集合完全一致
- 每层 scale/code/dequantized 可互相复算
- 最终预测可由量化快照与逐层激活独立复算
- 重算层与复用层无交集，并覆盖受影响层
- 快照版本单调、已完成层集合无重复

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 工作台页面 |
| GET | `/api/health` | 健康检查 |
| GET | `/api/dashboard` | 当前仪表盘 JSON |
| POST | `/api/demo` | 完整运行、强杀、恢复、比对 |
| POST | `/api/run` | 运行完整流水线 |
| POST | `/api/revision-demo` | 第 1 层版本升级失效演示 |

前端只使用本地静态 HTML/CSS/JS，不访问外部资源。

## 测试与验收

```bat
python -m unittest discover -s tests -v
python scripts/validate_local.py
```

当前实际验证结果：

- 环境：Python 3.10.12、NumPy 1.26.4（项目要求 `>=3.10`，代码兼容 Python 3.11；提供环境没有 3.11 二进制）
- 自动化测试：`29/29 OK`
- 完整运行与强杀恢复：`all_equal = true`
- 激活字节、预测字节、定点码字节、scale 字节、反量化权重字节：全部一致
- 恢复复用层：`layer_00, layer_01`
- 恢复重算层：`layer_02, layer_03`
- 最终预测校验和：`895506da7ab043d9b54a958c4ead3826b60a37d59da332e6129bb8c059e62344`
- 一致性自检：全部通过
- 定点独立复算：全部匹配
- 误差区间合法性：通过
- 主导来源：`group_scaling`

详细机器可读结果见 `artifacts/validation_report.json`、`artifacts/demo_report.json` 和 `artifacts/dashboard.json`。
