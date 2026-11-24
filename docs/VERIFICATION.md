# 本地验证记录

验证日期：2026-11-25  
工作区：`/home/zsl/feedback/uid2d1b8ac8-default-398`  
运行环境：Python 3.10.12、pytest 8.2.0（代码兼容题面要求的 Python 3.11+）

## 命令

```bash
python3 tools/generate_samples.py --output samples
python3 -m smugglebench validate --workspace samples
python3 -m smugglebench run --workspace samples --workdir .workbench-final --concurrency 4
python3 -m smugglebench run --workspace samples --workdir .workbench-final --concurrency 1
python3 -m pytest -q
```

## 样本与规范化

- 固定种子：`20261125`
- 观测时钟：`1700000000000` ms
- 报文记录：13
- 变异：10
- 规则记录：10（v1 + v2 分片/SQLite）
- 非法拒绝：0
- 活动规则：`chunk-anomaly`、`chunk-observation-v1`、`te-cl-observation`（第 2 代）、`trailer-injection`、`x-trace-observer`
- 墓碑覆盖：`duplicate-cl-observation`、`obsolete-rule`

## 工作台聚合结果

```json
{
  "completed_connections": 9,
  "discrepancies": {
    "D_BOUNDARY": 4,
    "D_CHUNK_LENGTH": 2,
    "D_CONTENT_LENGTH": 3,
    "D_DUP_HEADER": 1,
    "D_FRAMING": 1,
    "D_TE_CL": 1,
    "D_TRAILER": 1
  },
  "errors": {
    "E_MIX_CONN": 1,
    "E_MSG_GAP": 1
  },
  "mutation_count": 10,
  "overturn_count": 2,
  "verdicts": {
    "CLEAN": 3,
    "ERROR": 2,
    "SMUGGLED": 5
  }
}
```

关键变异：

| 变异 | 最终判定 | 分歧 | 说明 |
| --- | --- | --- | --- |
| `m01-te-cl-conflict` | `SMUGGLED` | `D_TE_CL`, `D_FRAMING`, `D_CONTENT_LENGTH`, `D_CHUNK_LENGTH`, `D_BOUNDARY` | 状态机取 chunked，回显中间件取 CL |
| `m02-duplicate-content-length` | `SMUGGLED` | `D_DUP_HEADER`, `D_CONTENT_LENGTH`, `D_BOUNDARY` | 本地首个 CL 与回显末个 CL 分歧 |
| `m03-chunk-length-distortion` | `SMUGGLED` | `D_CHUNK_LENGTH`, `D_CONTENT_LENGTH`, `D_BOUNDARY` | 严格块长度与宽松前缀解析分歧 |
| `m04-trailer-injection` | `SMUGGLED` | `D_TRAILER` | 本地保留尾字段，回显中间件忽略 |
| `m05-boundary-injection` | `SMUGGLED` | `D_BOUNDARY` | 本地识别第二个请求边界，回显保留剩余字节 |
| `m06-flag-control` | `CLEAN` | 无 | 本地规则预判为风险，真实回显确认无分歧，推翻 1 次 |
| `m07-gap` | `ERROR` | 无 | `E_MSG_GAP`，缺失区间 `[[2,2]]` |
| `m08-delayed-clean` | `CLEAN` | 无 | 回显档位 120 ms，实测延迟大于 110 ms |
| `m09-duplicate-delivery` | `CLEAN` | 无 | 重复投递按首次值，重复计数 1 |
| `m10-mixed-connection` | `ERROR` | 无 | `E_MIX_CONN` |

## 一致性验证

- 并发度 4 与并发度 1 的 `deterministic_summary` 完全相等。
- 相同变异在相同种子下重复运行得到相同判定、分歧集合、压制规则和异常码。
- 延迟档位 120 ms 的实测往返延迟大于 120 ms（本次约 164 ms）；其他无档位请求为实际回环耗时，未使用固定常量。
- 检查点重启恢复返回 `checkpoint.used=true`、`reason=RESTORED`，已完成连接不重发。
- 强制终止演练：在完整检查点出现、诊断报告尚未生成时发送 `SIGKILL`；退出码为 `-9` 且无退出报告，重启后恢复 `reason=RESTORED`、`rebuilt=false`、`residue=[]`，补齐全部 10 条变异。
- 规则补丁为 `te-cl-observation` 增加第 3 代墓碑后，闭包为：
  - 受影响连接：`conn-tecl`、`conn-trailer`；
  - 受影响变异：`m01-te-cl-conflict`、`m04-trailer-injection`；
  - 验证结果：`EQUAL_TO_FULL_REPLAY`。
- 规则依赖环测试返回 `E_RULE_CYCLE` 并列出 `te-cl-observation`、`trailer-injection`。
- 在途字节预算测试返回 `E_BUDGET` 且不发送。
- 50 ms 连接截止配合 500 ms 回显延迟时返回 `E_TIMEOUT`，同连接后续变异 `sent=false`。
- 提前关闭、503 状态、不可达端口分别返回 `E_PEER_CLOSED`、`E_UNEXPECTED_STATUS`、`E_ECHO_UNREACHABLE`，其他连接继续执行。

## 测试结果

```text
........................                                                 [100%]
29 passed in 6.27s
```

测试覆盖：

- 严格行级拒绝与继续加载；
- v1 观测时钟换算与系统时间隔离；
- v2 分片/SQLite 合并与墓碑覆盖；
- 乱序、重复、缺口、混连；
- 流水线切分、定长/分块/Trailer 状态；
- 规则命中的状态/偏移/代数；
- 冲突动作稳定裁决与依赖环；
- 真实回显四类走私分歧；
- 实测延迟、推翻计数、并发/串行稳定字段；
- 在途预算、超时终止、提前关闭、非预期状态、不可达；
- 文件锁冲突、检查点原子恢复、半写残留识别；
- 规则补丁闭包与全量重演一致性。

完整诊断报告保存在 `artifacts/verification/diagnostic_report.json`，强制终止恢复摘要保存在 `artifacts/verification/kill-resume.json`。
