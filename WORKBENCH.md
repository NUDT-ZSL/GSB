# 概率校准诊断工作台（calib_workbench）

面向表格二分类模型的本地概率校准可视化与诊断工作台。全部计算本地完成，
无数据库、容器或网络服务；唯一起源的随机性是配置中的固定种子。

## 环境准备

```bash
python -m venv .venv          # 需要 Python 3.11+
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install numpy             # 或 pip install -r requirements.txt
```

## 快速开始

```bash
python -m calib_workbench.cli init                 # 生成示例配置 + 确定性样本清单
python -m calib_workbench.cli run                  # 运行完整校准流水线
python -m calib_workbench.cli selfcheck            # 跨模块一致性自检
python -m calib_workbench.cli replay               # 按追溯链重放全部最终指标
python -m unittest discover -s tests               # 运行测试套件
```

`run` 结束后用浏览器打开 `out/report.html`：调度计划、预算账本、
逐 group 校准曲线、指标汇总、误差归因与追溯链六个视图。

## 样本清单与配置

`data/samples.jsonl` 每行一个样本：

```json
{"sample_id":"S000000","numeric":{"f0":..,"f1":..,"f2":..,"f3":..},
 "categorical":{"segment":"alpha","channel":"app"},"label":1,"group":"g1",
 "raw_score":0.83}
```

`raw_score` 由固定种子驱动的确定性打分函数产出（对真实 logit 施加
temperature/offset 失校准），无需真实模型文件。配置字段见
`calib_workbench/config.py` 的 `DEFAULTS`（种子、切分比例、校准器参数、
ECE 分箱、worker 数/每批任务数、双预算、三类资源上限）。

## 流水线与模块

| 阶段 | 模块 | 说明 |
| --- | --- | --- |
| 校验 | `validate.py` / `config.py` | 结构化业务码，指明字段 |
| 切分+预处理 | `split.py` | 组内固定种子切分；数值标准化（参考集统计量）、类别定序编码 |
| 拟合 | `calibrators/core.py` | Platt（Newton 法逻辑回归）、保序（PAVA）、直方图分箱 |
| 应用+指标 | `metrics.py` | 逐样本概率、ECE/Brier/可靠性曲线，口径显式记录 |
| 调度 | `scheduler.py` | 进程池分派，分块/派发/归并顺序确定 |
| 预算 | `budget.py` | 迭代+驻留内存双重准入裁决 |
| 缓存 | `cache.py` | 内容寻址（SHA-256 over canonical JSON） |
| 快照 | `snapshots.py` | 单调版本+校验和，整体替换，忙锁与回滚保护 |
| 失效传播 | `invalidation.py` | 三类定向变更分类与受影响节点计算 |
| 自检/重放 | `selfcheck.py` | E_INCONSISTENT / E_LINEAGE_BROKEN |

## 确定性契约（关键口径）

- 切分：组内按 `sample_id` 升序后以 `default_rng(stable_seed(seed,"split",group))`
  置换，`n_ref=floor(n*ratio)`，输出再按 `sample_id` 升序 canonical 化。
- 拟合：初值、比较顺序、停止条件在各 fitter docstring 中显式声明；
  同分按 `sample_id` 升序打破平局；每轮记录目标函数值与比较次数。
- ECE：等宽分箱 `linspace(0,1,k+1)`，归属规则 `min(int(p*k),k-1)`，
  权重 `bin_count/N`，无平滑无截断；口径随指标 payload 记录。
- 调度：组合按 `(calibrator, group)` 升序，按 `batch_size` 顺序分块，
  `pool.map` 保序收集，按任务下标归并；并行与串行结果逐字节一致。
- 缓存键：仅覆盖节点的输入内容（不含 config_version）；版本号变化而
  内容未变时精确复用，内容变化即整体失效。版本号记录于追溯链与快照。

## 业务码一览

输入/配置：`E_DUP_SAMPLE_ID`、`E_FEATURE_LENGTH`、`E_GROUP_MISSING`、
`E_UNKNOWN_CALIBRATOR`、`E_INVALID_BINS`、`E_INVALID_SPLIT_RATIO`、
`E_CONFIG_INVALID`、`E_MANIFEST_INVALID`
资源上限：`E_BATCH_LIMIT`、`E_MEM_TABLE_LIMIT`、`E_PARALLEL_LIMIT`
预算裁决：`E_ITER_BUDGET`、`E_MEMORY_BUDGET`（附被拒组合与缺口）
快照：`E_STAGE_BUSY`（非阻塞忙锁）、`E_SNAPSHOT_ROLLBACK`
追溯/自检：`E_LINEAGE_BROKEN`、`E_INCONSISTENT`
执行：`E_WORKER_FAILURE`（worker 异常结构化回传并归因到组合）
