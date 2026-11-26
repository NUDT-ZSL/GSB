# KV 缓存分层换出账本与恢复引擎

本目录交付了一个纯本地、确定性的 Python 3.10+ 实现。SQLite 使用标准库 `sqlite3`；固定种子生成器可使用环境中的 `numpy 2.0.0`，核心账本仅依赖标准库以保证跨进程结果稳定。

## 一键验收

```bash
python3 scripts/generate_samples.py samples
./run_demo.sh
```

`run_demo.sh` 会实际执行：

- 固定种子生成请求、显存配置、v1/v2 策略、分片哈希、SQLite 代数元数据；
- `load`、`merge`、多轮 `evict`、`status`、`verify`；
- 真 SIGKILL 注入与 `resume`；
- 两个并行换出/请求进程；
- “启动后失败”故障注入；
- 策略临时表阶段 SIGKILL、全量替换、v3 增量追加、旧版本回退拒绝；
- `unittest` 回归；
- 三份独立工作区的账本摘要比对。

最新一次完整结果保存在 `artifacts/validation/21-validation-summary.json`。

## 手动命令

```bash
WS=run/demo
CLOCK=1795737600000

python3 -m kvledger.cli generate-samples --out samples
python3 -m kvledger.cli --workspace "$WS" load \
  --requests samples/requests.json \
  --config samples/memory_config.json \
  --clock-ms "$CLOCK"
python3 -m kvledger.cli --workspace "$WS" merge \
  --package samples/policy_package \
  --clock-ms "$CLOCK"

for i in $(seq 1 14); do
  python3 -m kvledger.cli --workspace "$WS" evict \
    --round-id "R$i" --clock-ms $((CLOCK + 600000 + i * 1000))
done

python3 -m kvledger.cli --workspace "$WS" resume
python3 -m kvledger.cli --workspace "$WS" status
python3 -m kvledger.cli --workspace "$WS" verify
```

也可使用 `./kvswap ...` 或 `python3 main.py ...`。六个必需子命令为 `load`、`merge`、`evict`、`resume`、`status`、`verify`；另有样本生成命令 `generate-samples`。

## 定点账本约定

样本配置：

```json
"fixed_point": {"scale": 48, "rounding": "floor"}
```

本实现明确约定 `scale=48` 表示一个整数定点账本单位对应 48 实际字节。对请求/层的上一次余量 `carry_in`：

```text
fixed_delta, carry_out = divmod(actual_bytes + carry_in, 48)
```

余量按 `(seq_id, layer_id)` 持久化并参与下一次换算，因此恒等式为：

```text
total_bytes = 48 * fixed_units_cumulative + carry_bytes
```

每笔换出输出 `actual_bytes`、`fixed_value`、`carry_in`、`carry_out`、`error_item_bytes` 和保守累计上界 `error_bound_bytes`。由于余量被保留，锐利误差是当前 `carry_bytes < 48`；状态接口同时给出按每次 floor 累积的保守上界。

分层容量由 `total_memory_bytes * ratio` 得到精确 Decimal 值，整数容量向下取整，小数字节余量和固定点舍入余量均写入 `ledger_conversions`。

## 策略合并与版本绑定

- v1：单文件 JSON，校验非负整数 `effective_after_seconds` 和有限正阈值；相对秒按注入时钟换算为 `clock_ms + seconds * 1000`，舍入方向为 `exact`。
- v2：分片 JSON、manifest SHA-256、键摘要和 SQLite `shard_metadata` / `policy_metadata` 交叉校验；策略代数必须存在于元数据且按代递增。
- 同一策略键的裁决顺序为 `(absolute_clock_ms DESC, format_priority DESC, generation DESC)`。逻辑时钟相同时 v2 优先级高于 v1，所有输家写入 `rejected_sources`。
- 合并结果保留来源格式、来源版本、原始时间表示、阈值、策略代数和被否决来源。
- 全量替换先写版本化 `policy_versions/policy_entries` 与临时 stage，再在同一 SQLite 事务中切换 `meta.active_policy_version`。
- 增量快照复制旧版本未变更键、覆盖传入键并应用墓碑。
- 旧 manifest 版本重放返回 `POLICY_VERSION_ROLLBACK`；相同包重复合并为 no-op。
- 轮次创建时把当前策略指针绑定到 `rounds.policy_version`、`eviction_attempts.policy_version/generation` 和请求状态；策略替换不会改写未完成轮次的阈值。

## 换出调度与三类结局

候选顺序为 `priority ASC, seq_id ASC, cold_order ASC, layer_id ASC, token_index ASC`，保证请求优先级和稳定次序可复现。

每个候选在认领事务中依次复核：

1. 扣除同层其他 `writing` 预留后，显存压力是否仍存在；
2. 本轮 `writing + committed + 当前条目` 是否超过字节预算；
3. 本轮 `writing` 数是否达到并发上限。

不足时把候选记为 `abandoned`，原因分别为 `memory_pressure_cleared`、`byte_budget_exhausted` 或 `concurrency_slots_exhausted`。

