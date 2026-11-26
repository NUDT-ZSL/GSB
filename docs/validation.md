# 实际验证记录

验证日期：2026-11-27（Asia/Shanghai）

环境：

```text
Python 3.10.12
NumPy 2.0.0
SQLite 3.37.2
```

## 1. 自动化测试

命令：

```bash
python3 -m unittest discover -v
```

结果：

```text
Ran 17 tests in 4.032s
OK
```

覆盖项：

- 固定种子生成器字节级确定性；
- revision 只改变指定 head 的 K/V；
- 重复 case、错误 shape、错误 dtype、非法块长；
- 省略显式数组路径时的本地约定解析；
- causal 分块计划；
- float64 分块 online-softmax 与整段参考一致；
- 增量修订与全量分块重算逐元素完全一致；
- 未变化 score tile 不调用打分函数；
- 账本版本、活动快照、新旧版本重放和回退拒绝；
- 相同修订不同内容拒绝；
- 相同修订不同 case 配置拒绝；
- 并发提交 `E_STAGE_BUSY`；
- 资源串行降级和 `E_RESOURCE_LIMIT`；
- `E_TOLERANCE_EXCEEDED` 首个差异位置；
- artifact 篡改检测；
- 源输入文件被覆盖后仍可从 validation 固化数组重放；
- CLI demo 结构化业务码。

## 2. 完整固定种子示例

命令：

```bash
python3 -m flashdiag demo \
  --data-dir data \
  --runtime-dir artifacts/flashdiag-demo \
  --max-parallel-q-blocks 8
```

顶层结果：

```json
{
  "business_code": "OK",
  "acceptance": {
    "incremental_vs_full_recompute": "bitwise_equal",
    "incremental_vs_whole_sequence": "within_tolerance",
    "ledger_replay": "ok"
  }
}
```

生成器指纹：

```text
0bdb0e62e2403de109c31f1f998c99db685118858b515211c02c17f45728cf09
```

完整运行输出保存在 `artifacts/flashdiag-demo-output.json`。

## 3. Causal case

任务：`causal-q16-kv16`

```text
seq_len=64, head_dim=16, num_heads=2
q_block_len=16, kv_block_len=16, causal=true
weight_revision 1 -> 2，只改变 head 1 从第 33 行开始的 K/V
```

结果：

| 项目 | 值 |
| --- | --- |
| base snapshot | v1 |
| revision snapshot | v2 |
| base output SHA-256 | `d793a2f23614c908054687bc4ab811f3d61f5d17b706e52b9e46bfee08a660f3` |
| revision output SHA-256 | `1930be165530599fd41b9191950bf2d8e5e26abddb9e8e6d192f26c48b592553` |
| changed heads | `[1]` |
| affected Q blocks | `h1:q2`, `h1:q3` |
| recomputed | 7 个 block 节点 |
| reused | 29 个 block 节点 |
| cache hits / misses（含 validation/plan） | 29 / 9 |
| ledger nodes | 38 |
| parallel Q-block tasks / workers | 2 / 2 |
| resource degraded | false |

重算清单：

```text
score:h1:q2:k2
score:h1:q3:k2
score:h1:q3:k3
norm:h1:q2
norm:h1:q3
writeback:h1:q2
writeback:h1:q3
```

head 0 的全部 score/norm/writeback，以及 head 1 的 q0/q1 与 q2/q3 中不依赖变化 KV block 的 score tile 均由缓存复用。

## 4. Non-causal case

任务：`noncausal-q12-kv8`

```text
seq_len=48, head_dim=8, num_heads=3
q_block_len=12, kv_block_len=8, causal=false
weight_revision 1 -> 2，改变 head 0 从第 8 行开始的 K/V
```

结果：

| 项目 | 值 |
| --- | --- |
| base snapshot | v1 |
| revision snapshot | v2 |
| base output SHA-256 | `043ee255806805bce7504b7c591fc22e18d09710c7ea15b0f640030573ba4f55` |
| revision output SHA-256 | `cec31c2e8b5ad3fe72e1c1e2a0336de2c55ace28ad8a0b5556909846b8ae412a` |
| changed heads | `[0]` |
| affected Q blocks | `h0:q0`, `h0:q1`, `h0:q2`, `h0:q3` |
| recomputed | 28 个 block 节点（20 score + 4 norm + 4 writeback） |
| reused | 68 个 block 节点 |
| cache hits / misses（含 validation/plan） | 68 / 30 |
| ledger nodes | 98 |
| parallel Q-block tasks / workers | 4 / 4 |
| resource degraded | false |

非 causal 下 head 0 的变化 KV block 被所有 Q block 使用；head 0 的 KV block 0 仍可复用，head 1/2 的全部 block 节点复用。

## 5. 重放与一致性

两个 case 的 active snapshot 均通过：

```text
ledger_replay_verification.ok = true
```

验证内容：

- 重新推导 validation、plan、score、norm、writeback 缓存键；
- 校验所有 artifact manifest、数组文件、节点依赖和快照校验和；
- 由缓存 score 重新合并 norm；
- 由 norm 重新归一化 writeback；
- 重新拼接输出并比对 output SHA-256；
- 增量输出与全量分块重算 `bitwise_equal`；
- 分块输出与整段参考在 `atol=1e-9, rtol=1e-9` 内一致；
- 合并 `max/denom` 与整段参考统计在 `atol=1e-9, rtol=1e-9` 内一致。

独立命令也返回 `business_code=OK`：

```bash
python3 -m flashdiag query --case-id causal-q16-kv16 --summary \
  --ledger artifacts/flashdiag-demo/ledger.db \
  --store artifacts/flashdiag-demo/store \
  --export-root artifacts/flashdiag-demo/exports

python3 -m flashdiag verify --case-id noncausal-q12-kv8 \
  --ledger artifacts/flashdiag-demo/ledger.db \
  --store artifacts/flashdiag-demo/store \
  --export-root artifacts/flashdiag-demo/exports
```

## 6. 并发与资源错误路径

自动化测试通过额外连接持有 `BEGIN IMMEDIATE` 时运行 revision，后到提交返回：

```text
E_STAGE_BUSY
```

资源测试验证：

- `max_parallel_q_blocks=1` 时降级串行并标记 `resource.degraded=true`；
- `allow_serial_degrade=false` 时返回 `E_RESOURCE_LIMIT`；
- `max_resident_bytes=1` 时返回 `E_RESOURCE_LIMIT`。

账本篡改测试修改一个已提交 score artifact 的数组文件，重放返回 `E_CACHE_CORRUPT`。
