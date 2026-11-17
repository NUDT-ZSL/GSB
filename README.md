# Trustman Offline

这是一个仅使用 Python 标准库实现的离线证书链信任管理应用。它不访问网络，不使用真实证书，不依赖 Docker 或外部服务；证书、信任锚快照和链声明全部来自本地确定性 JSON。

应用针对以下场景设计：

- 信任锚集合和策略版本频繁演化；
- 同一批链需要反复在最新快照上重算；
- 吊销条目迟到后必须使相关链更新结论；
- 非法条目不能阻断其余条目的处理；
- 并发度、任务完成顺序和快照导入顺序不能改变最终结论；
- 进程强制终止后只恢复到最后一个完整、校验通过的检查点。

## 环境

- Windows 10/11 或当前 Linux 验证环境。
- Python 3.11 及以上。
- 运行应用只需要标准库。
- 测试需要 `pytest`。

本工作区的实际验证环境为 Python 3.12.13、pytest 9.2.0。代码未使用 3.11 之后才引入的语法。

```powershell
python --version
pytest --version
python -m pytest --collect-only -q
```

## 目录

```text
trustman/
  atomic.py       # 跨平台文件锁、临时文件、fsync、读回校验、os.replace
  checkpoint.py   # 带 SHA-256 校验和的检查点信封
  cli.py          # 四个 CLI 子命令及退出码
  engine.py       # 编排、检查点兼容性、重放、报告与对账
  models.py       # 数据模型、规范化、稳定 JSON
  parser.py       # 三类输入的严格解析和逐条拒绝
  samples.py      # 离线确定性样本生成器
  scheduler.py    # 稳定准入、预算、队列和真实耗时限制
  store.py        # 快照候选的原子持久化
  validation.py   # 路径构建、结论分级、吊销/过期传播
tests/            # pytest 测试集
samples/          # 可重复生成的演示输入
```

## 快速开始

在项目根目录执行：

```powershell
python -m trustman.samples --out samples

python -m trustman validate `
  --workspace .demo `
  --certificate samples/certificates.json `
  --chain samples/chains.json `
  --snapshot samples/snapshots/v1.json `
  --snapshot samples/snapshots/v2.json `
  --snapshot samples/snapshots/v3.json `
  --workers 4 `
  --max-tasks 1000 `
  --queue-limit 1000 `
  --report .demo/report.json

python -m trustman query --workspace .demo --chain-id chain-ok
python -m trustman check-checkpoint --workspace .demo
```

也可以先单独导入快照，再执行校验：

```powershell
python -m trustman import-snapshot `
  --workspace .demo `
  --snapshot samples/snapshots/v3.json `
  --snapshot samples/snapshots/v1.json `
  --snapshot samples/snapshots/v2.json
```

样本生成器只写固定内容。对同一版本重复生成，文件字节完全一致；`samples/manifest.json` 记录各输入文件的 SHA-256。

## 输入契约

所有输入对象都要求只出现规定的字段。非法条目产生 `E_SCHEMA` 拒绝记录，记录包含 `source_file`、零基 `entry_index`、一基 `entry_number`、稳定 `reason` 和消息；同文件其他合法条目继续处理。

### 证书描述

```json
{
  "schema": "trustman.certificates/v1",
  "certificates": [
    {
      "id": "leaf-ok",
      "issuer_id": "int-a",
      "public_key_fingerprint": "sha256:<64 hex>",
      "valid_from_ms": 1699999990000,
      "valid_to_ms": 1700000010000,
      "revoked": false
    }
  ]
}
```

约束：

- `id` 在所有输入文件中全局唯一；重复 ID 的所有出现均拒绝。
- `issuer_id` 必须能解析到一条合法证书。根证书使用自签引用，例如 `root-a -> root-a`。
- 指纹规范化为小写 `sha256:<64 hex>`。
- 有效期必须是整数毫秒，且 `valid_from_ms < valid_to_ms`。
- `revoked` 是布尔值；它与快照吊销集合合并后参与校验。

### 信任锚快照

```json
{
  "schema": "trustman.snapshots/v1",
  "snapshots": [
    {
      "version": 3,
      "policy_version": "1.2.0",
      "trusted_roots": ["root-a"],
      "revoked_entries": [
        {
          "cert_id": "leaf-late",
          "revoked_at_ms": 1699999999000,
          "reason": "late_revocation"
        }
      ],
      "observed_at_ms": 1700000000000
    }
  ]
}
```

