# 技术说明与验收记录

## 1. 总体设计

组件按“数据 → 门限 → 流水 → 计划 → 账本/快照 → 执行 → 自检 → 应用”分层，
所有模块仅依赖标准库与 NumPy，配置对象为不可变 dataclass，可 pickle 后
传入 `spawn` 子进程。

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 确定性底座 | `determinism.py` | splitmix64 种子派生、规范化 JSON、blake2b 指纹 |
| 数据 | `data.py` | JSONL 清单、域统计量、特征/权重数组 |
| 门限 | `threshold.py` | 严格/容差带策略、次正规数保护、非法配置拦截 |
| 流水 | `pipeline.py` | 标准化、插值迁移映射、梯度步进训练、目标域评估 |
| 调度 | `planner.py` | 成本/内存估算、三约束准入、裁决留痕、降级、分组 |
| 账本 | `ledger.py` | RLock 保护的原子扣减/释放/降级归还 |
| 快照 | `snapshots.py` | 版本号 + 校验和、原子落盘、整体替换、回退/损坏识别 |
| 并发 | `concurrency.py` | 非阻塞提交门 |
| 执行 | `executor.py` | spawn 池、波次分派、确定性归并、故障注入 |
| 自检 | `consistency.py` | 五类一致性核对 + 串行基线精度比对 |
| 编排 | `service.py` | 工作流门面、执行摘要、最近业务错误环 |

## 2. 确定性策略

1. 种子派生：`derive_seed(*parts)` 对每个参数 `repr` 后 blake2b 摘要，
   再经 splitmix64 混合；不使用 `hash()`、系统时间或环境随机源。
2. 随机流：每个用途独立派生子种子（`task-arrays`、`task-weights`、
   `split`、`target-eval`、`domain-stats`），互不污染。
3. 遍历顺序：任务在所有入口按 `task_id` 升序；字典/集合参与摘要前排序；
   域统计量以排序后的 `(domain, dim)` 键缓存。
4. 数值：统一 float64；标准化固定 `ddof=0`；训练为全批量固定步数梯度
   下降，梯度缓冲原地复用；切分排列由任务种子决定。
5. 指纹：每阶段记录数组形状、dtype、字节内容的 blake2b 指纹，最终分数
   也有独立指纹，支持逐字节一致性断言。

## 3. 适配流水数学定义

- 标准化：`z = (X - mean(X)) / max(std(X), floor)`
- 迁移映射（强度 `s`）：
  `M = z * ((1-s)*std_src + s*std_tgt) + ((1-s)*mean_src + s*mean_tgt)`
- 适配层：线性模型 `y = Xw + b`，损失 `MSE + λ||w||²`，全批量梯度步进，
  步数与学习率由 `adapter_kind` 决定（linear/lora_light/adapter_pro）。
- 评估：独立目标域评估集，`score = 1 / (1 + MSE)`，再过评估门限。

## 4. 三约束调度与裁决

- 计算代价（整数单元）：
  `(1 + steps) * effective_samples * input_dim + eval_count * input_dim`
- 内存（字节）：特征/标准化/映射/训练缓冲/权重/适配参数/梯度缓冲/
  目标评估缓冲，按 float64 逐项求和，公式字符串写入 `CostEstimate.basis`。
- 共享统计量：同 `(target_domain, input_dim)` 的只读统计量全局共享，
  仅按准入任务计一次驻留（`shared_stat_memory`）。
- 裁决键（默认顺序）：`priority`（小者优先）→
  `-shared_stat_benefit`（共享收益大者优先）→ `sample_count`（小者优先）
  → `task_id` 兜底，保证全序确定。
- 冲突主码优先级：槽位 → 计算 → 内存；纯计算冲突先按
  `min_sample_fraction` 二分搜索最大可容纳样本数做降级（路径 `degrade`），
  降级失败或其他冲突返回拒绝码，`arbitration_trail` 与 `paths` 全量留痕。
- 并行分组：按准入顺序以 `parallel_slots` 定长切波次，组内排序后提交。

## 5. 门限稳定性

- `strict`：仅在严格相等时进入 tie，按 `band_on_tie` 显式 accept/reject；
- `band`：`|value - threshold| <= tolerance` 统一按 tie 规则裁决，门限
  邻域内的浮点抖动不再改变结论；
- 次正规数：绝对值低于 `subnormal_floor` 的有限值夹到带符号地板；
- 非法门限（NaN、负容差、strict 带容差、未知策略）与非法迁移强度
  （非有限、越界）均抛 `E_CONFIG_INVALID`，上下文带 `field` 与 `value`。

