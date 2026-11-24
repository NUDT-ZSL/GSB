# 实现说明

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `supply_chain_audit/codes.py` | 稳定业务码、检查点结构版本、算法白名单版本和合并口径版本 |
| `supply_chain_audit/versions.py` | 规范键、精确 SemVer 版本和依赖版本表达式语法 |
| `supply_chain_audit/digests.py` | 算法别名归一、位宽分派、十六进制/大小写校验、前缀证据和真实摘要计算 |
| `supply_chain_audit/timeutils.py` | 固定观测时钟换算和秒到毫秒端点误差界 |
| `supply_chain_audit/parsing.py` | v1、v2 分片 JSON + SQLite、依赖和吊销批次解析 |
| `supply_chain_audit/graph.py` | Tarjan SCC 环检测和按来源分层的反向闭包 |
| `supply_chain_audit/analysis.py` | 单构件跨版本时间、代次、算法、摘要和状态分析 |
| `supply_chain_audit/engine.py` | 全量视图、增量受影响集合和逐字段一致性比较 |
| `supply_chain_audit/importer.py` | 线程并发、`time.monotonic()` 超时、队列和摘要计算预算 |
| `supply_chain_audit/checkpoint.py` | SQLite + JSON 双检查点、摘要校验、恢复和版本漂移重建 |
| `supply_chain_audit/generator.py` | 固定种子、无网络样本生成器 |
| `supply_chain_audit/cli.py` | `import-snapshots`、`query-view`、`export-closure`、`verify-checkpoint` |

## 2. 规范口径

规范构件键为小写名称加精确版本：

```text
security/crypto@3.0.1
```

名称允许 `/` 分段以及小写字母、数字、`.`、`_`、`-`。构件版本使用 `major.minor.patch` 加可选预发布/构建元数据。依赖约束支持逗号 AND、`||` OR、比较符、`^`、`~`、通配版本和 `*`。

签名状态白名单为：

```json
["signed", "unsigned", "invalid_signature"]
```

算法白名单当前包含 `sha256`、`hmac-sha256`、`sha512`、`hmac-sha512`。`SHA-256`、`sha_256`、`HMAC_SHA256` 等写法会归一到稳定算法名；摘要统一转为小写后比较，因此仅大小写不同不会判为不一致。

## 3. 时间换算

观测时钟文件必须声明：

```json
{
  "clock_id": "fixed-observation-clock-v1",
  "observed_at_ms": 1767225600000,
  "relative_second_origin_ms": 1767139200000
}
```

v1 的非负整数相对秒 `s` 换算为闭区间：

```text
lower = relative_second_origin_ms + s * 1000
upper = lower + 999
```

以 `lower` 作为规范整毫秒点时最大端点误差为 `999ms`；以 `lower + 499.5` 为中点时对称误差界为 `499.5ms`。若 v2 的精确毫秒落在区间内，则用 v2 时间细化点值且顶层 `error_bound_ms=0`，同时保留 v1 `time_conversion` 证据。若两代时间区间不相交，输出 `E_TIME_CONFLICT`。

## 4. 拒绝、冲突与摘要分派

非法记录不会中断同快照后续记录。拒绝记录至少包含：

```json
{
  "record_type": "rejection",
  "snapshot_id": "seed-v1-single-file",
  "artifact_key": "broken/hashwidth@0.1.0",
  "code": "E_HASH_ALGO",
  "message": "digest bit width does not match declared algorithm"
}
```

关键码：

- `E_CANONICAL_KEY` / `E_VERSION_SYNTAX` / `E_CONSTRAINT_SYNTAX`
- `E_DUP_KEY`
- `E_HASH_HEX`：非合法十六进制。
- `E_HASH_ALGO`：算法不在白名单或摘要位宽与算法不符。
- `E_HASH_MISMATCH`：位宽合法，但归一化摘要值不同。
- `E_SIGNATURE_STATUS`、`E_NON_NEGATIVE_INT`
- `E_TIME_CONFLICT`、`E_GENERATION_CONFLICT`、`E_ALGO_CONFLICT`
- `E_METADATA_MISMATCH`：v2 JSON 分片与 SQLite 元数据不一致。

摘要冲突证据包含快照、摘要代次、归一算法和 12 字符前缀，避免在冲突列表中只给结论而没有定位证据。

## 5. 图与闭包

依赖边方向定义为：

```text
dependent_key -> dependency_key
```

