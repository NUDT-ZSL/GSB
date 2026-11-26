# 实际验证结果（本地确定性样本）

- 环境：WSL2 Ubuntu 22.04.5 LTS（内核 `6.18.33.2-microsoft-standard-WSL2`）
- Python `3.10.12`；标准库 `sqlite3`（SQLite `3.37.2`）、`json`、`decimal`、`fcntl`
- pytest `8.2.0`（也可用 `python3 -m unittest discover -s tests`，结果相同）
- 无 Docker、无第三方运行时依赖、无网络访问

完整原始输出见 `docs/acceptance_output.txt`（由 `bash scripts/run_acceptance.sh` 生成）。

## 1. 复现方式

```bash
bash scripts/run_acceptance.sh
# 或分步：
bash scripts/prepare_samples.sh
python3 -m gsb --samples samples --state state load
python3 -m gsb --samples samples --state state merge
python3 -m gsb --samples samples --state state judge
python3 -m gsb --samples samples --state state status
python3 -m gsb --samples samples --state state verify
python3 -m pytest tests -q
```

## 2. 种子确定性

- `engineering_delivery/seed_samples.py` 复制为工作区 `seed_samples.py` 后运行，
  生成 14 事务 / 6 段主库流、11 实例 / 19 记录从库流、v1 单文件策略、
  2 个 v2 分片 + `policy_v2/meta.sqlite`、补丁与替换快照。
- 两次生成的全部 JSON/分片逐字节一致（`diff -r` 无差异）。
- v2 内容摘要：`sha256:864fb64d...24acdcb`，引擎 `load` 会重算并与 SQLite 核对。

## 3. 两代策略合并

实测裁决依据计数：`{'clock': 1, 'generation': 1, 'format_priority': 1, 'single_source': 8}`。

- `rep-alpha`：v2 时钟更新 → `clock`，v2 胜。
- `rep-beta`：时钟相同（`T0+61000`），v2 代数 9 > v1 代数 6 → `generation`。
- `rep-gamma`：时钟/代数均相同（且为墓碑）→ `format_priority`（v2>v1）。
- `rep-rho` 仅 v1；`rep-eta/theta/nu/xi/iota/kappa/delta` 仅 v2。
- v1 相对秒换算证据示例（`rep-rho`, `relative_retention_seconds=0`）：
  绝对截止 `1730000070350`，`unit_ms=1000 ROUND_FLOOR`，舍弃余量 `350ms`。

## 4. 延迟与定点余量

100ms 标度、`ROUND_FLOOR`、余量逐实例结转，实测：

| 实例 | 精确累计 | 定点单位 | 结转余量 | 延迟告警 |
| --- | ---: | ---: | ---: | --- |
| rep-alpha | 321ms | 3（300ms） | 21ms | 否 |
| rep-beta | 13130ms | 131（13100ms） | 30ms | 是（末次 12000ms > 8000ms 阈值） |
| rep-delta | 102ms | 1 | 2ms | 否 |
| rep-eta | 85ms | 0 | 85ms | 否 |

恒等式 `delay_fixed_ms + carried_remainder_ms == delay_exact_ms` zero 误差；
同一逻辑时钟序列重复运行，延迟 JSON 与段决策摘要逐位一致（测试断言）。

## 5. 段裁决与精度敏感

`status` 实测段状态：

```
[(1,'archivable'), (2,'retained'), (3,'retained'),
 (4,'retained'), (5,'retained'), (6,'alert')]
```

- 段 1：无活动引用 → `archivable`。
- 段 2：独立门限 `...70348 <= now ...70350` 本可归档，但与段 3 粗标度
  `delta=4ms < tolerance=5ms` 结为批次 `b-2-3`，批次门限在未来，保守改判
  `retained` 且 `precision_hold=true`。
- 精度对 `2|3`：粗标度 `delta=4` 相连；提高 1000 倍标度后 `delta≈19.47`
  分离，细标度下段 2/3 均为可归档 → `flip_if_scale_increased=true`，
  原因 `batch_link_and_verdict_change`。
