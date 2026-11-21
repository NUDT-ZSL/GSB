# 本地线性模型正则化路径训练引擎

这是一个不依赖网络、数据库或容器环境的 Python 3.10+ / NumPy 实现，用于在本地
JSONL 表格样本上训练二分类线性模型，并沿 L2 或弹性网正则化路径逐档求解。

引擎的重点不是追求某个精度档的极限速度，而是把以下约束做成可执行、可检查的
契约：

- 同一数据、配置和精度档重跑时，路径输出逐字节一致。
- 分块并行只改变执行方式，不改变按块号派发、按块号归约的确定性。
- 标准化、完整档位、路径校验、路径汇总均以内容校验和保护的整份快照提交。
- 强制终止后从最后一个完整档位继续，不重算已提交系数，丢弃未提交的档内迭代。
- 快照损坏、回退、并发提交、资源超限、路径单调性、目标重算和版本不一致均有
  独立业务码和上下文。

完整设计与实际验证记录见：

- [docs/DESIGN.md](docs/DESIGN.md)
- [docs/VERIFICATION.md](docs/VERIFICATION.md)

## 快速开始

```bash
# 可选：安装为可编辑包
python3 -m pip install -e '.[test]'

# 或直接使用源码路径
export PYTHONPATH="$PWD/src"

# 生成固定种子 JSONL 和五份实验配置
python3 scripts/generate_data.py --root . --seed 20261122 --samples 240

# float64，4 个 spawn worker
python3 -m linear_path_engine train -c configs/float64.json

# float32 与定点缩放整数档
python3 -m linear_path_engine train -c configs/float32.json
python3 -m linear_path_engine train -c configs/fixed-point.json

# L2 正则化示例
python3 -m linear_path_engine train -c configs/float64-l2.json

# 独立重算并检查已保存结果
python3 -m linear_path_engine validate \
  -c configs/float64.json \
  -r artifacts/run-float64/result.json
```

训练结果写入配置的 `output.directory/result.json`。每次训练还会在
`snapshots.directory` 下产生版本号严格递增的整份快照。

## 模型与数值语义

### 输入与编码

- 每行必须包含 `row_id`、`label`、若干数值列和类别列；标签必须是配置的
  `positive_label` 或 `negative_label`。
- `row_id` 必须唯一。输入行按 `row_id` 的稳定文本键排序，因此 JSONL 行序变化
  不会改变数据指纹。
- 数值列按 `preprocessing.standardization` 处理：
  - `zscore`: `(x - mean) / std`，`std=0` 时缩放因子取 1。
  - `minmax`: `(x - min) / (max - min)`，零跨度时缩放因子取 1。
- 类别列按 UTF-8 字典序排序后做 full one-hot，特征名为
  `cat:<column>=<token>`。数值特征名为 `num:<column>`。
- 训练内部标签为 `+1/-1`。默认正标签为 `1`，负标签为 `0`。

### 目标函数

当前实现的 `model.loss` 为 `squared_error`，即线性判别器的平方误差：

```text
J(w,b) = 1/(2n) * ||Xw + b - y||_2^2 + P(w)
```

`L2`：

```text
P(w) = 0.5 * lambda * ||w||_2^2
```

弹性网：

```text
P(w) = lambda * (l1_ratio * ||w||_1
                 + 0.5 * (1 - l1_ratio) * ||w||_2^2)
```

截距 `b` 不受正则化。系数输出对应标准化后的特征空间，原始标准化统计量同时
写入结果，供独立重算目标函数。

### 路径求解

`model.strengths` 必须严格递增。配置表示“从小到大的路径”，求解器按需求从
最强正则化档开始：

```text
config order:   lambda_0 < lambda_1 < ... < lambda_k
solve order:    lambda_k, lambda_{k-1}, ..., lambda_0
```

每个较弱档以上一个已提交档为 warm start。求解器是可复现的近端梯度法：

- 使用增广设计矩阵的谱 Lipschitz 常数确定基础步长；
- 使用确定性 FISTA 动量和自适应重启；
- 每轮由各数据块计算“按全局样本数归一化”的偏梯度；
- 主进程按块号升序相加，更新参数后再由共享精度算子执行弹性网近端映射；
- 参数无穷范数变化不超过该档 `tolerance` 时标记 `converged`，否则在
  `max_iterations` 处标记 `max_iterations`。

每档结果包含系数、截距、目标函数及分解、收敛标记、迭代数、梯度范数、
`stats_version`、精度档、比较次数等字段。

## 精度档

`precision.mode` 可设为：

- `float64`
- `float32`
- `fixed_point`

三档共用同一目标函数、路径顺序、分块和近端更新语义。定点档使用显式配置：

```json
{
  "precision": {
    "mode": "fixed_point",
    "fixed_point": {
      "scale": 4096,
      "rounding": "nearest_even"
    }
  }
}
```

`rounding` 支持 `nearest_even`、`nearest_away`、`truncate`。定点档将特征、标签、
参数和梯度表示成缩放整数；logit 和梯度累加使用整数点积后按配置舍入。输出结果
再解码为实数。定点档的差异只来自量化/舍入，不会切换损失或更新规则。

