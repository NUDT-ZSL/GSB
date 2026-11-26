# CDN Edge Consistency Rule Engine

一个仅依赖 Python 3.9+ 标准库的 CDN 边缘缓存一致性规则引擎。它确定性生成两代规则样本，完成 v1/v2 合并、依赖失效闭包、逻辑时钟窗口裁决、本地回源摘要仲裁、补丁增量重算、规则包原子切换、崩溃恢复和多进程幂等提交。

## 快速开始

```bash
python3 -m cdn_engine.seed --out samples/demo

# 可选：启动样本要求的本地回环回源服务
python3 -m cdn_engine.cli serve-origin \
  --content samples/demo/origin-content.json --port 18080

python3 -m cdn_engine.cli load \
  --state var/cdn.db --sample samples/demo --clock 1699999980000
python3 -m cdn_engine.cli merge \
  --state var/cdn.db --clock 1699999980000
python3 -m cdn_engine.cli probe \
  --state var/cdn.db --clock 1700000100000

python3 -m cdn_engine.cli patch \
  --state var/cdn.db \
  --file samples/demo/patches/patch-origin-catalog.json

CLOSURE_ID=$(python3 -m cdn_engine.cli status --state var/cdn.db \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["latest_closure"]["closure_id"])')
python3 -m cdn_engine.cli probe \
  --state var/cdn.db --recompute-closure "$CLOSURE_ID" --clock 1700000105000

python3 -m cdn_engine.cli status --state var/cdn.db
python3 -m cdn_engine.cli resume --state var/cdn.db
python3 -m cdn_engine.cli verify --workspace .verify-run
python3 -m unittest discover -s tests
```

也可以使用根目录便捷脚本 `./cdn-engine <command>`。

## 子命令

- `seed`：生成固定样本、v2 分片和 SQLite 元数据库。
- `load`：校验并导入样本；`load --package <dir>` 比较并原子切换新规则包。
- `merge`：构建跨版本统一规则视图，输出候选、时钟、代数、来源优先级和时间换算依据。
- `probe`：按节点处理观测、绑定验证窗口、发起本地回源仲裁并提交结论；支持 `--node`、`--file` 和故障注入参数。
- `patch`：幂等提交 `upsert`、`delete`、`tombstone` 补丁，输出依赖闭包和复用计划。
- `resume`：识别中止的规则包暂存操作与半提交窗口，只补做未完成工作。
- `status`：输出活动视图、时钟/窗口水位、窗口和结论计数、最新闭包。
- `verify`：运行 30 项端到端检查，包括重复序列比对、SIGKILL 恢复和三进程并行。
- `conclusions`、`serve-origin`：辅助查询和确定性回源服务。

## 确定性时钟与窗口

引擎不调用系统墙钟决定窗口。所有边界来自显式 `--clock`、样本固定时钟或 SQLite 中已持久化的逻辑时钟水位：

```text
window_start = floor((event_clock_ms - offset_ms) / size_ms) * size_ms + offset_ms
window_end   = window_start + size_ms
```

v1 的 `relative_effective_seconds` 按注入时钟换算：

```text
effective_at_ms = injected_clock_ms + relative_effective_seconds * 1000
```

换算记录包含 `scale_ms_per_second=1000`、`rounding_direction=floor` 和 `remainder_ms=0`。相同输入与相同时钟序列产生相同的 `decision_digest` 和结论摘要序列。

## 合并裁决

同名规则候选按以下顺序裁决：

1. `declared_clock_ms` 降序；
2. 时钟相同时按来源格式优先级降序：`patch=30`、`v2_sharded_sqlite=20`、`v1_single_json=10`；
3. 代数参与单调性校验并保留在每个候选的 `decision_basis` 中。

跨版本同规则要求 v2 代数不低于 v1。每条统一规则保留来源格式、来源版本、原始时间表示、绝对生效时刻、换算说明和完整裁决依据。

## 失效闭包

依赖边表示“当前规则依赖上游规则”。上游变化时，引擎沿反向边计算所有下游受影响规则，并输出：

- `trigger_patch`：触发补丁和变更操作；
- `rule_name` / `trigger_rule`：闭包规则与触发规则；
- `dependency_path`：从变更上游到受影响下游的确定性路径；
- `conclusion_reuse`：`recompute` 或 `discard_deleted_rule`；
- `conclusion_plan`：旧视图中的失效结论、窗口、观测序号，以及可安全复用的结论 ID。

