# shardpipe

`shardpipe` 是一个完全本地、确定性的训练样本分片缓存与并行预取调度控制面。它从 JSON 工作负载生成固定分片，完成缓存命中裁决、回源生成、预算内并发预取、batch 组装、SQLite 原子快照、中断恢复和串行真值校验。

实现不访问网络，不依赖外部数据集，不使用容器。核心调度决策不读取系统墙钟；时间只使用配置注入的逻辑起始值和步进。

## 快速开始

本工作区已用 Python 3.10.12 验证：

```bash
# 标准环境可直接使用 python3 -m venv
# 本机 ensurepip 不可用时，实际使用了已有 virtualenv：
/home/zsl/.feedback/python-base/bin/virtualenv \
  --clear --system-site-packages .venv
.venv/bin/python -c 'import numpy, pytest; print(numpy.__version__, pytest.__version__)'
# 1.26.4 8.3.3
```

初始化固定种子样本、缓存视图、作业与真值摘要：

```bash
.venv/bin/python scripts/init_workspace.py \
  --output data/sample.json --seed 20261121
```

生成的文件：

- `data/sample.json`：本地确定性工作负载。
- `data/sample.summary.json`：工作负载摘要及各数据集生成指纹。
- `data/sample.sha256`：输入文件 SHA-256。

运行完整链路：

```bash
STATE_DIR=$(mktemp -d /tmp/shardpipe-demo.XXXXXX)

.venv/bin/python -m shardpipe load \
  --input data/sample.json --state-dir "$STATE_DIR"

.venv/bin/python -m shardpipe schedule \
  --state-dir "$STATE_DIR" --job train-alpha --run-id alpha-1

.venv/bin/python -m shardpipe status \
  --state-dir "$STATE_DIR" --run-id alpha-1

.venv/bin/python -m shardpipe verify \
  --state-dir "$STATE_DIR" --run-id alpha-1
```

第二次运行同一作业会复用持久化缓存和 batch 内容：

```bash
.venv/bin/python -m shardpipe schedule \
  --state-dir "$STATE_DIR" --job train-alpha --run-id alpha-2
```

也可直接执行演示脚本，它每次创建新的临时状态目录：

```bash
bash scripts/demo.sh
```

## 输入模型

`data/sample.json` 是完整示例。核心字段如下：

```json
{
  "global_seed": 20261121,
  "datasets": [{
    "name": "alpha",
    "sample_count": 96,
    "feature_dim": 8,
    "sample_bytes": 64,
    "cacheable": true,
    "shard_size": 16,
    "version": "v1",
    "generation_params": {"salt": "alpha-local-v1"}
  }],
  "cache": {
    "generation": 3,
    "version": "demo-cache-v3",
    "entries": [{
      "dataset": "alpha",
      "shard_index": 0,
      "generation": 3,
      "bytes": 1024,
      "last_used_clock": 10,
      "digest": "<sha256>",
      "generation_fingerprint": "<dataset fingerprint>"
    }]
  },
  "jobs": [{
    "name": "train-alpha",
    "dataset": "alpha",
    "batch_size": 8,
    "epochs": 1,
    "data_parallelism": 2,
    "prefetch_depth": 2,
    "allow_shard_reuse": true
  }],
  "scheduling": {
    "logical_time_budget_ms": 64,
    "per_dataset_prefetch_limit": 2,
    "global_memory_budget_mib": 8,
    "cache_capacity_entries": 20,
    "tolerance_ms": 5,
    "logical_clock_step_ms": 2,
    "logical_clock_start_ms": 0,
    "generation_cost": {
      "base_ms": 1,
      "bytes_per_ms": 2048,
      "hit_ms": 0
    }
  }
}
```

每个 job 当前绑定一个数据集；多个 job 可以在同一持久化状态中并行调度不同数据集。字段别名如 `dataset_name`、`num_samples`、`world_size` 也被解析器支持。

校验包括：