## 分块、并行与预算

`execution` 配置包含：

```json
{
  "execution": {
    "block_size": 48,
    "workers": 4,
    "min_block_size": 8,
    "reduction_order": "block_index_ascending",
    "memory_budget_bytes": 67108864,
    "comparison_budget": 100000000,
    "max_path_size": 16,
    "max_iterations_per_strength": 5000,
    "allow_small_block_downgrade": true,
    "allow_worker_downgrade": true
  }
}
```

调度契约：

- 任务按块号升序提交；
- 结果按块号升序读取和归约；
- 每个 worker 的偏梯度按全局样本数 `n` 归一化，因此直接相加就是全量梯度；
- 分块边界必须连续覆盖 `[0, n)`，不遗漏、不重叠；
- 同一观测下，决策记录经 canonical JSON 序列化后逐字节一致。

决策器根据样本数、特征数、精度字节宽度、迭代预算和内存预算估算：

- 全局标准化数组、每个 worker 的编码数据副本、worker 工作区；
- 梯度点积、块归约、收敛比较和最终评估的比较次数。

若内存估算超限，允许在不改变求解语义的前提下按顺序降级：

1. 缩小 worker 数，标记 `workers_reduced`；
2. 缩小分块，标记 `small_block`。

仍超限则返回 `E_RESOURCE_LIMIT`。降级决策随标准化阶段一起提交，恢复时直接
复用该决策，不会重新规划或改变已提交档位结果。

`result.json` 的 `chunk_plan` 包含边界、覆盖证明、决策依据、估算值、降级类别和
`decision_checksum`。

## 四阶段状态机与恢复

阶段顺序：

```text
standardization -> path_solve -> path_validation -> path_summary
```

快照版本每次提交严格加一：

1. 标准化统计量完成后提交 `standardization`；
2. 每个档位只有在全部分块和迭代完成后才提交一个 `path_solve` 快照；
3. 最后一个档位提交时 `phase_complete=true`；
4. 跨档校验完成后提交 `path_validation`；
5. 汇总结果完成后提交 `path_summary`。

每个档位是独立的提交事务。档内中间迭代不落盘；进程在档内中断时，恢复会从
上一个已提交档的 warm start 重新构建该档。已提交路径项原样复用，不重新求解。

提交协议：

- 新快照先写入临时文件、`fsync`，再原子替换为不可变快照文件；
- 随后原子替换 `manifest.json`，manifest 是当前版本的唯一权威指针；
- 每个快照携带 payload 校验和与 envelope 校验和；
- 旧版本文件保留用于审计，但不会被 manifest 重新指向；
- 不另设可变缓存文件；标准化统计量、分块决策和已完成档位都由整份快照缓存，
  避免缓存与快照双写不一致；
- 检测到版本或校验和回退时拒绝提交并返回 `E_SNAPSHOT_ROLLBACK`；
- 读取时 payload/envelope/manifest 校验和不符返回
  `E_SNAPSHOT_CORRUPT`，并携带阶段名和版本号；
- 提交锁使用 `flock(LOCK_EX | LOCK_NB)`，并发提交立即返回
  `E_STAGE_BUSY`，不阻塞等待。

恢复时会校验：

- 配置语义指纹；
- 数据语义指纹；
- 求解顺序；
- 每个已完成档位的 `stats_version`；
- 标准化统计量内容版本；
- 已提交分块决策的精度档与输入规模。

## 跨档自检

`path_validation` 阶段会：

1. 按 `lambda` 升序检查系数 L2 范数：
   `higher_lambda_norm <= lower_lambda_norm + tolerance`；
2. 用已提交标准化统计量从原始数据独立重建 `X`，再由系数和截距重算目标函数；
3. 检查每个档位的 `stats_version` 等于标准化统计量内容哈希。

容差全部由配置显式给出：

```json
{
  "validation": {
    "monotonicity_abs_tolerance": 1e-10,
    "monotonicity_rel_tolerance": 0.005,
    "objective_abs_tolerance": 1e-7,
    "objective_rel_tolerance": 1e-8
  }
}
```

分别返回：

- `E_MONOTONICITY`
- `E_OBJECTIVE_MISMATCH`
- `E_VERSION_MISMATCH`

错误上下文包含 `stage`、`strength`、`strength_index` 和相关数值证据。

## 配置参考

最小配置结构：

