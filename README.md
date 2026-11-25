# ckptmgr

面向本地模型训练计划的确定性分片检查点管理组件。它只依赖 Python 3.8+
标准库（含 `sqlite3`），不加载真实模型、不访问网络，也不读取系统墙钟。

验证目标：

- 并行落盘调度受并发上限和每轮逻辑时间预算约束。
- 增量保存与同版本全量保存产生逐分片一致的字节和摘要。
- 落盘或清单提交阶段被 `SIGKILL` 后，`resume` 只重放未提交部分。
- 恢复同时执行“按清单拼接”和“逐层独立重建”两条路径并裁决一致性。
- 多进程保存/恢复通过 SQLite 事务和文件锁保持幂等。

## 快速开始

```bash
python3 scripts/generate_sample.py --out examples/data

WORK=$(mktemp -d)
python3 -m ckptmgr validate \
  --plan examples/data/plan.json \
  --strategy examples/data/strategy.json \
  --save-plan examples/data/checkpoint-manifest.json

python3 -m ckptmgr save --root "$WORK" \
  --plan examples/data/plan.json \
  --strategy examples/data/strategy.json \
  --save-plan examples/data/checkpoint-manifest.json \
  --version 1 --mode full --clock-ms 1000

python3 -m ckptmgr save --root "$WORK" \
  --plan examples/data/plan.json \
  --strategy examples/data/strategy.json \
  --save-plan examples/data/checkpoint-manifest.json \
  --version 2 --mode incremental --clock-ms 1100

python3 -m ckptmgr restore --root "$WORK" \
  --checkpoint demo --version 2 --clock-ms 1200

python3 -m ckptmgr restore --root "$WORK" \
  --checkpoint demo --version 2 --materialize "$WORK/restored-v2"

python3 -m ckptmgr verify --root "$WORK"
python3 -m ckptmgr status --root "$WORK"
```

`main.py` 与 `python -m ckptmgr` 等价。安装 `pyproject.toml` 后也可直接使用
`ckptmgr` 命令。

## 崩溃恢复示例

```bash
CRASH_WORK=$(mktemp -d)

# 进程在提交 2 个分片后由 SIGKILL 终止；shell 通常观察到退出码 137。
python3 -m ckptmgr save --root "$CRASH_WORK" \
  --plan examples/data/plan.json \
  --strategy examples/data/strategy.json \
  --save-plan examples/data/checkpoint-manifest.json \
  --version 1 --clock-ms 1000 \
  --crash-manifest examples/data/crash-write.json

python3 -m ckptmgr status --root "$CRASH_WORK"

python3 -m ckptmgr resume --root "$CRASH_WORK" \
  --plan examples/data/plan.json \
  --strategy examples/data/strategy.json \
  --save-plan examples/data/checkpoint-manifest.json \
  --version 1 --clock-ms 2000

python3 -m ckptmgr verify --root "$CRASH_WORK"
```

round 会持久化完整的 normalized plan 快照，因此也可以省略 `--plan`：

```bash
python3 -m ckptmgr resume --root "$CRASH_WORK" \
  --checkpoint demo --version 1 --clock-ms 2000
```

`crash-commit.json` 在全部分片提交完成、最终清单事务开始前终止进程。此时
SQLite 中 `rounds.status=committing`，但 `checkpoints` 和 `pointers` 仍为空；
`resume` 只做最终清单提交。

## 输入契约

### 训练计划 `plan.json`

```json
{
  "schema": "ckpt-plan/v1",
  "checkpoint": "demo",
  "global_seed": 20261126,
  "dtype": "float32",
  "layers": [
    {"name": "embedding.weight", "shape": [64, 32]},
    {"name": "encoder.0.weight", "shape": [32, 32], "dtype": "float32"}
  ],
  "strategy": {
    "shard_count": 4,
    "max_shard_bytes": 65536,
    "max_concurrency": 3,
    "round_budget_ms": 120,
    "clock_tolerance_ms": 250,
    "shard_base_cost_ms": 5,
    "bytes_per_ms": 4096
  },
  "checkpoint_manifest": {
    "versions": [
      {"version": 1, "changed_layers": ["*"]},
      {"version": 2, "changed_layers": ["encoder.1.weight", "decoder.bias"]},
      {"version": 3, "changed_layers": ["embedding.weight"]}
    ]
  }
}
```

