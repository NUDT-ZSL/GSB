# 确定性本地训练数据管线

在单机上复现「原始样本分片生成 → 内容寻址缓存 → 并行预取 → batch 组装 →
中断恢复 → 验证报告」的完整链路。所有时间判定只使用注入的逻辑时钟毫秒值，
不读取系统墙钟；样本与分片摘要全部由确定性哈希派生，可逐位复现。

## 环境

- 目标环境：Python 3.11（含标准库 `sqlite3`），`python -m pip install --upgrade numpy`。
- 本交付机仅提供 Python 3.10.12 且无 root 权限，无法安装系统 Python 3.11。
  代码刻意只使用 3.10/3.11 公共标准库 API（`dataclasses`、`sqlite3`、
  `concurrent.futures`、`hashlib`、`fcntl/msvcrt`），在两个版本下行为一致，
  未使用任何 3.11 专属语法。`numpy` 对核心链路非必需，仅在环境中保留可用。
- 无需网络服务、GPU 或外部数据集；不使用 Docker。

## 目录

```
training_pipeline/
  datapipe/
    errors.py       # 业务错误码与统一异常
    hashing.py      # blake2b 确定性哈希
    model.py        # 数据集/缓存视图/作业/时钟/注入领域模型
    loader.py       # 五份 JSON 的解析与严格校验（落盘前失败）
    sharding.py     # 内容寻址分片与确定性样本切分
    cache.py        # 原子落盘缓存、命中裁决、精确失效清单
    clock.py        # 注入逻辑时钟（含真实等待与回拨容差）
    injection.py    # 故障注入引擎与一次性崩溃标记
    store.py        # SQLite 原子事务、文件锁、幂等提交、快照指针
    scheduler.py    # 并行预取、预算裁决、三类结局、逻辑超时
    assembler.py    # batch 组装、worker 分配、串行真值、分歧定位
    pipeline.py     # 端到端编排、轮次规划、恢复、status
    cli.py          # load/schedule/assemble/resume/status/verify
  generate_samples.py
  samples/          # 10 个确定性场景（生成产物）
  tests/
    test_pipeline.py      # 17 个 unittest 用例
    test_multiprocess.py  # 多进程并发一致性脚本
```

## 快速开始

```bash
cd training_pipeline
python generate_samples.py
python -m datapipe --input-dir samples/baseline --work-dir run/baseline load
python -m datapipe --input-dir samples/baseline --work-dir run/baseline \
  verify --report run/baseline-report.json
python -m datapipe --input-dir samples/baseline --work-dir run/baseline status
python -m unittest discover -s tests -v
python tests/test_multiprocess.py
```

Windows 多进程使用 spawn 启动，入口为 `python -m datapipe`，已做主模块保护。

## 关键设计

### 确定性分片（需求 2）

样本身份是 `(数据集, 全局种子, 生成参数, 内容版本, 分片号, 样本号, 特征维度)`
的 blake2b 摘要；样本字节大小在该身份上做有界确定性抖动。分片摘要是其样本
摘要的有序组合，`cache_identity` 再绑定代数输入。相同输入重复生成得到逐位
一致的清单，样本内容只由 `(数据集, 分片号, 样本号)` 决定，与预取顺序无关。

### 缓存键与增量失效（需求 3）

缓存键 = 数据集 + 分片号 + 缓存代数 + 生成参数。代数由
`content_version * 1_000_000 + noise` 派生；任一生成参数或内容版本变化都会
推进代数。命中直接复用，未命中回源生成并以「同目录临时文件 + `os.replace`」
原子落盘，SQLite 内 `IMMEDIATE` 事务原子登记。`evaluate_invalidation` 输出
失效触发来源与被失效/保留分片清单，未受影响分片继续命中。

### 并行预取与预算（需求 4）

轮次内先按数据集分组，组间与组内均用线程池并发，受每数据集
`max_concurrent_prefetch` 与全局 `data_parallel` 双重约束。预算裁决在单线程
内确定性预留：候选按固定顺序计入估计字节，一旦超预算，其后所有候选标记为
`skipped_budget`（未启动、主动放弃）；已启动分片必须执行完毕，结局严格区分
`completed/hit`、`failed/timeout`、`skipped_budget`。

