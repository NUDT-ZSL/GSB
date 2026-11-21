# 多模态图文协同推理本地数据处理管线

一个完全本地、确定性、可恢复的四阶段图文协同推理管线。无外部服务、无网络依赖，
所有数值仅由固定种子与显式配置决定，同一 `pair_id` + `image_seed` 在相同配置下
产出**逐字节一致**的中间嵌入与最终分数。

## 环境要求

- Windows / Linux / macOS，Python 3.10+
- 依赖：`numpy>=1.24`、`psutil>=5.9`（见 `requirements.txt`）

```bash
pip install -r requirements.txt
```

## 快速开始

```bash
# 1. 生成确定性样本（manifest.jsonl + images/*.npy）
python -m mmpipe.cli generate ./work --num-pairs 12

# 2. 运行（或中断后恢复）四阶段管线
python -m mmpipe.cli run ./work

# 3. 独立复核快照内容、跨阶段一致性与恢复结果
python -m mmpipe.cli verify ./work

# 4. 查看某次资源裁决的完整依据
python -m mmpipe.cli arbitrate --stage fusion --pair-count 12 \
    --requested-workers 4 --requested-batch 8 --max-batch-pairs 2

# 5. 本地损坏注入（仅用于恢复/业务码验证）
python -m mmpipe.cli corrupt ./work text --mode tamper

# 一键验收演示（可复现、中断恢复、预算、损坏）
python run_demo.py
```

## 四阶段语义

| 阶段 | 输入 | 处理 | 输出 |
| --- | --- | --- | --- |
| `image` | 种子生成的 RGB 像素 | 分块均值池化 → 固定权重投影 | `image_dim` 维 float32 |
| `text` | 受控词表 token 序列 | 词频加权池化（按 token 序累加） | `text_dim` 维 float32 |
| `fusion` | 图像/文本嵌入 + `modality_mask` | mask=1/2 拼接（缺失侧补零），mask=3 加权门控 | `fusion_dim` 维 float32 |
| `score` | 融合嵌入 | 与固定参考向量余弦相似度 + 偏置 | 0 维 float32 标量 |

`modality_mask` 合法取值：`1` 仅图像、`2` 仅文本、`3` 双模态门控；其他值返回
`E_MASK_INVALID`。所有权重矩阵由 `master_seed` 经 `numpy.random.default_rng` 一次性
生成、只读、跨进程共享。

## 确定性保证

- 全部计算使用 float32，固定累加顺序，输出为 C 连续数组。
- 样本像素仅由 `image_seed` 派生；期望形状同样由种子派生，不符返回 `E_SEED_SHAPE`。
- 处理顺序为稳定排序 `(-priority, pair_id)`；分块边界固定，进程池 `map` 保序，归并顺序确定。
- 进程池使用 `spawn` 启动方式，跨平台行为一致，不继承父进程随机状态。
- 快照校验和按 `pair_id` 排序后，对每个 `.npy` 的 SHA-256 再聚合求 SHA-256。
- 配置经规范化 JSON 求摘要写入快照；配置变化会使旧快照判定为整体替换。

## 快照、提交与恢复

磁盘布局：

```
work/snapshots/<stage>/
    v<version>/
        manifest.json      # 版本、pair 集合、校验和、配置摘要
        COMMITTED          # 原子可见标记（os.replace 发布）
        embeddings/<pair>.npy
    .tmp-<version>/        # 暂存区，永远不会被视为已提交
```

- 只有该阶段**全部 pair** 完成并通过校验才提交；`COMMITTED` 的原子重命名是唯一可见点。
- 重启时读取最后一个已提交快照：已提交 pair 标记为 `reused`，绝不重算或重复计入；
  未提交的 `.tmp-*` 暂存区一律清空并从该阶段起点重建。
- 版本号单调递增；检测到版本/校验和/配置/pair 集合变化时**整体替换**旧版本，不做增量修补。
- 损坏或校验和不符返回 `E_SNAPSHOT_CORRUPT`，错误体带 `stage` 与 `version`，禁止使用部分可解析数据。
- `run --force-rebuild` 会删除损坏快照并从该阶段重建（显式修复路径）。

## 资源预算与裁决