约束：

- `layers` 中每层名称非空且全局唯一。
- `shape` 必须是非空的正整数数组；`nbytes = product(shape) * dtype_itemsize`。
- 合法 dtype：`bool`、`uint8/16/32/64`、`int8/16/32/64`、
  `float16/bfloat16/float32/float64`。接受 `fp16/fp32/fp64`、`bf16`、
  `half/float/double` 作为别名。
- `shard_count` 为正整数且不超过层数。
- `max_shard_bytes`、`max_concurrency`、`round_budget_ms` 为正整数。
- 版本号严格递增。`changed_layers` 中的层在对应版本递增一次 revision；
  `"*"` 表示所有层。revision 参与确定性参数派生。
- `strategy`、`checkpoint_manifest` 可由 `--strategy`、`--save-plan`
  覆盖；覆盖后的完整解析结果参与 `plan_digest`。

也支持以对象形式给出层（`{"embedding.weight": {...}}`），以及
`model_layers`、`parameters`、`dims`、`param_name` 等常见字段别名。

### 崩溃注入清单

```json
{
  "schema": "ckpt-crash/v1",
  "injections": [
    {"stage": "write", "after_committed_shards": 2, "signal": "SIGKILL"}
  ]
}
```

`stage=write` 支持 `after_committed_shards`；`stage=commit` 在最终事务前终止。
也支持 `--crash-stage write --crash-after 2` 命令行覆盖。

## 核心设计

### 确定性派生

每层参数物化材料：

```text
seed_material = canonical_json({
  domain, global_seed, layer_name, shape, dtype, revision
})
payload = SHAKE-256(seed_material, nbytes)
param_digest = SHA-256(domain || seed_material || payload)
```

层按 `SHA-256("ckpt-shard/v1\0" || layer_name)` 排序后按
`rank % shard_count` 分配，保证 `shard_count <= layer_count` 时没有空分片。

分片文件格式：

```text
"CKPTSH01" || uint64_be(header_len) || canonical_json(header) || payload
```

分片摘要为完整文件字节的 SHA-256。对象路径为
`objects/<digest[:2]>/<digest>.shard`，相同摘要只保存一份。落盘使用
同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`。

### 逻辑时钟与并行调度

- save/resume/restore 的时间判定只使用 `--clock-ms`/`--logical-clock-ms`。
- 逻辑成本：`shard_base_cost_ms + ceil(size_bytes / bytes_per_ms)`。
- 调度器在虚拟时间轴上按分片索引启动任务，同时最多
  `max_concurrency` 个任务运行。
- 任务只有在其虚拟开始时间早于 `start_ms + round_budget_ms` 时才启动；
  一旦启动就运行到完成，即使结束时间越过预算边界。
- 预算耗尽时不启动剩余分片，记录 `budget_unstarted` 和原因。
- 已启动任务失败记录为 `started_failed`；失败波次之后的已选任务记录为
  `abandoned`。三者互不混淆。
- 时钟水位持久化在 SQLite。显式时钟低于水位且超出
  `clock_tolerance_ms` 时返回 `E_CLOCK_REGRESSION`。

### 增量保存

- 比较目标分片与上一版本同 index 分片的摘要。
- 摘要相同且对象文件有效时直接复用，记录
  `geometric-shard-digest-match` 及 `source_version`。
- 即使不是增量基准，只要内容寻址对象已存在，也记录
  `content-addressed-object-hit`，不再写第二份。
- 未命中摘要的分片才进入本轮调度器。

因此增量保存和同版本全量保存写出的分片文件、分片摘要和参数摘要一致；
清单中的 timing/reuse 元数据可以不同。

### SQLite 账本

工作目录：

```text
<root>/
  .ckpt/ledger.sqlite3
  .ckpt/ledger.lock
  .ckpt/tmp/
  objects/<prefix>/<sha256>.shard
  manifests/<checkpoint>/<version>.json
