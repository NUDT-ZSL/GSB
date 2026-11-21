# probe-ledger

本地服务注册中心探测结果差分解算引擎。它不发起网络探测，只把本地注册清单、v1/v2 探测观测分片和注入逻辑时钟序列转换为可重放、可校验、可差分推进的实例状态账本。

核心特性：

- 观测按 `(instance_id, obs_seq, logical_clock_ms, digest)` 确定性归并。
- 健康、可疑、失联三态演进，连续失败、租约、退避阶梯和状态世代单调推进。
- 分片任意顺序加载、重叠校验、暂时缺片缓冲、封口后补洞。
- v1 相对延迟与 v2 绝对时刻/状态码集合/直方图摘要互操作。
- SQLite 临时表 + 批次号整体切换指针，JSON 快照临时文件原子替换。
- 强制终止后从已提交水位恢复，不重复递增失败计数。
- `verify` 检查指针、分片、缓冲区、时刻换算和状态世代历史。

## 快速开始

要求 Python 3.10+（推荐 3.11+），仅使用标准库运行；测试依赖 `numpy`、`pytest`。

```bash
python -m pip install -r requirements.txt

python -m probe_ledger load \
  --workspace workspace \
  --registry samples/registry.json \
  --clocks samples/clocks.json \
  --shard-dir samples/shards

python -m probe_ledger reconcile --workspace workspace
python -m probe_ledger status --workspace workspace
python -m probe_ledger verify \
  --workspace workspace \
  --registry samples/registry.json \
  --clocks samples/clocks.json \
  --shard-dir samples/shards
```

也可以安装命令入口：

```bash
python -m pip install -e .
probe-ledger status --workspace workspace
```

默认运行产物：

- `workspace/ledger.sqlite3`：权威事务账本。
- `workspace/ledger.json`：逐字节确定的账本快照。
- `workspace/diagnostics.json`：包含到达顺序的完整差异报告。

## 输入格式

### 注册清单

```json
{
  "schema_version": 1,
  "suspect_after": 1,
  "lost_after": 3,
  "clock_rollback_tolerance_ms": 5,
  "services": [
    {
      "service_name": "checkout",
      "instances": [
        {
          "instance_id": "checkout-a",
          "endpoint": "http://127.0.0.1:18101/health",
          "expected_status_codes": [200, 204],
          "lease_generation": 20,
          "state_generation": 1,
          "serialization_baseline": "canon-v1"
        }
      ]
    }
  ]
}
```

校验规则：

- 服务名、实例 ID 非空且唯一；实例 ID 全局唯一。
- 端点必须是 `http/https` 回环地址，端口为 `1..65535`。
- `expected_status_codes` 非空，元素为 `100..999` 整数。
- `lease_generation >= 0`，`state_generation >= 1`。
- 状态机阈值可放在清单根或实例内；默认 `suspect_after=1`、`lost_after=3`。
- 可配置 `backoff_base_ms`、`backoff_max_ms`、`lease_decay_per_observation`、`lease_refresh_on_success`。

### v1 观测

```json
{
  "schema_version": 1,
  "instance_id": "checkout-a",
  "obs_seq": 3,
  "logical_clock_ms": 1030,
  "status_code": 503,
  "latency_ms": 12,
  "digest": "<sha256>"
}
```

v1 相对延迟在注入逻辑时钟下换算为：

```text
observed_at_ms = floor(logical_clock_ms - latency_ms)
```

快照中的 `time_conversions` 保留 `scale=1`、`rounding=floor` 和换算后的绝对时刻，即使观测已提交、缓冲区已清空。

### v2 观测

```json
{
  "schema_version": 2,
  "instance_id": "inventory-a",
  "obs_seq": 2,
  "logical_clock_ms": 1020,
  "observed_at_ms": 1013,
  "status_codes": [200],
  "latency_histogram": {"0-10": 2, "10-20": 1},
  "latency_summary_ms": 8.5,
  "digest": "<sha256>"
}
```

