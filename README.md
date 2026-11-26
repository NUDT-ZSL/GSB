# LLM 多轮对话状态诊断引擎

面向本地 Linux/WSL Ubuntu 的确定性诊断引擎。输入完全由固定种子脚本生成，不访问账号、网络或外部服务，不使用 Docker。实现覆盖三格式槽位快照、双向往返迁移、确定性 DAG、内容寻址缓存、增量后缀重算、版本化原子快照、并发提交与强制终止恢复。

## 快速开始

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_samples.py
python3 -m unittest discover -v
python3 scripts/acceptance.py
```

`scripts/acceptance.py` 会重建 `samples/`，执行格式互迁、DAG 确定性、增量失效、快照并发与恢复检查，并生成完整报告：

```text
artifacts/acceptance_report.json
```

常用 CLI：

```bash
python3 -m dialog_state_engine inspect \
  --config samples/config.json --state samples/state_v3.json

python3 -m dialog_state_engine migrate \
  --config samples/config.json --source samples/state_v3.json \
  --target-version v1 --out /tmp/state_v1.json --overwrite

python3 -m dialog_state_engine run \
  --config samples/config.json --turns samples/turns.jsonl \
  --cache-dir /tmp/dialog-cache --out /tmp/run-report.json
```

## 实际验证结果

本次在本机 Python 3.10.12、SQLite 3.37.2、NumPy 1.26.4 上完成：

- `python3 -m unittest discover -v`：30 项测试全部通过。
- v1/v2/v3 读取后的统一语义校验和均为 `sha256:e73d08ba...a84c5e`。
- 六组跨格式迁移全部 `semantic_equal=true`；v1 升级补全 4 项，v2 升级补全 2 项；v3 降至 v1/v2 时显式丢弃 7 条嵌套历史。
- 冷缓存首跑重算 17 个节点；热缓存重跑复用 17 个节点，语义结果字节一致。
- 追加第 6 轮仅重算 `parse:turn=6`、`score:turn=6`、`merge:through_turn=6`、`pending`、`summary`。
- 撤回中间第 4 轮时受影响后缀为第 5、6 轮，未受影响的第 1-3 轮合并前缀与第 5、6 轮打分产物复用。
- 快照并发冲突返回 `E_STAGE_BUSY`，回退返回 `E_SNAPSHOT_ROLLBACK`；强制退出前后，最后一次已提交版本保持不变且可恢复。
- 验收报告连续两次生成的文件 SHA-256 均为 `435a7feda41435e081edf43af26ba2ba070c66057406660c4d9156ceeba2c043`，报告内校验和为 `sha256:f21c773e...e662a`。

完整机器可读结果及全部重算/复用集合见 `artifacts/acceptance_report.json`。

## 输入契约

### 对话轮次 JSONL

每个非空行必须且仅包含以下 5 个字段：

```json
{"session_id":"sess-demo-001","turn_no":2,"user_text":"目的地改成上海","assistant_text":"已更新","slot_candidates":[{"slot_key":"destination","value":"上海","confidence":0.58}]}
```

- `slot_candidates` 支持对象映射或数组；数值必须有限且位于 `[0,1]`。
- 轮次按 `turn_no` 升序进入 DAG。
- `(session_id, turn_no)` 是业务主键；重复时返回 `E_DUPLICATE_SESSION`，错误字段指向 `session_id` 并附带 `turn_no`。
- 助手文本中的澄清请求由配置正则提取，例如 `[clarify:destination,guests]`。

### 解析配置

`samples/config.json` 固定包含：

- 时间原点与轮次时间步长；
- 置信度权重、轮次权重、打分精度；
- 澄清门限与请求正则；
- 槽位键命名正则；
- 合并策略。

配置参与所有缓存键和解析规则摘要。澄清门限要求获胜候选置信度严格小于门限；等于门限也不满足“低于”。

## 三格式与统一状态

| 格式 | 表达方式 | 时间 | 代数 | 变更历史 |
|---|---|---|---|---|
| v1 | 单 JSON、扁平整槽记录、独立覆盖候选列表 | 相对秒 | 可选；缺省时补全并记录来源 | 无 |
| v2 | 分片 JSON + `manifest.json` + SQLite | SQLite 绝对毫秒 | manifest、meta、每槽 timing 三重校验 | 无 |
| v3 | 嵌套槽位、覆盖候选、逐槽变更历史 | 绝对毫秒 | 根与槽位记录 | 有 |

统一内部状态仅包含业务语义：

```text
session_id
state_generation
updated_at_ms
slots[slot_key] = {
  winner,
  overwritten[],       # 审计信息，不作为可见赢家
  conflict,
  updated_at_ms
}
pending_clarifications[]
state_checksum
```

`change_history` 被定义为可丢弃的审计轨迹，不属于语义状态。这样 v3 降级到 v1/v2 时，业务状态严格相等，同时丢弃明细会被逐项报告。若调用方要求历史不可丢，设置 `strict_audit_drops=True`，则返回 `E_SEMANTIC_DRIFT`。

迁移补全示例：

- v1 缺少 `state_generation`：使用配置默认值；
- v1 相对秒转绝对毫秒：记录配置时间原点来源；
- v1/v2 缺少历史：补 `[]` 并记录格式默认来源；
- v1/v2 缺少校验和：按规范语义状态计算并记录 `computed` 来源。

## DAG 与缓存

```text
turn JSONL
  -> parse:turn=N
  -> score:turn=N
  -> merge:through_turn=N
  -> pending
  -> summary