```

主要表：

- `rounds`：版本、计划摘要/快照、模式、基准版本、逻辑时钟水位、预算、状态。
- `round_shards`：本轮所有分片意图、预期摘要、逻辑成本、运行结局、放弃原因。
- `objects`：内容寻址对象清单，`digest` 为主键。
- `checkpoints`：已提交版本、参数摘要、完整清单 JSON、清单摘要。
- `checkpoint_shards`：已提交分片清单及复用记录。
- `parameter_digests`：逐层摘要、shape、dtype、revision、位置。
- `reuse_records`：命中/未命中、来源版本、命中依据。
- `pointers`：每个 checkpoint 的最新版本指针。
- `clock_watermarks`：全局逻辑时钟水位。
- `events`：可诊断事件。

提交协议：

1. 在写分片前，单个事务写入 round 和全部 shard intent。
2. 每个分片先原子落盘，再单独提交 `objects` 与
   `round_shards.status=committed`。文件已 rename 但 DB 未提交时，
   `resume` 校验摘要后会直接采纳该文件。
3. 全部分片完成后设置 `rounds.status=committing`。
4. 最终事务一次性写入 `checkpoints`、`checkpoint_shards`、
   `parameter_digests`、`reuse_records`、round 完成状态和 pointer 替换。
5. DB 事务提交后再原子导出 manifest；若导出前崩溃，下一次 resume 会重建导出。

`save`/`resume` 使用独占 `flock`；`restore`/`status`/`verify` 使用共享
`flock`。SQLite 使用 WAL、`synchronous=FULL` 和 `BEGIN IMMEDIATE`。

### 恢复裁决

`restore` 在内存中完成全部校验后才返回、写 `--output` 或物化到
`--materialize`，不会输出部分结果。物化使用目标父目录中的临时目录构建，
完成后整体 rename：

1. 校验导出的 manifest 摘要与 SQLite 一致。
2. 校验分片文件存在性；缺失返回 `E_SHARD_MISSING`。
3. 校验文件摘要；损坏返回 `E_SHARD_DIGEST_MISMATCH`。
4. 校验 shard/layer 顺序；不一致返回 `E_STITCH_ORDER_MISMATCH`。
5. 校验 shape、dtype、payload 长度；不一致返回 `E_LAYER_SHAPE_MISMATCH`。
6. 从 manifest 内嵌的完整 normalized plan 重新派生全量参数。
7. 拼接路径与逐层独立重建路径必须逐字节一致，否则返回
   `E_REBUILD_MISMATCH`。
8. 实际参数摘要必须等于清单参数摘要，否则返回
   `E_PARAMETER_DIGEST_MISMATCH`。

`verify` 对 SQLite 中所有存活版本重复上述全量重建，并检查导出 manifest、
复用来源和孤儿对象。

## 错误码

CLI 失败时向 stderr 输出：

```json
{
  "ok": false,
  "error": {
    "code": "E_SHARD_MISSING",
    "message": "shard object file is missing",
    "details": {"checkpoint": "demo", "version": 1, "shard_id": "..."}
  }
}
```

| 错误码 | 退出码 | 含义 |
|---|---:|---|
| `E_PLAN_INVALID` | 2 | 计划 JSON/字段非法 |
| `E_PLAN_LAYER_NAME_DUPLICATE` | 2 | 层名重复 |
| `E_PLAN_SHAPE_INVALID` | 2 | shape 非正整数 |
| `E_PLAN_DTYPE_INVALID` | 2 | dtype 不在枚举内 |
| `E_PLAN_SHARD_COUNT_INVALID` | 2 | 分片数非法或超过层数 |
| `E_PLAN_STRATEGY_INVALID` | 2 | 策略数值/上限非法 |
| `E_PLAN_VERSION_ORDER_INVALID` | 2 | 版本未严格递增 |
| `E_SHARD_SIZE_EXCEEDED` | 2 | 分片超过 `max_shard_bytes` |
| `E_MANIFEST_INVALID` | 2 | 检查点清单非法 |
| `E_CRASH_MANIFEST_INVALID` | 2 | 崩溃注入清单非法 |
| `E_SHARD_MISSING` | 3 | 分片文件缺失 |
| `E_SHARD_DIGEST_MISMATCH` | 4 | 分片摘要不匹配 |
| `E_MANIFEST_DIGEST_MISMATCH` | 4 | 清单摘要不匹配 |
| `E_SHARD_FORMAT_INVALID` | 4 | 分片二进制格式非法 |
| `E_STITCH_ORDER_MISMATCH` | 5 | 拼接顺序错误 |
| `E_PARAMETER_DIGEST_MISMATCH` | 6 | 参数摘要不匹配 |
| `E_LAYER_SHAPE_MISMATCH` | 7 | 层 shape/nbytes 不匹配 |
| `E_REBUILD_MISMATCH` | 8 | 拼接路径与独立重建不一致 |
| `E_VERSION_ROLLBACK` | 9 | 版本回退 |
| `E_CLOCK_REGRESSION` | 10 | 逻辑时钟回拨超容差 |
| `E_CHECKPOINT_NOT_FOUND` | 11 | 检查点/账本不存在 |
| `E_CHECKPOINT_INCOMPLETE` | 12 | 存在未完成 round，应 resume |
| `E_CHECKPOINT_CONFLICT` | 13 | 同版本/同 round 的计划冲突 |
| `E_ROUND_BUDGET_EXHAUSTED` | 14 | 预算耗尽，存在未启动分片 |
| `E_SHARD_WRITE_FAILED` | 15 | 启动后失败或后续波次主动放弃 |
| `E_PLAN_DIGEST_MISMATCH` | 16 | resume 计划与中断 round 不一致 |
| `E_VERIFY_FAILED` | 17 | verify 聚合失败 |
| `E_REUSE_RECORD_INVALID` | 18 | 复用记录无依据或来源不一致 |

## 测试与验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q ckptmgr scripts tests main.py
bash scripts/run_validation.sh
```