- 数据集名和作业名唯一。
- 样本数、特征维度、样本字节数、分片大小、batch size、DP 度为正整数。
- 分片大小整除样本数。
- 预取深度非负，可缓存和分片复用标志为布尔值。
- 缓存条目字节数等于 `sample_bytes * shard_size`。
- 缓存摘要与确定性分片内容一致。
- 同一数据集内缓存代数单调；跨 `load` 操作代数不得回退。
- 非法输入返回稳定业务错误码，且不写入 SQLite 或缓存目录。

## 确定性与内容寻址

数据集生成指纹包含：

```text
global_seed, dataset name, dataset version, sample_count, feature_dim,
sample_bytes, shard_size, generation_params
```

每个样本由 SHA-256 计数器流生成：

- 前 `feature_dim * 4` 字节解释为小端 float32，并映射到 `[0, 1)`。
- 其余字节作为确定性 padding，总长度严格等于 `sample_bytes`。
- 分片边界固定为 `[shard_index * shard_size, (shard_index + 1) * shard_size)`。
- 分片摘要为完整分片字节的 SHA-256。

缓存键为：

```text
(dataset, shard_index, cache_generation, generation_fingerprint)
```

batch key 包含 job、数据集指纹、epoch、worker、局部下标和样本下标；batch digest 是 batch 内有序样本记录字节的 SHA-256。SQLite 对 `batch_digest` 建唯一索引，因此相同 batch 内容跨 epoch 或重复提交只记录一次全局结果，但每个 run 仍保留自己的 batch 出现记录。

## Worker 与 batch 规则

### 允许跨 worker 分片复用

全局 batch 流按 epoch 和固定 batch size 排列，batch `k` 分配给 `worker = k % data_parallelism`。各 worker 的样本集合不相交；跨 batch 边界的同一分片可以被多个 worker 引用，物理生成和缓存只发生一次。

### 禁止跨 worker 分片复用

分片按 `shard_index % data_parallelism` 独占分配给 worker，每个 worker 在 epoch 内按分片序号和样本序号读取。任何分片只属于一个 worker，因此不需要跨 worker 复用。

两种模式都满足：

- batch 内样本顺序固定。
- 任意样本只属于一个 worker。
- 多次构建相同输入得到相同 worker、分片、batch 和样本映射。
- 每个 batch 记录来源分片和样本序号。

## 缓存、预算与逻辑时钟

一轮调度按 `prefetch_depth` 将 worker 的局部 batch 划分为 wave。每轮：

1. 按最早使用 batch 和分片序号排序候选分片。
2. 以缓存键查找持久化内容或缓存视图；命中不执行生成。
3. 为候选分片预占全局内存；内存不足时当前及本轮剩余未启动分片标记为 `abandoned`。
4. 使用每个数据集的并发预取上限进行逻辑并发槽位模拟。
5. 只启动可在本轮逻辑时间预算内完成的分片；其余标记为 `not_started`。
6. 已启动分片在线程池中实际执行，并等待全部启动任务结束。
7. 生成任务由每分片文件锁保护，只有一次内容提交可成功。
8. 将本轮结果、分片、batch、缓存更新、内存账目和时钟水位放入单个 SQLite 事务，并整体替换快照指针。

逻辑耗时：

```text
generation_cost = base_ms + ceil(shard_bytes / bytes_per_ms)
duration        = ceil(generation_cost / logical_clock_step_ms) * step
```

命中耗时为 `hit_ms`。所有逻辑时间均从注入的 `logical_clock_start_ms` 开始推进，核心不读取 `time.time()`、`monotonic()` 或 SQLite 当前时间。

结局分类：

- `generated`：缓存未命中并成功提交。
- `hit`：缓存视图或内容缓存命中。
- `started_failed`：已启动但生成、提交或实际等待超时失败。
- `not_started`：逻辑时间预算不足，未启动。
- `abandoned`：全局内存预算耗尽，主动放弃。

实际等待使用逻辑预算和 `tolerance_ms` 换算出的等待时限。只有“逻辑时钟达到/超过轮次 deadline”且“实际等待仍未完成”才记录实际超时；即使超时，已启动任务也会继续执行完毕。