### 逻辑时钟与超时（需求 5）

每条时钟步进同时给出 `advance_ms`（逻辑推进）与 `sleep_ms`（真实等待）。
超时判定在 `clock.advance()` 之后比较累计逻辑耗时与轮次预算，因此需要时钟
推进与真实等待共同成立，绝非事后标注。负 `advance_ms` 注入受控回拨，超过
`rollback_tolerance_ms` 抛 E3004；超时 E3002 与主动放弃、执行失败 E3001 使用
不同错误码，超时不会被记为完成。

### batch 一致性（需求 6）

worker 按固定分片序做取模分配，保证不重叠；样本按 `(数据集, 分片, 样本)`
确定性展平后按 `batch_size` 切分，同一 epoch 每样本恰出现一次。单进程串行
`serial_truth_batches` 是唯一真值，并行结果逐 batch 逐样本比对，分歧时输出
首个分歧批次、样本位置与两条路径的分片来源（E4002）。

### 持久化与恢复（需求 7）

轮次水位、已提交分片、缓存条目版本、内存账目、batch 水位、逻辑时钟水位均在
SQLite 事务内原子写入；轮次提交时在同一事务整体替换 `current_snapshot` 指针，
读取方只能看到完整已提交轮次。崩溃注入模拟进程被 kill，残留 open 轮次；
`resume` 对账已提交与缺失分片，只重放缺失项，幂等主键保证内存与缓存不重复
计入，恢复后 batch 与不中断运行逐样本一致。

### 多进程与幂等（需求 8）

`committed_shards`、`cache_entries`、`memory_ledger`、`batches` 均以业务主键
约束，重复提交相同分片/batch 幂等返回；摘要冲突分别抛 E5001/E4001。每个调度
实例按其拥有的数据集集合获得隔离的轮次索引命名空间，分片层仍靠 SQLite 事务
与咨询文件锁协调，保证同一分片同一代数只提交一次。

## 业务错误码

| 码 | 含义 |
| --- | --- |
| E1000 | 通用输入校验失败 |
| E1001 | 数据集/作业/缓存条目重名 |
| E1002 | 分片大小不能整除样本总数 |
| E1003 | 注入/作业/缓存引用了不存在目标 |
| E1004 | 缓存声明字节或摘要与确定性派生不一致 |
| E2001 | 生成摘要不匹配 |
| E2002 | 缓存代数回退 |
| E2003 | 缓存原子写失败 |
| E3001 | 预取执行失败 |
| E3002 | 逻辑时钟超时（区别于失败/放弃） |
| E3003 | 预算耗尽（报告用，非异常退出） |
| E3004 | 逻辑时钟回拨超容差 |
| E3005 | 时钟步进表耗尽 |
| E4001 | batch 摘要冲突 |
| E4002 | 与串行真值的 batch 分歧 |
| E4003 | 样本覆盖/缺失/重复 |
| E5001 | 持久化状态损坏或幂等冲突 |
| E5002 | 文件锁获取超时 |
| E6001 | 注入式强制终止（中断，退出码 3，可 resume） |

## 场景与验证结果

`samples/` 下场景：`baseline`、`invalidation`、`budget_exhausted`、
`fault_gen_digest`、`fault_cache_rollback`、`fault_cache_write`、
`fault_prefetch`、`fault_timeout`、`fault_clock_rollback`、
`fault_batch_conflict`、`fault_crash_generate`。

实测结论（详见交付时运行记录）：

- 基线：5 分片（2 命中 + 3 回源），2 轮，8 batch，与串行真值 8/8 摘要全匹配。
- 失效：`images#s000000` 因 `gen_params` 变更被精确失效，`text#s000000` 保留命中。
- 预算：首个分片占用后其余标记 `skipped_budget`，未启动且原因可核对。
- 超时：纯时钟推进 + 真实等待触发 E3002，无注入 fault 记录。
- 回拨：时钟步进回拨超容差触发 E3004。
- 崩溃恢复：仅重放缺失分片，内存/缓存零重复，恢复后 8 batch 与真值一致。
- 多进程：两实例合计 5 分片，`duplicate_shards=0`、`duplicate_ledger=0`。
- 17 个 unittest 全部通过。