## 6. 快照、并发与账本

- 快照信封：`{kind, version, checksum, payload}`，校验和为规范化载荷的
  blake2b；写入走临时文件 + `os.replace` 原子替换，旧版本归档到
  `history/`；加载时版本号或校验和变化即整体替换内存态。
- 损坏：JSON/结构异常或校验和不符抛 `E_SNAPSHOT_CORRUPT`（带 kind、
  version）；候选版本不高于当前版本抛 `E_SNAPSHOT_ROLLBACK`。
- 提交门：`Lock.acquire(blocking=False)`，冲突立即抛 `E_STAGE_BUSY`，
  不排队不等待。
- 账本：全部写操作在同一 RLock 内完成，`snapshot()` 持锁组装深拷贝，
  查询侧只能观察到完整状态；失败任务原子释放全部预留。

## 7. 进程池执行与异常回传

- `mp.get_context("spawn").Pool`，按波次 `pool.map(chunksize=1)` 分派，
  波次内与最终归并均按 `task_id` 排序，分块边界可复现。
- worker 是纯函数，任何异常收口为 `FailedTask(task_id, code, stage,
  message, context)`；同批其他任务结果不受影响，摘要中失败任务标记
  `status=unavailable`，且不泄漏到成功分数表。

## 8. 一致性自检（E_INCONSISTENT 的 category）

1. `admission_ledger`：准入集合 = 账本预留 ∪ 已释放失败任务，计算扣减
   与估算逐项可核对；
2. `shared_statistics`：共享键唯一且占用与计划一致；
3. `summary_membership`：摘要 task_id 集合与准入集合并集相同；
4. `rejected_leak`：被拒任务不得出现在摘要；
5. `serial_baseline`：成功任务并行结果对串行基线的 score/MSE 差在声明
   精度内，且阶段指纹完全相同。

## 9. 实际验证结果

环境：Python 3.10.12、NumPy 1.26.4、pytest 8.3.3，本机 Linux，无网络。

### 9.1 自动化测试

命令：`python -m pytest tests/ -q`

结果：**36 passed**。覆盖确定性复现、门限翻转保护、非法配置、三约束
拒绝码、降级、共享统计只计一次、账本原子性、快照版本/回退/损坏、
提交门并发、spawn 池与串行基线一致性、失败隔离、重启恢复。

### 9.2 常规运行（8 任务，2 槽位）

- 准入 8 / 拒绝 0，分 4 个波次；
- 账本 `compute 956000/2000000`、`memory 1265472/20000000B`；
- 自检 `serial_baseline.max_abs_score_diff = 0.0`（逐字节一致）。

### 9.3 预算冲突演示

收紧为 `compute=180000, memory=3000000B, max_queued=6`：

- 准入 3 个（含 1 个 `degrade` 降级任务）；
- 拒绝 5 个，业务码 `E_COMPUTE_BUDGET`，冲突类型与已用预算、共享键
   写入裁决依据；被拒任务不出现在分数表；
- 账本 `compute 179776/180000`，自检全部 ok。

### 9.4 并行异常回传演示

向 `task_000@adapter_train`、`task_001@target_evaluate` 注入故障：

- 同进程与真实 `spawn` 池（`--pool`）两种模式均返回
  `E_WORKER_FAILURE` + 阶段名；
- 其余 6 个任务正常出分，失败任务在摘要标记 `unavailable`，
  其账本预留被原子释放；自检全部 ok。

### 9.5 门限稳定性演示

门限 0.5：strict 下精确等点按 tie 规则裁决；band（容差 1e-9，tie=reject）
下 `0.5`、`0.5±1e-12` 全部稳定拒绝，`0.5+1e-8` 正常接受；连续 50 次
重复裁决序列完全相同（`repeat_stable=True`）。

### 9.6 快照重启

三类快照文件落盘后，新服务实例 `restore_from_snapshots()` 识别版本
`{plan: 1, ledger: 1, summary: 1}` 并整体恢复内存态；人为篡改校验和后
加载抛 `E_SNAPSHOT_CORRUPT`（测试用例固定覆盖）。

## 10. 复现实验命令

```bash
python -m pytest tests/ -q
python -m transfer_scheduler.app run
python -m transfer_scheduler.app demo-budget
python -m transfer_scheduler.app demo-failure --pool
python -m transfer_scheduler.app demo-threshold
```