未受影响结论保持不变；`probe --recompute-closure` 只在新视图中为失效窗口创建重算结论。

## 一致性裁决

结论码严格区分：

- `CACHE_HIT_VERIFIED`：摘要或回源摘要匹配；
- `DIGEST_MISMATCH`：观测摘要与规则摘要/回源响应摘要不一致；
- `ORIGIN_CONNECTION_REFUSED`：本地回环回源端口拒绝连接；
- `NO_OBSERVATION`：窗口内没有任何观测；
- `RULE_TOMBSTONED`、`RULE_NOT_FOUND`、`EDGE_PROBE_ERROR` 等。

对 `verify_type` 含 `origin` 的规则，引擎请求节点清单中的 loopback 地址，禁用环境代理，并在结论中保存请求头、响应状态、响应头、响应摘要、观测摘要、匹配结果和规则版本。

## 规则包切换

`load --package` 比较清单版本、`min_generation`、每个分片 SHA-256 和规则集合摘要，决策为 `full_replace`、`incremental_append` 或 `noop`。低版本包或更低代数下限返回 `E_PACKAGE_ROLLBACK`；同版本摘要变化返回 `E_PACKAGE_VERSION_CONFLICT`。

完整替换先写临时表 `temp.staged_unified_rules`，再在一个 SQLite 事务中写视图、规则、包记录并切换 `active_view_id`。已打开窗口保存自己的 `view_id`，因此在途探测始终使用窗口开始时的规则版本。

## 持久化与并发

SQLite 保存活动视图指针、版本化统一规则、输入观测、已处理观测、补丁、闭包、重算记录、窗口、结论、时钟水位和规则包记录。

并发控制包括：

- `<state.db>.locks/global.lock`：合并、补丁、包切换；
- `<state.db>.locks/node-<node>.lock`：同一边缘节点串行推进；
- SQLite `BEGIN IMMEDIATE`、`busy_timeout`、`UNIQUE(view_id, seq)` 和视图 `input_digest UNIQUE`。

不同节点可并行运行；同一观测在同一视图不会重复计数，重复补丁返回同一闭包，重复探测跳过已提交观测。

## 主要错误码

| 错误码 | 含义 |
| --- | --- |
| `E_DUPLICATE_RULE_NAME` | 同一版本内规则名重复 |
| `E_GENERATION_NOT_MONOTONIC` | 规则代数未单调或跨版本回退 |
| `E_INVALID_EFFECTIVE_SECONDS` | v1 相对秒数不是非负整数 |
| `E_INVALID_EFFECTIVE_MS` | v2 绝对毫秒不是整数 |
| `E_DANGLING_DEPENDENCY` | 依赖引用了未声明规则 |
| `E_SHARD_HASH_MISMATCH` | v2 分片哈希或规则集摘要与 SQLite 不一致 |
| `E_INVALID_TOMBSTONE` | 墓碑引用不存在的规则 |
| `E_OBS_SEQUENCE_NOT_MONOTONIC` | 观测序号未严格递增 |
| `E_OBS_DIGEST_RESULT_MISMATCH` | 探测结果与摘要存在性/格式不一致 |
| `E_PACKAGE_ROLLBACK` | 规则包版本或代数下限回退 |
| `E_PATCH_IDEMPOTENCY_CONFLICT` | 同一补丁 ID 提交不同内容 |
| `E_IDEMPOTENCY_CONFLICT` | 同一观测序号提交不同载荷 |
| `E_CLOCK_ROLLBACK` | 注入逻辑时钟向后回退 |

## 代码结构

- `cdn_engine/seed.py`：确定性样本和 SQLite 元数据生成。
- `cdn_engine/parser.py`：输入解析与业务校验。
- `cdn_engine/merge.py`：跨版本合并裁决。
- `cdn_engine/closure.py`：补丁规范化和依赖闭包。
- `cdn_engine/engine.py`：事务、幂等、包切换、探测和恢复。
- `cdn_engine/adjudication.py`：窗口一致性裁决。
- `cdn_engine/origin.py`：本地确定性回源服务和仲裁客户端。
- `cdn_engine/storage.py`：SQLite schema 与事务封装。
- `cdn_engine/verification.py`：可重复端到端验证。
- `tests/test_engine.py`：单元和集成测试。

实际验证记录见 `docs/VERIFICATION.md`。