约束：

- `version` 是大于等于 1 的整数。
- `policy_version` 是语义版本格式，例如 `1.2.0` 或 `2.0.0-rc.1`。
- `trusted_roots` 和 `revoked_entries` 都按集合语义规范化、排序。
- 每个快照都整体替换信任状态，不是增量补丁。
- 所有链默认使用版本号最大的快照。`observed_at_ms` 是有效期判定的唯一时间真值。
- 同一版本若出现冲突内容，选择 `content_fingerprint` 字典序最小者；其余候选进入拒绝清单。因此最终状态与到达顺序无关。

### 链声明

```json
{
  "schema": "trustman.chains/v1",
  "chains": [
    {
      "chain_id": "chain-ok",
      "certificate_ids": ["leaf-ok", "int-a", "root-a"],
      "deadline_ms": 1000,
      "expected_summary": "trusted:snapshot=3:policy=1.2.0"
    }
  ]
}
```

约束：

- 序列按“叶到根”排列，不得重复标识。
- 空序列在语法上可解析，但业务结果为 `E_CHAIN_EMPTY`，位置为 `0`。
- `deadline_ms` 是正整数，作为该任务真实耗时上限。
- `expected_summary` 与计算出的 `<conclusion>:snapshot=<version>:policy=<policy>` 比较。

## 结论与错误码

结论分三档：

- `trusted`：完整签发路径、受信根、有效期和吊销检查全部通过。
- `partially_trusted`：路径和信任锚成立，但链内存在过期、未生效或吊销的证书。应用仍会报告具体失效证书及其传播范围。
- `untrusted`：链结构、证书可用性、根信任或快照依据本身不成立。

稳定的路径错误码：

| 错误码 | 含义 | `position` |
|---|---|---|
| `E_CHAIN_EMPTY` | 链长度为零 | `0` |
| `E_MISSING_CERT` | 叶或终端证书不存在 | 缺失下标 |
| `E_MISSING_INTERMEDIATE` | 链中省略了实际签发者，或中间证书不存在 | 缺失/应插入下标 |
| `E_CHAIN_BROKEN` | 相邻证书的签发者与实际签发者不一致 | 子证书下标 |
| `E_ROOT_UNTRUSTED` | 终端证书不在生效快照的受信集合 | 终端下标 |
| `E_NO_SNAPSHOT` | 没有可用快照 | `null` |
| `E_EXPIRED_*` | 叶/中间/根在观测时间已过期 | 证书下标 |
| `E_NOT_YET_VALID_*` | 叶/中间/根在观测时间尚未生效 | 证书下标 |
| `E_REVOKED_*` | 叶/中间/根被吊销 | 证书下标 |

调度错误码：

| 错误码 | 含义 |
|---|---|
| `E_TIMEOUT` | 单任务超过真实耗时上限 |
| `E_BUDGET` | 超过整批累计任务预算 |
| `E_QUEUE_FULL` | 超过稳定队列准入上限 |
| `E_LOCK` | 工作目录写锁冲突 |
| `E_SCHEMA` | 输入结构非法 |

每条链都记录 `basis.snapshot_version`、`basis.policy_version`、`basis.observed_at_ms` 和快照内容指纹。即使任务超时、超预算或被拒绝，也会带当前生效快照依据。

## 并发、预算与队列

任务按 `(chain_id, source_file, entry_index)` 稳定排序。为保证结论和拒绝清单不随并发时序变化，队列上限实现为“稳定前缀准入上限”：

1. 前 `queue_limit` 条链允许入队，其余返回 `E_QUEUE_FULL`；
2. 入队链中前 `max_tasks` 条允许启动，其余返回 `E_BUDGET`；
3. 已允许启动的任务由 `workers` 个线程执行。

因此改变 `workers` 不会改变任何链的业务结论或调度准入结果。单任务时限使用 `time.perf_counter()` 从任务开始时测量真实耗时；生产路径没有任何人为 sleep 或固定延迟判定。链的 `deadline_ms` 与可选全局 `--task-timeout-ms` 取较小值。

一个任务超时或异常只影响自身，其他任务继续完成。报告不含耗时值、工作线程数或完成顺序，因此同一输入在不同并发度下可逐字节比较。

## 原子性、锁与检查点

工作目录写操作在同一把非阻塞独占锁下串行化：