```json
{
  "schema_version": 1,
  "run_id": "demo-float64",
  "data": {
    "path": "../data/table.jsonl",
    "row_id_column": "row_id",
    "label_column": "label",
    "numeric_columns": ["x1", "x2", "x3", "x4", "x5"],
    "categorical_columns": ["cat0", "cat1"],
    "positive_label": 1,
    "negative_label": 0,
    "category_encoding": "one_hot_sorted"
  },
  "preprocessing": {
    "standardization": "zscore"
  },
  "model": {
    "loss": "squared_error",
    "regularization": {
      "type": "elastic_net",
      "l1_ratio": 0.3
    },
    "strengths": [0.0005, 0.001, 0.002, 0.005]
  },
  "solver": {
    "max_iterations": 1200,
    "tolerance": 1e-8,
    "lipschitz_safety": 0.99,
    "iterations_per_strength": null,
    "tolerance_per_strength": null
  },
  "precision": {
    "mode": "float64",
    "fixed_point": {
      "scale": 4096,
      "rounding": "nearest_even"
    }
  },
  "execution": {
    "block_size": 48,
    "workers": 4,
    "min_block_size": 8,
    "reduction_order": "block_index_ascending",
    "memory_budget_bytes": 67108864,
    "comparison_budget": 100000000,
    "max_path_size": 16,
    "max_iterations_per_strength": 5000,
    "allow_small_block_downgrade": true,
    "allow_worker_downgrade": true
  },
  "snapshots": {
    "directory": "../artifacts/snapshots-float64",
    "enabled": true,
    "resume": true
  },
  "output": {
    "directory": "../artifacts/run-float64"
  },
  "validation": {
    "monotonicity_abs_tolerance": 1e-10,
    "monotonicity_rel_tolerance": 0.005,
    "objective_abs_tolerance": 1e-7,
    "objective_rel_tolerance": 1e-8
  }
}
```

可选 `solver.iterations_per_strength` 和 `solver.tolerance_per_strength` 数组必须与
递增的 `model.strengths` 对齐，用于给每档不同上限和容差。

可选 `testing.fault_worker_at` 形如 `"2:1"`，只用于故障注入测试：在升序强度索引
2、块索引 1 的 worker 内主动抛错。

## CLI

```text
python -m linear_path_engine train -c CONFIG [--print-result]
python -m linear_path_engine validate -c CONFIG -r RESULT.json
python -m linear_path_engine snapshot-status -c CONFIG
python -m linear_path_engine snapshot-rollback -c CONFIG --to-version VERSION
```

强制终止验证参数（仅测试用）：

```text
--crash-after-strengths N
--crash-during-strength INDEX --crash-after-iterations N
--fault-worker-at "STRENGTH_INDEX:BLOCK_INDEX"
```

`--crash-*` 使用 `os._exit(97)`，不会执行正常清理，用于验证原子快照和恢复。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_CONFIG` | 配置结构或取值非法 |
| `E_DATA` | JSONL、字段、标签或数值非法 |
| `E_RESOURCE_LIMIT` | 路径数、迭代、比较次数或驻留内存超限 |
| `E_STAGE_BUSY` | 另一阶段正在提交，非阻塞冲突 |
| `E_SNAPSHOT_CORRUPT` | 快照缺失、版本/校验和/内容损坏 |
| `E_SNAPSHOT_ROLLBACK` | 尝试版本或校验和回退 |
| `E_MONOTONICITY` | 系数范数违反路径单调性 |
| `E_OBJECTIVE_MISMATCH` | 保存目标值无法由系数独立重算 |
| `E_VERSION_MISMATCH` | 配置、数据、统计量或求解版本不一致 |
| `E_WORKER` | worker 异常，附阶段、档位、块索引和边界 |
| `E_STATE` | 内部状态机非法状态 |
| `E_ARGUMENT` | CLI 参数组合非法 |

CLI 将该结构写入 stderr，并用非零退出码区分。

## 测试

```bash
export PYTHONPATH="$PWD/src"
python3 -m pytest -q
```

测试覆盖：

- JSONL 排序、类别编码和标准化可复现性；
- 分块覆盖、决策字节一致性、内存降级和比较预算；
- float64 / float32 / 定点三档路径语义，以及三种定点舍入模式；
- 串行与 4 worker / 3 worker 的路径逐项一致；
- worker 异常归因；
- 档位提交后硬终止恢复、档内迭代硬终止恢复、降级计划复用；
- 快照版本单调、整体替换、损坏定位、回退拒绝、非阻塞并发锁；
- 单调性、目标重算、版本一致性及全部资源业务码。

本机实际结果见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

## 项目结构

```text
src/linear_path_engine/
  config.py          配置解析与语义指纹
  data.py            JSONL 读取、排序、固定编码
  standardization.py 统计量、变换和内容版本
  precision.py       三档共享算子语义
  planner.py         分块/内存/比较代价决策
  parallel.py        spawn 进程池、worker 协议
  solver.py          FISTA 近端路径求解与预算
  snapshot.py        原子快照、校验和、提交锁
  validation.py      跨档自检
  engine.py          四阶段状态机与恢复
  cli.py             命令行接口
scripts/generate_data.py
tests/
configs/
data/
artifacts/
```

## 已知边界

- 数据必须完整驻留内存；本项目验证的是驻留内存估算上限，不是核外训练。
- 当前只实现平方误差线性判别器，尚未实现 logistic 损失。
- 未实现新样本推理时的未知类别策略；训练数据中的类别集合在标准化阶段固定。
- 多进程任务粒度较细。很小的表上，进程间调度开销可能高于计算收益；决策记录
  会显式给出估算依据，但不会为了“看起来更快”而改变已声明的 worker 数。