v2 成功条件为：所有观测状态码都在期望状态码集合中。v1 的 `{status_code}` 与 v2 状态码集合必须完全相等才可能语义等价。

### 分片

```json
{
  "schema_version": 1,
  "stream_id": "fixed-probe-stream",
  "shard_id": "shard-01",
  "shard_index": 0,
  "shard_count": 4,
  "expected_shard_ids": ["shard-01", "shard-02", "shard-03", "shard-04"],
  "observations": [],
  "shard_digest": "<sha256>"
}
```

当 `shard_count` 对应的全部索引到齐（且声明的 `expected_shard_ids` 已覆盖）时自动封口；`reconcile --seal` 可强制封口。未封口时遇到序号缺口，引擎停留在缺口前，不把暂时缺片误判为永久缺失。

### 逻辑时钟文件

```json
{
  "clocks": [
    {
      "instance_id": "checkout-a",
      "obs_seq": 1,
      "logical_clock_ms": 1010
    }
  ]
}
```

观测可省略 `logical_clock_ms`，由时钟文件注入；若观测自带时钟且与时钟文件不一致，返回 `E_CLOCK_INJECTION_MISMATCH`。

## 摘要规范

所有摘要使用 UTF-8、SHA-256、64 位小写十六进制，可带 `sha256:` 前缀。

规范化 JSON 固定为：

```python
json.dumps(value, ensure_ascii=False, allow_nan=False,
           separators=(",", ":"), sort_keys=True)
```

数值规范化：整数值浮点数统一转换为整数，例如 `10.0` 与 `10` 都编码为 `10`。

观测摘要载荷：

- v1：`schema_version, instance_id, obs_seq, logical_clock_ms, status_code, latency_ms`
- v2：`schema_version, instance_id, obs_seq, logical_clock_ms, observed_at_ms, status_codes, latency_histogram, latency_summary_ms`
- `status_codes` 排序去重，直方图键排序。
- 摘要字段本身不参与摘要。

分片摘要覆盖完整分片对象，但排除 `shard_digest`、`content_digest`、`content_hash`。

账本中的 `last_basis.digest` 是跨版本稳定的语义摘要，覆盖实例、序号、结局和状态码集合；原始观测摘要保留在观测表和差异报告中。这样，稍后到达的 v1/v2 等价别名不会改变已提交迁移依据。

## 归并与演进规则

### 归并

实例内先按 `obs_seq` 分组，再按 `(logical_clock_ms, digest, shard_id)` 稳定排序。处理顺序为 `< 已提交水位` 的迟到观测先审计、`> 已提交水位` 的观测按序号推进。

- 完全重复或 v1/v2 语义等价：只产生一次状态迁移。
- 同版本同序号不同摘要：`E_CONFLICT_OBS`，双方摘要和到达顺序保留，不静默取舍。
- v1/v2 同序号语义不等价：`E_VERSION_CONFLICT`，输出 `difference_fields`，实例标记 `review_required=true` 和 `version_conflict=true`，不覆盖已提交状态世代。
- 迟到观测与已提交序号语义不一致：`E_OBS_SEQ_ROLLBACK`；逻辑时钟回拨超过容差：`E_CLOCK_ROLLBACK`。

跨越缺口的补洞观测只在分片封口后生成。缺失时钟在前后两个逻辑时钟之间做整数线性插值。

### 状态机

每次提交一个观测或补洞，状态世代 `state_generation` 严格加一，并写入唯一历史行。

- 成功：连续失败清零，退避清零，状态转为 `healthy`（租约为零时 `lost`）。
- 状态码不符：连续失败加一，按阈值进入 `suspect` 或 `lost`，退避阶梯为 `min(base * 2^(failures-1), max)`。
- 缺失：不冒充成功，不递增连续失败，健康实例转为 `suspect`；不把已失联实例升级。
- 租约：默认每个已裁决逻辑观测递减 1；为零时强制 `lost`。可配置成功续租。
- `healthy/suspect/lost` 均可在后续成功观测且租约仍可用时恢复为 `healthy`。