内存账目按每个 run 中成功装载的唯一分片字节累计。峰值同时包含已启动但最终失败的分片占用；失败分片不进入最终 resident 账目。

## 持久化与中断恢复

状态目录结构：

```text
state/
  control.db
  locks/
  cache/
    blobs/
    manifests/
    tmp/
    orphans/
```

SQLite 使用 WAL、`FULL` synchronous 和 `BEGIN IMMEDIATE`。关键表：

- `runs` / `rounds`：run 与每轮逻辑结果。
- `run_shards`：run 已提交分片清单。
- `run_batches`：run 已提交 batch 出现记录。
- `batch_results`：按 batch digest 去重的全局内容结果。
- `cache_entries`：当前缓存视图及代数、摘要、最近逻辑时钟。
- `shard_contents`：全局内容寻址 blob 登记。
- `memory_ledger`：每轮新增分片内存账目。
- `snapshots` / `current_snapshots`：完整轮次快照及当前指针。

blob 和 sidecar manifest 均通过临时文件加 `os.replace` 原子落盘。分片生成使用 `fcntl.flock` 的每键文件锁；同一分片同一缓存键在多个进程中只有一个提交赢家，其他实例复用既有内容。不同 run 使用独立 run 文件锁。

恢复行为：

- 读取当前快照指针，只看到完整已提交轮次。
- 未进入快照的内容 blob 不会重复计入内存。
- 完整 sidecar 但尚未登记 SQLite 的 blob 被视为半提交缓存，恢复时登记并复用。
- 恢复扫描不移动未配对 blob，避免与另一个进程“已写 blob、尚未写 manifest”的正常窗口竞争。
- `run_shards` 唯一约束保证同一 run 分片内存只计一次。
- 已提交分片不会重新生成；只重放未完成轮次和缺失分片。

故障注入用于验证恢复：

```bash
SHARDPIPE_FAILPOINT=after_blob_write  # blob+manifest 已写，SQLite 未登记，exit 92
SHARDPIPE_FAILPOINT=before_round_commit  # 内容已提交，轮次事务未提交，exit 91
SHARDPIPE_FAILPOINT=after_round_commit   # 快照事务已提交，exit 91
```

## CLI

```bash
python -m shardpipe load     --input data/sample.json --state-dir state
python -m shardpipe schedule --state-dir state --job train-alpha --run-id run-1
python -m shardpipe resume   --state-dir state --run-id run-1
python -m shardpipe status   --state-dir state --run-id run-1
python -m shardpipe verify   --state-dir state --run-id run-1
```

说明：

- `schedule --input FILE` 会先校验并加载文件；不传 `--input` 时使用最近加载的 workload。
- 不传 `--run-id` 时生成确定性的顺序 run id。
- 对已完成 run 再次执行 `schedule` 返回 `already_complete`。
- 相同 run 被另一个进程持锁时返回 `RUN_BUSY`。
- 所有命令输出稳定排序 JSON。

`status` 按数据集给出：

- 缓存命中率、命中/未命中数。
- 已生成分片数、复用字节数。
- resident 内存。
- 放弃分片、未启动分片、失败分片。
- 轮次逻辑时钟、内存峰值、快照版本。
- 失效触发来源、被复用项清单和 batch 缓存命中。

`verify` 会：

- 重新单进程串行计算所有 batch 真值。
- 逐 batch、逐样本摘要和来源分片比较。
- 在首个差异处输出 batch、batch 内样本位置、两侧前缀摘要和中间状态。
- 校验跨 worker 样本不重复。
- 校验 batch digest 全局唯一记录。
- 校验 resident/peak/ledger/快照账目一致。
- 校验逻辑时钟单调、每轮预算、快照版本连续、当前指针最新。
- 校验缓存声明、确定性摘要和内容 blob。

资源裁决导致缺失 batch 时，可加 `--allow-incomplete` 验证已完成部分的一致性。

## 实际验证结果

环境：

```text
Python 3.10.12
numpy 1.26.4
pytest 8.3.3
```

全量测试：