结局字段明确区分：

- `planned`：已做账本规划、预算内但尚未启动；SIGKILL 后由 `resume` 重放；
- `writing` / `failed_started`：已原子认领并分配偏移，启动后写失败会记录错误且不扣显存；
- `abandoned`：调度时主动放弃并带原因；
- `committed`：NVMe 帧写入、摘要校验、显存扣减和幂等账本在同一收尾事务中提交。

NVMe 模拟文件使用 `KVSWAP01` 魔数、JSON 头、定长 payload 和 SHA-256 页脚。偏移在认领事务中由 `nvme_files.next_offset` 预留，`fcntl.flock` 串行化 `pwrite + fsync`。

## SIGKILL 恢复协议

关键状态转换：

1. 水位和每个 token 的分层增量、定点换算在一个事务中提交；
2. 候选规划在事务中写入；
3. 认领事务把候选改为 `writing`，并提交唯一文件偏移；
4. 帧真实写入并 fsync；
5. 收尾事务提交 `committed`、条目 `evicted`、请求层/全局层显存扣减和幂等账本。

因此 SIGKILL 后只有三种情况：

- 仍是 `planned`：未启动，恢复路径重新认领并写完整帧；
- 是 `writing` 且文件缺失/半写/摘要错误：在原预留偏移覆盖完整帧；
- 是 `writing` 但帧已完整：校验 SHA-256 后直接补提交。

已 `committed` 的记录受部分唯一索引 `uq_committed_entry` 保护，恢复查询不会选中。重复收尾通过 `idempotent_ledger` 的 `(ledger_key, response_hash)` 判定幂等或 `IDEMPOTENCY_CONFLICT`。

## 多进程一致性

- SQLite 使用 WAL、`synchronous=FULL`、`busy_timeout=30000`；
- 所有关键读改写使用 `BEGIN IMMEDIATE` 串行化；
- `kv_entries` 对应活动/已提交换出有部分唯一索引；
- `policy_applied.manifest_version` 是主键，并发同包合并只有一个实际替换，另一个返回 no-op；
- `nvme_files.next_offset` 在事务中分配，所有有偏移的记录接受区间不重叠校验；
- `fcntl.flock` 保护 NVMe 数据帧的写入/fsync；
- `--shard-count N --shard-index I` 可把稳定排序后的请求分给多个进程。

## `verify` 校验项

`verify` 输出稳定排序 JSON 和 `ledger_digest`，检查：

- 活动策略指针和版本化策略条目；
- 无残留 `planned/writing`；
- 请求 token 数与 KV 条目数；
- 请求层 `total = hbm + evicted`；
- 定点恒等式 `total = scale * fixed + carry`；
- 全局层显存与条目 SUM 一致；
- 各层 HBM 占用不超过容量；
- 总显存大于分层容量和；
- 幂等账本数等于 committed 数；
- 每个 committed 帧摘要有效；
- 所有已分配偏移区间不重叠。

## 最新实测摘要

固定样本包含 6 个请求、128 个 token、4 层，共 512 个 KV 条目；收敛后提交 320 笔换出。

```text
ledger_digest = 3cac10e53c539d9324d22508012447df0d9c997d3c991bbc79603edfc475add1
normal_ok     = true
crash_ok      = true
parallel_ok   = true
digests_match = true
unittest      = Ran 5 tests ... OK
```

真 SIGKILL 换出进程退出码为 137；杀死后 `status` 报告 `recovery_required` 和 30 个未完成尝试。`resume` 识别出 2 个 `writing`：一个帧完整、一个 `bad_magic` 半写，并补跑 28 个 `planned`。恢复后再跑预算轮次，摘要与正常/并行工作区逐位相同。

策略阶段 SIGKILL 退出码同样为 137；重启后状态中没有活动策略指针，重跑合并成功。v3 增量结果为 `policy_count=4, incoming_policy_count=1, tombstone_count=1`。旧 v1 包在活动版本为 3 时返回退出码 2 和 `POLICY_VERSION_ROLLBACK`。

启动后失败注入实测为：

```text
status=completed_with_failures
counts={'abandoned': 1, 'committed': 29, 'failed_started': 1}
```

## 主要代码位置

- `kvledger/seed.py`：固定种子样本和双格式策略包；
- `kvledger/validation.py`：输入/配置校验与错误码；
- `kvledger/policy.py`：v1/v2 解析、哈希、SQLite 元数据和跨代裁决；
- `kvledger/storage.py`：SQLite schema、WAL 事务和文件锁；
- `kvledger/ledger.py`：定点换算和 NVMe 帧；
- `kvledger/engine.py`：load/merge/token/规划/并发换出；
- `kvledger/recovery.py`：SIGKILL 后恢复；
- `kvledger/reports.py`：status/verify；
- `kvledger/cli.py`：六个核心子命令；
- `tests/test_engine.py`：校验、恢复、摘要一致性和幂等测试。