## 持久化与恢复

SQLite 主表按 `batch_id` 版本化，包括实例、分片、观测、历史、诊断。每次提交：

1. `BEGIN IMMEDIATE`。
2. 创建 `TEMP` 暂存表并复制当前批次。
3. 应用变更，分配新 `batch_id`。
4. 将暂存内容写入新批次。
5. 单条 `UPDATE pointer` 原子切换当前账本指针。
6. 提交事务后，以临时文件 + `os.replace` 发布规范化 JSON 快照和差异报告。

恢复时以 SQLite 当前指针为准：

- 实例 `watermark` 给出已提交观测水位。
- 未应用观测保留在待裁决缓冲区。
- `resume` 只重放水位之后的观测，不重复递增失败计数。
- 快照和诊断文件落后于 SQLite 时，`resume` 从权威批次重新物化。

## CLI

```text
probe-ledger load       --registry REG --clocks CLOCKS --shard FILE... --shard-dir DIR...
probe-ledger reconcile  [--seal]
probe-ledger resume
probe-ledger status     [--instance ID]
probe-ledger verify     [--registry REG] [--clocks CLOCKS] [--shard FILE...]
```

所有命令支持 `--workspace`、`--db`、`--ledger`、`--diagnostics`。输出为稳定排序 JSON。

退出码：

