# 本地确定性 CTR 训练管线

面向搜索推荐 CTR 场景的单机数据处理、并行调度、预算准入与结果验证工具。项目只依赖 Python 3.10+、NumPy 和本地 `multiprocessing`，不需要 GPU、容器、外部服务或网络。

核心链路：

```text
确定性样本生成
  -> JSONL 摄入与配置/标签校验
  -> Blake2b 特征散列与交叉项展开
  -> 计算/内存双预算准入
  -> 分块梯度并行计算
  -> 按声明顺序归约
  -> 整段训练对照
  -> AUC/LogLoss 独立重算
  -> 版本化快照与并发提交控制
```

## 快速运行

```bash
python3 -m pip install numpy

# 生成可复现样本与特征字典
python3 -m ctr_pipeline generate --out data --samples 120 --seed 20261121

# 单 worker 跑通
python3 -m ctr_pipeline run \
  --exposures data/exposures.jsonl \
  --features data/features.jsonl \
  --feature-dim 128 \
  --chunk-size 8 \
  --batch-size 24 \
  --workers 1 \
  --out results/single-worker

# 并行运行并发布三类快照
python3 -m ctr_pipeline run \
  --exposures data/exposures.jsonl \
  --features data/features.jsonl \
  --feature-dim 128 \
  --chunk-size 8 \
  --batch-size 24 \
  --workers 3 \
  --out results/parallel \
  --snapshot-dir snapshots \
  --snapshot-version 1

# 独立验证结果
python3 -m ctr_pipeline validate \
  --result results/parallel/run.json \
  --exposures data/exposures.jsonl \
  --features data/features.jsonl \
  --feature-dim 128
```

运行完整验收套件：

```bash
python3 -m ctr_pipeline acceptance --out results/acceptance
pytest -q
```

## 数据格式

`features.jsonl` 每行是一个特征字典项：

```json
{"feature_index":0,"dimension":1,"type":"user","cardinality":17,"dictionary_version":"dict-v1"}
```

`exposures.jsonl` 每行是一个曝光样本：

```json
{
  "sample_id":"s000001",
  "user_indices":[0,1],
  "user_values":[[0.31],[-0.22,0.74]],
  "item_indices":[100,102],
  "item_values":[[0.55],[0.18,-0.41]],
  "cross_slots":[
    {"slot":0,"left":0,"right":100},
    {"slot":1,"left":1,"right":102}
  ],
  "label":1
}
```

校验规则包括：特征索引必须存在于字典、用户/物品类型匹配、取值维度与字典一致、数值有限、交叉槽位可解析、槽位不重复、交叉两侧维度一致、标签严格为 `0/1`、样本标识唯一。

## 确定性与并行口径

- 摄入后按 `sample_id` 升序处理；重复标识返回 `E_DUP_SAMPLE`。
- 特征桶由 `blake2b` 稳定散列得到，不依赖 Python `hash`、字典遍历顺序或系统时间。
- 散列冲突按固定类别/索引顺序累加；非零索引升序输出。
- 分块边界为已准入样本前缀上的固定窗口；任务按 `(batch_index, chunk_index)` 派发与归并。
- 每个 worker 独立计算 `loss_sum` 和梯度；主进程按分块序号显式累加，再更新参数。
- 所有参数与梯度使用 `float64`；规范化 JSON 不写入时间戳或绝对路径。
- `determinism_fingerprint` 覆盖分块轮次、损失、梯度、逐样本分数、最终系数与整段对照结果。

同一输入、字典版本、分块口径和训练配置下，1 worker 与 3 worker 的轮次/系数字节及指纹一致。验收中两者指纹均为：

```text
e3a570bbf51d0553ab1c686ff52fa8416f28607264bee55fe855f6bbe74f0742
```

## 预算与裁决

预算器估算：

- MAC：样本前向/反向乘加、参数更新、分块归约开销、每 worker 固定开销。
- 内存：准入样本驻留、稀疏非零项、分数明细、参数/梯度、worker scratch、分块元数据。