本轮实际执行结果：

- `22` 项测试全部通过，覆盖解析校验、零写入失败、确定性逐字节一致、
  增量/全量等价、预算放弃、写阶段 `SIGKILL`、提交阶段 `SIGKILL`、
  多进程保存幂等、多进程恢复、错误码隔离和 verify 损坏检测。
- 样本计划：`9` 层、`4` 分片、每轮并发上限 `3`、逻辑预算 `120ms`。
- 版本 2 增量保存：`2` 个变化分片实际提交，`2` 个未变化分片命中复用；
  增量与全量版本 2 的 restore 输出完全相同。
- 写阶段崩溃在提交 `2` 个分片后触发，进程被 `SIGKILL`；`resume` 后对象数
  为 `4`、round 为 `complete`、`orphan_objects=[]`。
- 提交阶段崩溃后 `checkpoints=0`、`pointers=0`、round 为 `committing`；
  `resume` 后 pointer 原子指向版本 `1`。
- `4` 个并发 save 实例中 `1` 个真正提交、`3` 个幂等返回，最终仍只有
  `1` 个 checkpoint、`4` 个 checkpoint_shards 和 `4` 个对象。
- 缺失、摘要损坏、顺序篡改、参数摘要篡改分别返回
  `E_SHARD_MISSING`、`E_SHARD_DIGEST_MISMATCH`、
  `E_STITCH_ORDER_MISMATCH`、`E_PARAMETER_DIGEST_MISMATCH`。

## 目录

```text
ckptmgr/                    实现
scripts/generate_sample.py  固定种子样本生成器
examples/data/              已生成样本输入
tests/                      标准库 unittest 验收测试
main.py                     便捷 CLI 入口
pyproject.toml              可选安装配置
```