- 精度对 `4|5`：粗标度 `delta=3`、细标度 `delta≈3.03`，均 `<5` 仍相连，
  两段均 `retained` → `flip_if_scale_increased=false`。
- 每条敏感记录含 `participating_instances` 与完整 `error_source_chain`
  （div/mul/add 局部与累计误差上界，粗标度累计界 `32.5ms`/节点）。
- 段 6：`rep-iota pin` 与 `rep-kappa expedite` 矛盾 →
  `status=alert, error_code=E_CONFLICT_POLICY`，冲突依据（来源/签名/时钟/代数）落库。

## 6. 补丁闭包与快照替换

- theta 补丁（修订 `1 -> 2`）：变更实例 `['rep-theta']`，
  `dirty_segments=[2,3,4]`，`reusable_segments=[1,5,6]`，
  沿用决策 `[1,5,6]`、沿用精度对 `['5|6']`；随后 `judge` 仅提交 `[2,3,4]`。
- v3 全量快照（修订 `2 -> 3`，`3.0.0/gen12`）：检测到
  `['rep-rho','rep-theta','rep-xi']` 差异（全量快照同时回退了 theta 补丁并删除
  v1-only 的 rho），闭包 `dirty=[2,3,4,5,6]`，仅沿用段 1；`judge` 后 verify 通过。
- 相同快照 + 相同 `idempotency_key` 再提交：`idempotent=true`，不产生新修订。
- 在 v3 活动时提交 `2.4.0` 回退快照：退出码 2，错误码
  `E_VERSION_ROLLBACK`（details: active_version=3.0.0 vs incoming=2.4.0）。

## 7. 崩溃恢复

| 注入点 | 强杀后数据库实况 | resume 行为 | 结果 |
| --- | --- | --- | --- |
| `judge --crash-after 2` | 决策行 `[1,2]`；pending 日志 `seg:1:3` | half_committed=段3，重放 `[3,4,5,6]` | verify 全绿；与干净运行 6 段摘要完全相同 |
| `merge --crash-phase merge` | 0 修订；pending `merge:1` | resume 先重放 merge 再裁决全部段 | verify 全绿 |
| `judge --crash-phase delay` | 延迟证据事务前 `os._exit(137)` | 重算并幂等 upsert | verify 全绿 |
| 真实 `kill -9` + `--slow-ms 400` | 通常 `[1,2]` 已提交、段3 pending | resume 补齐 | verify 全绿 |

验收脚本中的摘要比对：
`PASS recovery digests identical to clean run: 6 segments`。

## 8. 多进程并发与幂等

- 8 个 OS 进程并发执行 `judge --all --worker-id mp0..7`：
  `segment_decisions` 恰有 6 行、每段 1 行（无重复组），`decision_claims` 6 行；
  跨进程合计 `committed=6, idempotent=42`（8×6=48 次段结果 = 6 提交 + 42 幂等）。
- 6 进程对同一段 `judge --segment 4 --slow-ms 300` 强竞争：
  `(committed, idempotent) = (1, 5)`，唯一裁决者 `h4`。
- `judge --parallel 3` 的 `ProcessPoolExecutor` 路径同样 verify 通过。
- 重复执行 `judge`：`committed=[]`，全部段进入 `idempotent`。

## 9. 自动化测试

`python3 -m pytest tests -q` → `42 passed`；
`python3 -m unittest discover -s tests` → `Ran 42 tests ... OK`。

覆盖：定点余量恒等式/误差界、13 类校验错误码、合并三依据与来源留存、
延迟逐位一致性、裁决状态与精度翻转、补丁闭包/快照替换/回退、
三类崩溃恢复、真实 SIGKILL、8 进程并发 exactly-once、同段竞争幂等。

## 10. 已知边界（非缺陷）

- 时间戳均为逻辑毫秒；`freshness_lag_ms/position_lag_ms` 仅作证据输出，
  告警判据使用“提交→回放时延”`last_delay_ms`，以贴合可注入逻辑时钟语义。
- 精度批次是保守持有策略；是否在生产中启用批次合并可由阈值配置调整。
