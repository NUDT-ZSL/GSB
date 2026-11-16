# 特征交叉组合与降维实验工作台 — 技术说明

## 1. 总体结构

工作台以纯 Python 包 `featbench` 交付，所有随机性均来自显式种子，不依赖网络数据。

| 模块 | 职责 |
| --- | --- |
| `featbench/config.py` | YAML 加载、强类型校验、规范化与配置指纹 |
| `featbench/data.py` | 固定种子合成数值/类别/冗余列、缺失值与目标列，支持多档放大 |
| `featbench/combiner.py` | 类别列白名单组合枚举、确定性哈希、基数统计与预算降级 |
| `featbench/encoder.py` | one-hot 编码、缺失标记、均值填充与常量列识别 |
| `featbench/reduction.py` | PCA / 截断随机投影 / 方差筛选三条降维路径 |
| `featbench/strategy.py` | 规模指标计算、预置策略表评分与确定性决策 |
| `featbench/evaluation.py` | 确定性划分与 Ridge 回归评估 |
| `featbench/cache.py` | 分层缓存键、键级锁、复用/失效台账 |
| `featbench/snapshot.py` | 临时文件 + 原子替换快照、配置指纹校验、信号标志 |
| `featbench/pipeline.py` | 排序键、线程池并发、异常隔离、阶段编排 |
| `featbench/report.py` | 诊断报告组装与原子写盘 |
| `featbench/cli.py` | `validate` / `run` 命令行入口 |

## 2. 确定性与指纹体系

1. **配置指纹**：规范化后的配置经 canonical JSON（键排序、固定分隔符）后取
   blake2b 16 字节摘要。`work_dir` 与 `workers` 不参与指纹：前者是运行位置，
   后者是执行参数，二者变化不得改变实验内容、随机投影种子或缓存语义。
2. **数据指纹**：配置指纹 + 规模档参数 + 生成形状派生；每个列生成器使用
   `np.random.default_rng`，种子由 `配置种子|规模档|阶段标签` 派生。
3. **缓存键**：`阶段__规模__路径__摘要`，摘要材料包含上游内容指纹与参数指纹，
   因此规模、阶数、预算、降维参数变更只会使对应阶段及下游失效。
4. **随机投影**：投影矩阵种子由 `配置指纹|规模档|rp_seed_tag` 派生，同配置
   多次运行得到逐元素相同的矩阵。
5. **PCA 符号归一**：每个主成分取绝对值最大的系数作为枢轴，若为负则整行取反，
   消除 SVD 符号歧义；成分按奇异值降序排列。
6. **原生 BLAS 约束**：管线在 `threadpool_limits(limits=1)` 下运行，并预置
   `OMP/OPENBLAS/MKL` 单线程环境变量，保证线程池并发不改变浮点求和顺序。

## 3. 交叉组合与基数预算

- 组合按 `itertools.combinations(sorted(whitelist), order)` 枚举，顺序稳定。
- 交叉值以 `v1||v2||...` 拼接，blake2b 64 位哈希作为确定性整数编码。
- 原始基数 = 非缺失唯一拼接值数量。
- `raw_cardinality <= budget`：保留无碰撞词汇表，策略记为 `kept`。
- 超预算且策略为 `hash_bucket`：最终基数固定为预算，`hash % budget` 截断。
- 超预算且策略为 `skip`：跳过该组合，审计中 `final_cardinality=0`。
- 每条审计记录包含组合列、阶数、原始/最终基数、预算、策略与原因。

## 4. 降维路径的业务码

| 场景 | 行为 |
| --- | --- |
| 缺失值未填充（`impute_missing: false`） | 抛出 `E_MISSING_UNFILLED`，不产生 NaN 矩阵 |
| 全部特征为常量 | 抛出 `E_CONSTANT_COLUMN` |
| 部分 one-hot 列为常量 | 降维前剔除并在摘要 `dropped_constant_count` 记录 |
| 方差筛选后无可保留特征 | 评估阶段记录 `insufficient samples or features`，任务不崩溃 |

## 5. 规模自适应策略

指标：样本数 `n`、编码特征数 `p`、小样本比例 `p/n`、平均预算占用率、
活跃/降级交叉数。策略表每行包含样本窗口与三个阈值门控，门控全过为 eligible；
无候选全过时取最高分。评分相同时按 `(path, name)` 字典序打破平局，
交叉阶数上限再与配置阶数取 min，保证同指纹同规模档决策记录完全一致。

## 6. 增量复用与失效

- `cross`、`encode` 为规模级共享产物，`reduce`、`evaluate` 为路径级产物。
- `CacheStore` 根目录同时按 `work_dir + 配置指纹` 命名空间隔离，避免不同
  实验目录互相串扰；键级锁保证并发下共享阶段只计算一次。
- 每次运行结束，未被当前任务引用的缓存文件进入 `invalidated` 清单。
- 报告中的 `cache_ledger` 给出复用、重算、失效键及命中/未命中计数。

## 7. 并发、快照与异常隔离

- 任务排序键 = `规模序号|路径序号|规模ID|方法名`，结果汇总前按该键排序，
  指标表、决策记录、失败清单顺序与单线程执行完全一致。
- 快照载荷含 `version/stage/config_fingerprint/scale_id/path/cursor/
  artifact_summary`，写入走临时文件 + `os.replace`；恢复时指纹不符抛
  `E_CONFIG_MISMATCH`，版本不符抛 `E_SNAPSHOT_CORRUPT`。
- `ShutdownFlag` 捕获 SIGINT/SIGTERM，当前阶段快照完成后以退出码 0 结束；
  强制终止时未完成替换的临时文件不会被读取。
- 单任务异常被转换为 `{scale, stage, code, message, exception_type}` 写入
  失败清单，其余任务继续执行。

## 8. 验证方式

```bash
python3.11 -m unittest discover -s tests -v
python3.11 scripts/run_acceptance.py
python3.11 -m featbench.cli run configs/experiment_multi_scale.yaml
```

`run_acceptance.py` 执行五步编辑序列（规模切换 → 阶数调整 → 预算变更 +
skip 策略 → 降维方法切换），再从空白目录全量重算，逐字段比较指标表、
保留特征、决策记录、降级清单、失败清单与报告指纹。