- `0`：成功。
- `2`：用法、文件或输入校验失败。
- `3`：已隔离的业务冲突（如 `E_CONFLICT_OBS`、`E_VERSION_CONFLICT`、`E_CLOCK_ROLLBACK`）。
- `4`：核验失败或账本不存在。
- `5`：内部错误。
- `137`：测试用强制终止注入点（`--fault-after-transitions`，仅用于恢复验收）。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_INSTANCE` | 实例条目或实例 ID 非法 |
| `E_DUPLICATE_INSTANCE` | 实例 ID 重复 |
| `E_DUPLICATE_SERVICE` | 服务名重复 |
| `E_BAD_ENDPOINT` | 非回环或非法端点 |
| `E_BAD_EXPECTED_STATUS` | 期望状态码集合非法 |
| `E_BAD_LEASE` | 租约代数非法 |
| `E_BAD_STATE_GENERATION` | 初始状态世代非正整数 |
| `E_BAD_OBS_SEQ` | 观测序号非正整数 |
| `E_BAD_LOGICAL_CLOCK` | 逻辑时钟缺失或非法 |
| `E_CLOCK_INJECTION_MISMATCH` | 观测时钟与注入序列不一致 |
| `E_DIGEST_MISMATCH` | 观测摘要不匹配 |
| `E_SHARD_DIGEST_MISMATCH` | 分片内容摘要不匹配 |
| `E_UNKNOWN_INSTANCE` | 观测引用未声明实例 |
| `E_CONFLICT_OBS` | 同序号互斥观测 |
| `E_VERSION_CONFLICT` | v1/v2 语义不等价 |
| `E_OBS_SEQ_ROLLBACK` | 迟到观测落到已提交水位之下且语义不一致 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回拨超过容差 |
| `E_SHARD_CONFLICT` | 同 `shard_id` 绑定不同内容摘要 |
| `E_MANIFEST_CONFLICT` | 账本已绑定另一份注册清单 |
| `E_VERIFY_*` | 指针、分片、缓冲区、诊断、换算或世代核验失败 |

单个分片解析失败时整个文件被拒绝，不写入该分片或其中任何观测；其他分片仍可提交。单个实例冲突不会阻止其他实例提交。

## 核验内容

`verify` 检查：

- 当前指针指向存在的批次，批次版本/摘要与指针一致。
- `pointer_version == sum(实例转移历史条数)`。
- 分片清单、实例账本、诊断、缓冲区、v1 换算清单摘要自洽。
- 磁盘 `ledger.json`、`diagnostics.json` 与 SQLite 当前批次一致。
- 每个实例历史世代严格递增，最后历史与当前状态、计数器、水位数一致。
- 水位以下没有遗留待裁决观测。
- 可选地重新解析磁盘注册清单和分片，比较清单摘要和分片内容摘要。

## 样本

- `samples/registry.json`：2 个服务、3 个实例。
- `samples/clocks.json`：固定注入逻辑时钟。
- `samples/shards/shard-01..04.json`：乱序、重叠、v1/v2 混合分片；`checkout-a` 故意缺少序号 4。
- `samples/streams/monolithic.json`：等价的单流对照样本。
- `samples/invalid/`：摘要错误、未知实例、截断 JSON、时钟回拨、同序号冲突、跨版本冲突、重复服务名等隔离样本。

固定样本的预期最终状态：

| 实例 | 状态 | 失败数 | 租约 | 世代 |
| --- | --- | ---: | ---: | ---: |
| `checkout-a` | `healthy` | 0 | 12 | 9 |
| `checkout-b` | `healthy` | 0 | 6 | 9 |
| `inventory-a` | `healthy` | 0 | 10 | 12 |

`checkout-a` 的序号 4 必须成为一次 `missing` 迁移，连续失败仍为 1，不递增；`checkout-b` 在第 3 次真实失败时进入 `lost`，随后成功恢复；`inventory-a` 的 v1/v2 等价序号 2 只计一次。

分片运行与单流对照比较的是实例状态、转移历史、缓冲区、时刻换算和 `pointer_version`；`shards` 清单按定义记录实际加载的分片，因此单流样本与四分片样本的该字段不会相同。

## 验证

```bash
pytest -q
python tools/run_acceptance.py
```

本次实际验证结果：

- pytest：`18 passed`。
- 两次完整运行的 `ledger.json` 逐字节一致。
- 在 7 次迁移后强制退出（退出码 `137`），`resume` 后与正常运行的 `ledger.json` 逐字节一致。
- 三次 `verify` 退出码均为 `0`。
- `pointer_version = 19`。
- 快照 SHA-256：
  `8d96158dac1bb85bad6700886a8f0e7492a63473cf115801271e3e8126f01c23`
- 实例账本摘要：
  `0a62a00d6af8c401a1a489b8b674171a25f7c546eed7faa9ef1c5c5723357724`
- 分片清单摘要：
  `b39be604f1d97928cd7df663825e4baeda422ce2f6f6db8c0226a233a239f2b4`
- 空缓冲区摘要：
  `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`

`python tools/run_acceptance.py` 每次使用新的临时工作目录重跑并输出相同确定性摘要。

## 项目布局

```text
probe_ledger/
  canonical.py    规范化 JSON、SHA-256 与语义摘要
  validation.py   注册清单、逻辑时钟、分片和 v1/v2 校验
  merge.py        双键归并、跨版本等价与冲突字段
  engine.py       状态差分推进、补洞、故障注入与恢复重放
  store.py        SQLite 批次指针、临时表、原子快照、verify
  cli.py          load/reconcile/resume/status/verify
samples/          固定有效/错误样本
tools/            样本生成与端到端验收脚本
tests/            pytest 验收测试
workspace/        默认运行产物目录
```

## 边界说明

- 引擎不监听端口、不发起网络请求，只读取本地 JSON 文件和 SQLite。
- 分片暂时缺失且出现序号缺口时，引擎不会猜测补洞；只有分片计划到齐或显式 `--seal` 才生成缺失迁移。
- 已提交状态世代不可回退。若互斥或跨版本不等价观测在提交后才到达，引擎保留原状态、记录诊断并要求复核，而不是回滚历史。
