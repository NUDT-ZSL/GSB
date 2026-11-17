# 敏感标记派生守卫引擎

这是一个零外部依赖的本地确定性校验器，用于解析 JSON 字段描述集、重建敏感标记传播链、检测派生环，并输出可复现报告与诊断命令。样本完全由项目内生成器产生，不访问外部数据集，不使用 Docker。

## 快速开始

要求 Python 3.11+。Windows 下：

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

Linux/macOS 下：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

生成确定性样本、执行校验并写出原子报告：

```bash
.venv/bin/python -m sensitive_guard.generator --out-dir samples --seed 20261118 --clean
.venv/bin/python -m sensitive_guard validate \
  --input samples/batch.json \
  --report reports/report.json \
  --concurrency 4 --timeout-ms 100 --max-fields 30
```

一次完成清理、生成、并发一致性检查和退出码验收：

```bash
.venv/bin/python scripts/acceptance.py
```

验收产物位于 `artifacts/acceptance/`。`--clean` 会先删除样本输出目录，保证实验从同一初始状态开始。

## 输入格式

单个描述集：

```json
{
  "schema_version": 1,
  "dataset_id": "demo",
  "sync_marker": "sha256:...",
  "injection": {
    "force_cycle_length": 3,
    "force_downgrade_at": "order[0].view",
    "simulate_delay_ms": 0
  },
  "records": [
    {
      "path": "order[0].secret",
      "sources": [],
      "masking": "plaintext",
      "policy_id": "policy.default",
      "version": "v2",
      "transitive": true
    },
    {
      "path": "order[0].view",
      "sources": ["order[0].secret"],
      "masking": "masked",
      "policy_id": "policy.default",
      "version": "v2",
      "transitive": true
    }
  ]
}
```

批文件为单个数据集、数据集对象数组，或以下清单：

```json
{
  "schema_version": 1,
  "datasets": [
    "datasets/clean.json",
    "datasets/cycle_injection.json"
  ]
}
```

清单中的相对路径相对于清单文件所在目录解析。`--dataset-id` 可重复指定以只运行部分描述集。

### 路径规范化

点路径中的数字段与数组下标等价，统一输出为：

```text
orders.0.customer.name   -> orders[0].customer.name
orders[12].items.3.id    -> orders[12].items[3].id
matrix.0.2.value         -> matrix[0][2].value
```

空段、负下标、非法方括号、非字符串路径均拒绝。

### 状态、版本与记录序号

- 状态规范值为 `plaintext`、`masked`、`invisible`，也接受 `明文`、`脱敏`、`不可见`。
- `v1` 禁止传递标记；若提供 `transitive` 必须为 `false`。
- `v2` 必须携带布尔 `transitive`。
- `sync_marker` 由记录多重集和 `injection` 的规范 JSON 计算；记录顺序和 `sources` 列表顺序不影响标记。
- `rejected_records.record_index` 是输入中的 1 基序号。

## 传播语义

每个字段的声明状态是一个独立传播源：

- `v1`：只影响直接依赖字段，不继续外延。
- `v2 + transitive=true`：沿派生方向持续外延。
- `v2 + transitive=false`：与直接传递相同。
- 中间字段不会把继承来源“改写成自己”继续传播；它自己的声明状态仍按自己的版本独立传播。

目标字段的 `propagated_status` 是所有到达来源状态的最高等级，等级固定为：

```text
plaintext > masked > invisible
```

`effective_status` 再与目标自身声明状态按同一优先级归一。`truth_sources` 列出达到该有效等级的全部稳定来源键。

违规定义：

- 来源等级高于目标声明等级：`E_DOWNGRADE`，例如明文来源到达脱敏目标。
- 不可见来源到达任意其他字段：`E_LEAK`。
- 目标直接来源同时包含 `v1` 与 `v2`：`E_VERSION_CONFLICT`，并输出全部冲突来源键。传播仍按每条来源自己的版本执行。
- 来源图存在自环或任意长度环：`E_CYCLE`，输出以最小规范键开头、首尾闭合的稳定环序列。