- Windows 使用 `msvcrt.locking(..., LK_NBLCK, ...)`；
- POSIX 使用 `fcntl.flock(..., LOCK_EX | LOCK_NB)`；
- 锁冲突立即返回 `E_LOCK`，不写入任何状态或报告。

快照存储、检查点和报告都使用以下流程：

1. 写入同目录临时文件；
2. `flush` 和 `fsync`；
3. 重新读取并验证 JSON 内容；
4. 使用 `os.replace` 整体替换；
5. 在支持目录 fsync 的平台上同步目录。

检查点信封包含：

- `structure_version`；
- 有效策略版本与快照版本水位；
- 快照、证书、链和调度配置的稳定指纹；
- 已完成的链结论；
- 对 `{structure_version, payload}` 的 SHA-256 校验和。

恢复流程：

1. 校验信封结构和 SHA-256；
2. 若结构版本或有效策略版本变化，整体重建；
3. 若输入指纹或调度预算配置变化，整体重建；
4. 对已完成的业务结论做纯函数重放，字段不一致则整体重建；
5. 一致时跳过已完成链，只执行缺失任务；
6. 检查点目录中的中断临时文件会被识别并报告，但不会当作正式检查点使用。

进程被强制终止时，正式检查点要么是替换前完整版本，要么是替换后完整版本；不承诺退出报告。`check-checkpoint` 会返回校验结果、策略/水位信息和 `has_half_write_residue`。

## CLI 与退出码

四个子命令：

- `import-snapshot`：解析并原子合并快照；
- `validate`：并发校验链、写检查点与报告；
- `query`：从最近报告或检查点查询单链结论；
- `check-checkpoint`：校验检查点结构、校验和、策略与水位。

退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 全部条目处理成功、无输入拒绝、无超时/预算/队列拒绝，且期望摘要全部匹配 |
| `1` | 部分失败：存在输入拒绝、调度拒绝/超时/预算、锁冲突或期望摘要不一致 |
| `2` | 参数错误，例如文件不存在、参数值非法、必需参数缺失 |

业务结论为 `untrusted` 不等于程序失败；只要它与 `expected_summary` 一致且任务正常完成，`validate` 仍返回 `0`。

报告摘要保证：

```text
tasks_total = succeeded + timed_out + over_budget + rejected
rejections_total = input_rejections + task_rejections
```

`reconciliation` 同时核对逐条状态计数、链排序和快照排序。

## 测试覆盖

执行：

```powershell
python -m pytest --collect-only -q
python -m pytest -q
```

测试覆盖：

- 非法证书、快照和链条目逐条拒绝并继续处理；
- 快照版本到达顺序、同版本冲突和稳定排序；
- 样本结论、错误位置与期望摘要；
- 串行/并行报告逐字节一致；
- 叶吊销、中间证书吊销、过期及传播集合；
- 迟到吊销后重算；
- 快照观测时间替代系统时间；
- 真实耗时超时且不中止其他任务；
- 累计预算、稳定队列上限和并发不变性；
- 检查点复用、结构/策略变化重建、重放不一致检测；
- 临时半写残留识别；
- 子进程强制终止后的恢复；
- 文件锁冲突时返回 `E_LOCK` 且状态字节不变；
- CLI 退出码、查询、报告对账和样本字节确定性。

当前实际结果：

```text
$ python -m pytest --collect-only -q
14 tests collected in 0.02s

$ python -m pytest -q
..............                                                           [100%]
14 passed in 2.40s
```

确定性样本的当前验证结果：

```text
effective_snapshot_version = 3
effective_policy_version = 1.2.0
tasks_total = 9
succeeded = 9
timed_out = 0
over_budget = 0
rejected = 0
trusted = 1
partially_trusted = 4
untrusted = 4
expected_mismatches = 0
reconciliation = all true
```

其中：

- `chain-ok` 为 `trusted`；
- `chain-expired`、`chain-int-revoked`、`chain-late`、`chain-revoked-leaf` 为 `partially_trusted`；
- `chain-empty`、`chain-broken`、`chain-missing-intermediate`、`chain-root-untrusted` 为 `untrusted` 并带稳定错误位置；
- 吊销传播区分 `leaf` 与 `intermediate` 出现位置；
- 过期传播由快照 `observed_at_ms` 驱动。

串行 `workers=1` 与并行 `workers=8`、三种快照导入顺序、六种快照到达排列均生成完全相同的规范化报告。