```text
$ .venv/bin/python -m pytest -q
................................                                         [100%]
32 passed in 7.70s
```

覆盖内容：

- 非法 JSON/名称/数值/分片整除/缓存字节/摘要/代数回退，且非法 load/schedule 不创建状态目录。
- `load` 即时执行缓存容量上限并稳定淘汰低最近逻辑时钟条目。
- 分片生成逐位一致、float32 特征维度、两种 worker 映射、无重复样本、固定切分边界。
- 首次 alpha 运行：2 个缓存命中、4 个回源生成、12 个 batch。
- 第二次 alpha 运行：6/6 分片命中、12/12 batch 结果命中，batch digest 不重复记录。
- 数据集版本变化：alpha 的 6 个已缓存分片全部精确失效并重算；beta 4/4 分片复用、8/8 batch 摘要不变。
- 跨 epoch 相同 batch digest 只写一条全局 batch 结果。
- 1 MiB 内存预算下 800 KiB 分片已启动，后续 800 KiB 分片主动放弃；已提交 2 个 batch，内存峰值 819200 字节。
- 逻辑时间预算不足时产生 `not_started`，轮次逻辑耗时不超过预算。
- 并发槽位为 1 时，两个分片的逻辑启动区间确定为 `[0,2]`、`[2,4]`。
- 慢生成任务在逻辑 deadline 已到且实际等待超时后标记 `started_failed`，已启动任务仍执行完毕。
- 注入首个 batch 样本摘要差异后，`verify` 返回首个差异 batch、样本位置和两侧中间摘要。
- `after_blob_write` 强制终止后 `resume` 登记半提交缓存并完成，6 个分片内存只计 6144 字节。
- `before_round_commit` 强制终止后从上一快照恢复并完成。
- 两个进程并发调度同一作业，两者的 batch/分片均通过 verify；内容行与 batch 摘要均保持唯一。
- 两个进程并发调度 alpha/beta 不同数据集，验证通过；该场景连续重复 20 次均通过。
- CLI `status` 两次输出逐字节一致，并符合 `json.dumps(..., sort_keys=True, indent=2)`。

手工演示输出摘要：

```json
{
  "alpha_run_1": {
    "cache_hits": 2,
    "cache_misses": 4,
    "clock_ms": 4,
    "committed_batches": 12,
    "memory_bytes": 6144,
    "status": "complete"
  },
  "alpha_run_2": {
    "cache_hits": 6,
    "cache_misses": 0,
    "batch_cache_hits": 12
  },
  "beta_run": {
    "cache_hits": 1,
    "cache_misses": 3,
    "committed_batches": 8
  },
  "verify": {
    "checks_passed": 16,
    "first_difference": null,
    "ok": true
  }
}
```

## 目录

```text
shardpipe/
  workload.py       JSON 解析、规范化、业务校验与指纹
  determinism.py    样本/分片内容寻址与逻辑耗时
  batching.py       worker 计划、batch 计划、串行真值
  scheduler.py      缓存裁决、预算调度、逻辑时钟、恢复续跑
  storage.py        SQLite schema、事务、快照、幂等提交
  blobs.py          原子 blob/manifest 与半提交恢复
  locking.py        进程内线程锁 + fcntl 文件锁
  verification.py   真值逐样本比较与状态审计
  cli.py             load/schedule/resume/status/verify
scripts/
  init_workspace.py 固定种子样本与真值摘要
  demo.sh           可重复 CLI 演示
tests/              32 项单元、集成、故障注入与并发测试
```

## 边界说明

- 当前每个 job 绑定一个 dataset；不同 dataset 通过不同 job/调度实例并行。
- resident 内存在一个 run 内保留到结束；本轮放弃后不会在后续轮次重新尝试同一分片。
- 逻辑耗时为显式配置模型，不测量 CPU 真实耗时；真实等待只用于超时安全的二次判定。
- 初始缓存视图被视为已缓存内容，命中时读取其摘要而不要求本地 blob 已存在。
- `status` 的 cache hit rate 定义为 `hits / (hits + misses)`，不含主动放弃和未启动分片。