显式优先级：

1. 保持标签、样本顺序和已准入样本评分语义不变。
2. 内存安全优先；必要时缩小分块或拒绝尾部完整分块。
3. 先最大化准入样本数，再减少 worker 或增加归约开销。
4. 准入集合相同时，优先较大分块，再优先更多 worker。
5. 默认不缩小散列特征维度，因为它会静默损失交叉表达能力。

超限时默认进入受限模式，在 `budget.error` 和 `summary` 中返回 `E_COMPUTE_BUDGET` 或 `E_MEMORY_LIMIT`，并记录候选比较、批次路径、让步项和理由。`--strict-budget` 会直接抛出结构化错误。

## 快照与并发

`schedule_plan`、`budget_ledger`、`training_summary` 各自使用单调递增版本号和内容校验和。发布采用临时文件 `fsync` 后 `os.replace` 原子整体替换。加载时校验对象名、版本、schema 与 SHA-256 校验和；不一致返回 `E_SNAPSHOT_CORRUPT`，并包含 `object_name` 与 `version`。

调度计划提交使用非阻塞 `flock` 门闩。并发提交的后到者立即得到 `E_STAGE_BUSY`，不等待。账本扣减/释放由进程内互斥锁保护，并按事务标识幂等处理。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_CONFIG_INVALID` | 配置或字段非法，`details.field` 指明字段 |
| `E_DUP_SAMPLE` | 重复 `sample_id`，包含首次与重复行号 |
| `E_COMPUTE_BUDGET` | MAC 预算受限并降级 |
| `E_MEMORY_LIMIT` | 驻留内存预算受限并降级 |
| `E_WORKER` | worker 内异常，包含阶段、批次与分块归属 |
| `E_INCONSISTENT` | 跨阶段一致性校验失败，包含类别与首个偏差位置 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏或校验和不符 |
| `E_STAGE_BUSY` | 已有调度计划提交生效 |
| `E_LEDGER_INSUFFICIENT` | 账本余额不足 |

## 实际验收结果

已使用 `seed=20261121`、60 条确定性样本执行 `python3 -m ctr_pipeline acceptance`，13 项检查全部通过：

- 同种子生成两份数据逐字节一致。
- 特征字典逆序与曝光日志逆序展开结果一致；展开校验和 `74d6c5e09f3b83fcac70efcaf24286dc1c9ae3d1b9ad29355ac74b262b0905fd`。
- 1/3 worker 的训练轮次、梯度与最终系数一致。
- 分块与整段 AUC `0.7586980920314254`、LogLoss `0.6232462321864833`；最终参数最大绝对偏差 `2.7755575615628914e-17`；两条路径收敛轮次标记一致。
- 计算预算降级：准入 56/60，拒绝 4，`E_COMPUTE_BUDGET`，worker 自动降到 1。
- 内存预算降级：准入 12/60，拒绝 48，`E_MEMORY_LIMIT`，分块缩到 4。
- 注入第 1 块故障：保留其余 7 块成功结果，错误定位 `batch_index=0, chunk_index=1`。
- 配置/标签/维度/重复样本错误码均命中预期字段。
- 三类快照从版本 1 整体替换到版本 2；损坏快照返回对象名与版本号。
- 并发计划提交立即返回 `E_STAGE_BUSY`；100 次并发扣减与 10 次释放后账本余额精确为 910。

完整机器可读结果见 `results/acceptance/acceptance_report.json`。当前环境：Python `3.10.12`、NumPy `1.26.4`、`multiprocessing` 启动方式 `fork`。自动化测试 `23 passed`。

## 目录

```text
ctr_pipeline/       管线、训练、预算、验证、快照、并发与 CLI
tests/              pytest 自动化测试
data/               可写样本目录
cache/              可写缓存目录
snapshots/          可写快照目录
results/            运行与验收输出
docs/TECHNICAL.md   计算口径、预算公式与验证设计
```
