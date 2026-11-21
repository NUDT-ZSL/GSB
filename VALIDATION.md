# 实际验证记录

## 环境

- 主机环境：Windows 工作区中的 WSL Ubuntu 22.04。
- Python：3.10.12。
- NumPy：1.26.4。
- 运行方式：本地 Python 进程，无 Docker、无容器、无外部数据下载、无线上服务依赖。
- 项目代码仅使用 NumPy 与 Python 标准库，路径、原子替换、文件锁和批处理脚本按 Windows/Linux 兼容方式实现。

## 1. 自动化测试

命令：

```text
python -m unittest discover -s tests -v
```

实际结果：

```text
test_dashboard_retract_append_and_trace ... ok
test_cache_corruption_and_retrieval_by_key ... ok
test_concurrent_same_entry_writer_gets_stage_busy_immediately ... ok
test_snapshot_replacement_rollback_and_corruption ... ok
test_repeated_build_reuses_every_node ... ok
test_score_tie_uses_source_then_slot_key ... ok
test_seeded_session_and_state_are_byte_identical ... ok
test_threshold_change_changes_cache_keys ... ok
test_append_matches_full_rebuild_without_rescoring_committed_turns ... ok
test_illegal_batch_order_is_rejected_without_mutation ... ok
test_legal_batch_order_matches_direct_rebuild ... ok
test_retract_assistant_turn_removes_clarification_request ... ok
test_retract_matches_direct_rebuild_and_reuses_unaffected_suffix ... ok
test_broken_chain_returns_slot_and_break_node ... ok
test_changed_version_returns_lineage_stale ... ok
test_successful_replay_and_covered_set ... ok
test_source_override_and_equal_score_value_tiebreak ... ok
test_pending_slot_missing ... ok
test_threshold_conflict ... ok
test_valid_pending_artifact ... ok

----------------------------------------------------------------------
Ran 20 tests in 2.604s

OK
```

覆盖内容：

- 固定种子会话与最终状态逐字节一致；
- 重复构建全部节点缓存命中；
- 澄清门限进入缓存键；
- 分数并列时来源轮次/槽位键排序；
- 来源覆盖、同分同源值字典序裁决与冲突标记；
- 被覆盖候选仅存在于合并产物，不出现在最终槽位；
- `E_PENDING_SLOT_MISSING` 与 `E_THRESHOLD_CONFLICT`；
- 撤回与物理删除重建逐字节一致；
- 撤回助手轮后澄清请求移除，槽位合并节点保持复用；
- 追加与全量重建一致且 `committed_turns_rescored=[]`；
- 批处理逆序返回 `E_OPERATION_ORDER` 且无状态变化；
- 缓存按键读取、篡改检测与哈希核对；
- 快照整体替换、外部快照应用、版本回退与快照损坏；
- 并发同键写入立即获得 `E_STAGE_BUSY`；
- 追溯成功重放、断链与版本陈旧；
- HTTP 仪表盘、撤回、追加、追溯与缓存按键读取。

## 2. 固定种子样本构建

命令：

```text
python -m slotline build --seed 42 --out data/session_seed42.json --runtime .runtime
```

实际结果摘要：

```text
session_sha256 = fed0085d9045ab8a67f2eb95687a8f5f571d2f44eac88e853c9d3f85866dbc33
state_hash     = 1335de385bbcdda824883e2a3ca551000bb4eeeeeec3a77b470dc5cd28687468
snapshot       = v1
checksum       = 4cdf06d646619bd2f711d265d01e533530883e6cf6e14fff24c0a8edab562a0b
cache_entries  = 17
```

初始最终槽位：

| 槽位 | 获胜值 | 置信度 | 分数 | 来源 | 版本 | 冲突 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `budget` | 350 | 0.59 | 0.6195 | t7 | v2 | 否 |
| `city` | 杭州 | 0.95 | 0.9500 | t7 | v1 | 是 |
| `cuisine` | 淮扬菜 | 0.75 | 0.7875 | t7 | v2 | 否 |
| `party_size` | 2 | 0.91 | 0.9555 | t5 | v2 | 否 |

## 3. 撤回与追加演示

命令：

```text
python -m slotline demo
```

撤回 `u2` 的实际结果：

```text
affected_slots        = [budget, city, cuisine]
recomputed_nodes      = [
  merge:u3:budget, merge:u3:city, merge:u4:budget,
  merge:u4:city, merge:u4:cuisine, pending, summary
]
reused_nodes          = [
  score:u1, score:u3, score:u4,
  merge:u1:budget, merge:u1:city, merge:u3:party_size
]
removed_nodes         = [
  score:u2, merge:u2:budget, merge:u2:city, merge:u2:cuisine
]
equivalent_to_direct_rebuild = true
```

该结果证明 `u3/u4` 已提交轮次无需重新打分，无关槽位 `party_size` 合并节点直接复用，仅同槽位覆盖后缀重算。

追加确定性尾轮 `u9` 的实际结果：

```text
affected_slots        = [city, dietary, party_size]
recomputed_nodes      = [
  score:u9, merge:u9:city, merge:u9:dietary,
  merge:u9:party_size, summary
]
reused_nodes          = [
  score:u1, score:u3, score:u4,
  merge:u1:budget, merge:u1:city, merge:u3:budget,
  merge:u3:city, merge:u3:party_size, merge:u4:budget,
  merge:u4:city, merge:u4:cuisine, pending
]
committed_turns_rescored = []
equivalent_to_full_rebuild = true
```

追加后最终快照为 `v3`，校验和为：

```text
aa51cc4cfb1366641b2e6eac74970408d4817fd576025639a030a1b0078f19a4
```

## 4. 因果追溯重放

同一演示中对 `budget` 槽位重放：

```text
value                 = 350
source_turn           = 7
extractor_version     = v2
score                 = 0.6195
covered_values        = [600, 750]
final_merge_cache_key = bfba41c48ca1729278f586ee5b3bc0398851d7624cf3770c0e6a5aa8558d6826
verified              = true
```

`covered` 与最终合并缓存条目中的累计被覆盖集合完全一致，且最终 `state.slots.budget` 不暴露该集合。

## 5. 业务错误码复现

命令：

```text
python -m slotline diagnose
```

实际复现：

| 错误码 | 触发方式 | 断点信息 |
| --- | --- | --- |
| `E_SNAPSHOT_ROLLBACK` | 以当前版本号再次提交快照 | `node=snapshot` |
| `E_STAGE_BUSY` | 持锁线程运行期间第二次写同一缓存键 | `node=cache` |
| `E_OPERATION_ORDER` | 批处理使用 `append → retract` | `node=batch` |
| `E_LINEAGE_BROKEN` | 删除 `budget` 最终合并缓存条目后追溯 | `slot=budget`, `node=merge:u4:budget` |
| `E_CACHE_CORRUPT` | 篡改缓存条目校验和后读取 | `node=cache` |

单元测试另复现了：

```text
E_PENDING_SLOT_MISSING  slot=budget
E_THRESHOLD_CONFLICT    slot=city
E_LINEAGE_STALE         slot=budget, node=summary
```

## 6. HTTP 冒烟

`ApiTests` 在随机本地端口启动 `ThreadingHTTPServer`，实际完成：

```text
GET  /
GET  /api/state
POST /api/retract
POST /api/append
GET  /api/trace?slot=budget
GET  /api/cache?key=<summary-cache-key>
```

全部成功；静态页含 SlotLine 标题且无外部 `https://` 资源，追加后快照版本至少为 3，追溯返回 `verified=true`，缓存按键读取返回 `verified=true`。服务仅绑定 `127.0.0.1`。