环中的传播使用带去重的可达性搜索，不会无限递归；环路径、违规来源和字段结果均按规范键排序，因此与记录到达顺序无关。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_RECORD` | 非法记录、路径重复、来源目标不存在等，附 1 基记录序号 |
| `E_SYNC` | 提供的 `sync_marker` 与当前记录和注入参数不一致 |
| `E_FIELDS` | 原始字段记录数超过 `--max-fields` |
| `E_TIMEOUT` | 单描述集真实耗时超过 `--timeout-ms` |
| `E_CYCLE` | 检测到自引用或任意长度派生环 |
| `E_VERSION_CONFLICT` | 目标直接来源的声明版本混用 |
| `E_DOWNGRADE` | 来源保护等级高于目标声明等级 |
| `E_LEAK` | 不可见来源传播到其他字段 |
| `E_INTERNAL` | 防御性隔离的未预期内部错误 |

## 报告与退出码

报告包含：

- 批次级汇总、业务码计数、观测一致/不一致计数。
- 每个描述集的状态、业务码、拒绝记录、重复边、环、违规和逐字段传播状态。
- 每个描述集的重跑命令，以及按业务码生成的放宽资源或聚焦 pytest 命令。
- `truth_sources`、`incoming`、`duplicate_edges` 等可审计诊断信息。

报告先写入同目录临时文件，`flush` 和 `fsync` 后通过 `os.replace` 整体替换目标文件。单个描述集异常被隔离为 `E_INTERNAL`，不会中断同批其他描述集。

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部描述集成功 |
| `1` | 一个或多个描述集失败（部分失败或全部失败） |
| `2` | 参数、输入文件或报告写出错误 |

## 资源与并发

- `--concurrency` 控制批处理线程数，结果始终按输入描述集顺序重排。
- `--timeout-ms` 使用 `time.monotonic()` 实测；记录解析、建图、环枚举、传播、违规聚合等循环均检查截止时间。
- `--max-fields` 在解析前检查原始记录数，避免超限分配。
- 注入参数 `simulate_delay_ms` 只用于本地确定性超时复现，不访问网络或外部资源。

并发度变化只改变执行调度，不改变任一描述集的字段状态、业务码、违规清单、环路径或报告顺序。

## 确定性生成器

```bash
.venv/bin/python -m sensitive_guard.generator --out-dir samples --seed 20261118 --clean
```

内置样本覆盖：无违规基线、顺序无关冲突、4 节点环、自环、v1/v2 混用、降级、泄露、16 层深链、超时和超字段数。注入参数只描述待构造结构和复现条件；验证逻辑从描述集、同步标记与注入参数独立计算观测，不读取生成器中间结果。

也可用显式注入文件现场构造单个描述集：

```json
{
  "dataset_id": "explicit-injection",
  "injection": {
    "force_cycle_length": 3,
    "force_downgrade_at": "made.down",
    "forge_version_mix_at": "made.mix",
    "deep_chain": {
      "prefix": "made.deep",
      "depth": 4,
      "initial_status": "plaintext",
      "rest_status": "masked"
    },
    "simulate_delay_ms": 0
  }
}
```

```bash
.venv/bin/python -m sensitive_guard.generator \
  --out-dir injected-sample --clean \
  --injection-file injection.json \
  --dataset-id explicit-injection
```

支持的显式控制包括 `force_cycle_length`、`cycle_prefix`、`force_downgrade_at`、`force_downgrade_from`、`forge_version_mix_at`、`deep_chain`、`simulate_delay_ms`。`deep_chain` 可使用 `{prefix, depth, initial_status, rest_status}`，也可直接提供 `{paths, expect_effective}`。

## 测试

```bash
.venv/bin/python -m pytest -q
```

测试覆盖路径等价性、1/2/16 层传播、24 种记录排列、1/2/5/12 长度环、版本冲突、非法记录继续处理、真实超时、字段上限、并发报告一致性、CLI 退出码、原子替换失败保护和输入文件不变性。

实际验证结果见 `docs/VALIDATION.md`，算法与报告契约见 `docs/DESIGN.md`。