```

打分：

```text
turn_factor = 1 - 1 / (turn_no + 1)
score = confidence_weight * confidence + turn_weight * turn_factor
```

分数按配置精度量化。排序为分数降序、来源轮次升序、槽位键字典序，随后用候选值规范 JSON 字典序和候选内容键消除剩余并列。

合并：

1. 来源轮次更大者覆盖；
2. 同轮次取分数更高者；
3. 同轮同分按候选值字典序裁决并标记 `conflict=true`；
4. 非获胜候选只进入审计列表，不出现在最终可见赢家集合中。

内容寻址缓存：

- 对象文件名为 SHA-256 摘要，内容包含 schema、节点、载荷哈希和载荷。
- 缓存键纳入节点名、配置摘要、上游缓存键、上游载荷哈希和节点附加参数。
- 候选另有独立内容键，可直接按候选键与来源轮次重放槽位值。
- 缓存对象损坏、键/节点/载荷哈希不一致时返回 `E_FORMAT_CORRUPT`，不会误命中。

增量修订先比较 `(session_id, turn_no)` 内容集合，得到撤回、追加、修改和保留集合；首次变化轮次及其后的轮次构成受影响后缀。前缀合并产物直接复用，未改轮次的解析/打分继续命中，只有后缀合并、待澄清与摘要按新键重算。

## 快照与恢复

- SQLite 单行保存完整快照 JSON，版本号单调递增。
- 内容校验和覆盖 `{version, payload}`；读取时总是重新校验并解析整个快照。
- 版本或校验和变化时整体替换进程内缓存，不做字段级修补。
- 提交使用非阻塞文件锁与 `BEGIN IMMEDIATE`；竞争失败立即返回 `E_STAGE_BUSY`。
- 版本降低或同版本不同内容返回 `E_SNAPSHOT_ROLLBACK`。
- `WAL + synchronous=FULL + 单事务 DELETE/INSERT` 保证强制终止时旧提交完整保留，已提交事务可恢复。

## 错误码

| 业务码 | 场景 |
|---|---|
| `E_VERSION_UNSUPPORTED` | 未知快照版本或无法按字段集分派 |
| `E_FORMAT_CORRUPT` | JSONL 字段集/JSON/SQLite/必填字段/数值/校验和损坏 |
| `E_SEMANTIC_DRIFT` | 严格模式下无法保留历史，或迁移后语义状态不一致 |
| `E_PENDING_SLOT_MISSING` | 澄清请求指向不存在的槽位 |
| `E_CONFIDENCE_ABOVE_THRESHOLD` | 澄清槽位赢家置信度不低于门限 |
| `E_DUPLICATE_SESSION` | 重复 `(session_id, turn_no)` 业务键 |
| `E_SLOT_KEY_INVALID` | 槽位键不符合配置命名规则 |
| `E_CONFIDENCE_OUT_OF_RANGE` | 置信度不在 `[0,1]` |
| `E_SNAPSHOT_ROLLBACK` | 快照版本回退或同版本异内容 |
| `E_STAGE_BUSY` | 另一提交正在生效 |
| `E_CONFIG_INVALID` | 配置结构、权重、门限或正则非法 |

所有错误均包含 `code`、`message`、可选 `field` 和结构化 `details`。

## 目录

```text
dialog_state_engine/
  formats.py       # v1/v2/v3 读取、写出与版本分派
  migration.py     # 双向迁移、补全/丢弃、语义漂移检查
  turns.py         # JSONL 解析与输入校验
  graph.py         # 打分、合并、待澄清、摘要与增量缓存
  cache.py         # 内容寻址对象存储
  snapshot.py      # 版本化原子快照、并发门闩与恢复
  samples.py       # 固定种子本地样本生成
tests/             # 30 项 unittest
scripts/           # 样本生成与端到端验收
samples/           # JSONL、配置、v1/v2/v3 快照
artifacts/         # 验收报告
```