例如 `app -> api -> crypto`。当 `crypto` 摘要变化或被吊销时，沿反向边得到直接和传递依赖它的 `api`、`app` 等构件。每个失效条目包含：

```json
{
  "source_key": "security/crypto@3.0.1",
  "level": 2,
  "chain": ["app/portal@6.1.0", "net/tls@4.2.0", "security/crypto@3.0.1"],
  "source_to_dependent_path": ["security/crypto@3.0.1", "net/tls@4.2.0", "app/portal@6.1.0"]
}
```

环检测使用 Tarjan SCC。自引用输出 `[key, key]`；普通环输出首尾相同的稳定键序列和成员列表，业务码为 `E_DEP_CYCLE`。

## 6. 增量等值

`AuditEngine` 维护按构件键分组的 claims、rejections、revocations 和图。批次到达时：

1. 记录变更键。
2. 合并旧来源闭包与新来源闭包，得到受影响键并集。
3. 只重建这些构件及其关联边；未受影响构件复用上一视图。
4. 调用 `compare_views(incremental, full_recompute)` 比较构件字段、`conflicts`、`cycles`、`closure_sets` 和 `invalidation_index`。
5. 若不同，返回 `E_RECOMPUTE_DIVERGE`，并给出首个分歧 `artifact_key` 与 `field_name`。

种子样本上的 codec 摘要更新只重判 7 个闭包构件，log 吊销只重判 6 个闭包构件；`foundation/core@1.4.0` 等闭包外构件不会进入 `rejudged_keys`。

## 7. 并行与资源预算

`BatchImporter` 使用 `ThreadPoolExecutor` 并发解析和做可选内容摘要复算。每个批次有独立 `BatchExecution` 计量器：

- `timeout_seconds`：基于 `time.monotonic()` 的真实墙钟截止时间；在记录和摘要边界协作式取消，返回 `E_TIMEOUT`。
- `queue_limit`：一次导入调用中允许受理的未完成批次上限；超限同步返回 `E_QUEUE_FULL`，不会提交新 Future，因此不影响已受理批次。
- `max_hash_operations`：批次内真实 `hashlib`/`hmac` 计算次数上限；超限返回 `E_HASH_BUDGET`。

Python 不能安全强杀正在执行的单次 C 扩展哈希或 JSON 读取，因此超时保证在下一条记录/下一次摘要边界终止后续处理；本地确定性文件和可配置预算使该边界可测试、可复现。

## 8. 检查点与崩溃边界

检查点目录包含：

```text
.audit-workspace/
  checkpoints.sqlite3
  checkpoints/
    checkpoint-000004-<digest12>.json
```

SQLite 行和 JSON envelope 都保存水位、策略版本和完整视图摘要。写入顺序是：

1. 同目录临时 JSON。
2. `flush + fsync + os.replace` 原子改名。
3. 一个 SQLite 事务写入 `status='complete'`。

强杀后，只有 SQLite 中完整且 JSON 摘要一致的最后一行可恢复。改名后、SQLite 提交前产生的孤儿 JSON，以及 `.tmp` 残留，都不会被当作完整检查点。`os._exit(137)` 子进程测试覆盖了这个边界；强杀场景不承诺生成退出报告。

以下三个版本任一变化都会返回 `E_CHECKPOINT_VERSION/rebuild_required=true`，并要求从原始输入整体重建：

- `CHECKPOINT_SCHEMA_VERSION`
- `ALGORITHM_WHITELIST_VERSION`
- `MERGE_POLICY_VERSION`

`recover_or_rebuild(full_rebuild)` 不会把旧 `view` 传入重建回调，从流程上避免新旧结论混合。

## 9. CLI 输出

所有命令输出 JSON。`--out`/`--report` 路径以及检查点 JSON 都通过同目录临时文件加 `os.replace` 写入。导出的闭包报告包含：

- 构件级 `status` 与 `business_codes`
- 时间误差界和 v1 换算区间
- 摘要比较与前缀证据
- `closure_sets`
- 每条失效来源、层级和 `chain`
- 冲突、环和拒绝记录

## 10. 无网络保证

生成器只使用 `hashlib`、`sqlite3`、`json`、`pathlib`、`random` 等价的确定性字符串输入和标准库文件 API；没有 `urllib`、`requests`、socket、账号或远端服务。固定种子常量为 `uid2d1b8ac8-fixed-seed-20261125`。