`BudgetConfig` 提供可配置硬上限：`max_workers`、`max_batch_pairs`、
`max_resident_embeddings`（驻留嵌入字节）、`max_stage_pairs`（单阶段 pair 数）、
`max_parallel_tasks`（并行槽位）。

- 裁决器是观测与预算的**纯函数**，同一观测必然得到同一决策，可独立单测。
- 优先级固定为 `("progress", "memory", "parallelism")`，可显式配置顺序。
- `overflow_policy="reject"`：超限直接 `E_RESOURCE_LIMIT`。
- `overflow_policy="degrade"`：先收缩批大小（`batch_size_reduced`），再收缩 worker
  （`worker_count_reduced`），必要时标记 `parallel_tasks_reduced`，并在
  `run_report.json` 的 `scheduling.rationale` 记录裁决依据与降级来源。
- psutil 指标记录在每阶段 `resource_observation`，仅用于可观测性，不参与数值计算。

## 业务码

| 代码 | 触发条件 |
| --- | --- |
| `E_DUP_PAIR` | 重复 `pair_id` |
| `E_MASK_INVALID` | `modality_mask` 非 1/2/3 |
| `E_SEED_SHAPE` | `image_seed` 与 `image_shape` 不匹配 / 像素漂移 |
| `E_TEXT_EMPTY` | `text_tokens` 为空（worker 阶段归因到 pair） |
| `E_PAIR_ID_INVALID` | pair_id 缺失或含不安全字符 |
| `E_HINT_INVALID` | `stage_hint` 非法 |
| `E_SNAPSHOT_CORRUPT` | 快照缺失/校验和/配置摘要不符，带阶段与版本 |
| `E_RESOURCE_LIMIT` | 预算硬上限超限 |
| `E_INCONSISTENT` | 跨阶段集合/计分基数/复用重算/重放字节不一致，带类别与版本 |

worker 异常以结构化 `PairFailure`（code/message/stage/pair_id）回传主进程，单 pair
失败不静默丢弃，且失败阶段不产生提交。

## 本地故障注入

- 强制终止：设置环境变量 `MMPIPE_KILL_STAGE` 与 `MMPIPE_KILL_PAIR`，对应 worker 在
  该 pair 处理时 `os._exit(137)`，阶段提交前死亡，仅留下未提交暂存区。
  Windows 上等价于进程直接终止，重启恢复逻辑完全相同。
- 快照损坏：`python -m mmpipe.cli corrupt <work> <stage> --mode {tamper,truncate,manifest}`
  或调用 `mmpipe.fault_tools.corrupt_snapshot`。

## 一致性自检（独立验证工具）

`verify` 不信任管线簿记，重新打开每个已提交快照并重算校验和，检查：

1. 四阶段 `pair_id` 集合完全一致且规范有序；
2. 融合阶段每条 pair 恰被计分一次（无重复、无遗漏）；
3. 复用集合与重算集合不相交且并集覆盖全部受影响 pair；
4. 用独立重放逐字节比对每个嵌入与分数；

任一违反抛出 `E_INCONSISTENT`，含 `category`（如 `pair_set_mismatch`、
`score_cardinality`、`recompute_overlap`、`replay_bytes`）与快照版本号。

## 测试

```bash
python -m unittest discover -s tests -v
```

33 个测试覆盖：阶段纯函数与确定性、清单各业务码、快照原子提交/损坏/版本替换、
预算裁决与降级、硬杀恢复逐字节一致、pair 失败归因、独立一致性复核。

## 模块布局

```
mmpipe/
    config.py       # 管线/预算配置、裁决决策数据类
    weights.py      # 固定权重、种子派生形状与像素生成
    manifest.py     # 样本生成、JSONL 读写、清单校验、规范排序
    stages.py       # image/text/fusion/score 四阶段纯函数
    workers.py      # 进程池 worker 与强制终止注入点
    scheduler.py    # 预算准入、确定性裁决、分块
    store.py        # 快照存储、原子提交、校验和、版本管理
    pipeline.py     # 编排、恢复、报告与 provenance
    consistency.py  # 独立跨阶段一致性自检与重放复核
    resources.py    # psutil 资源观测
    fault_tools.py  # 本地损坏注入
    cli.py          # 命令行入口
tests/              # unittest 测试套件
run_demo.py         # 一键验收演示
```

