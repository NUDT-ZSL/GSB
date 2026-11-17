# blocksnap 技术说明

面向 Windows 本地教学/排障的块存储写时复制（CoW）快照与回滚组件。无容器、
无数据库、无网络依赖，仅依赖 Python 3.11+ 与 numpy。

## 1. 目录与存储布局

```
<store>/
  manifest.json              # 整数 format_version + content_digest 的快照清单
  manifest.json.bak          # 整体替换前的只读旧清单
  state.json                 # detached（回滚后）等持久状态
  live.bitmap                # 当前开放周期的变更块位图（numpy uint8 打包）
  objects/sha256/xx/<digest> # 内容寻址对象：基线、CoW 块、状态块、位图
  journal/rollback.json      # 回滚日志（PREPARED / COMMITTED）
  journal/rollback.tmp       # 临时区域镜像
  journal/rollback.done      # 完成标记
  audits/audit-vN-sM.json    # 按清单版本持久化的审计结论（整体替换）
  audits/latest.json
  logs/events.log.jsonl      # 结构化业务日志（四类业务码）
```

## 2. 核心模型

* 卷 `Volume`：固定块大小（512/1024/2048/4096/8192/16384/32768/65536），
  镜像长度必须严格等于 `block_size * block_count`，元数据旁挂 `.meta.json`，
  `volume_version` 仅在快照创建成功时单调递增。
* 位图 `Bitmap`：`ceil(block_count/8)` 字节，小端位序，padding 位强制为零，
  位图长度与卷块数在构造期强校验。
* 快照 `SnapshotEntry` 同时持有两类内容寻址引用：
  * `blocks`：本周期稀疏写前 CoW 块，配合位图用于位图差分回滚；
  * `state_blocks`：创建时刻全卷逐块引用（内容寻址去重，未变块零新增对象），
    用于全量比对、链回放与审计，保证三条路径可独立交叉验证。
* 写路径：块首次被改写时记录写前值与位图，重复改写只更新卷，不重复建对象；
  每次写都 fsync，位图与清单原子落盘。

## 3. 三条回滚路径（结果逐位一致）

选择规则（`RollbackEngine.choose_path`，默认阈值 0.25）：

* 目标快照存在前驱周期 → `incremental_chain`；
* 单周期且 `changed_blocks / block_count < 阈值` → `bitmap_diff`；
* 单周期且比例 ≥ 阈值 → `full_compare`。

三条路径都先在 `journal/rollback.tmp` 生成完整目标镜像，再以同目录
`os.replace` 原子替换卷镜像。峰值内存估计（整数字节）：

* bitmap：`block_count*block_size + changed_blocks*block_size`
* full：`2*block_count*block_size + block_count*8`
* chain：`block_count*block_size + block_count*8`

报告同时给出 `peak_memory_estimate` 与进程 `observed_peak_memory`。

## 4. 原子切换与中断恢复

`Journal` 状态机：`PREPARED → COMMITTED`。崩溃后重开 store 时
`SnapshotManager` 自动执行 `recover_pending`：

* 临时镜像缺失/截断/摘要不符/清单版本或摘要变化 → 中止并清理，当前卷不受污染；
* 临时镜像完整且摘要等于目标 `state_digest` → 完成原子替换并标记 detached。

未完成回滚永远不会被视为成功，journal 未清理前禁止新建快照与清单替换。

## 5. 清单整体替换

`Manifest.content_digest` 覆盖除摘要外的全部规范 JSON 字段。格式版本变化或
内容摘要不一致即触发 `replace_manifest`：旧清单复制为只读 `.bak`，新条目全量
替换（禁止增量合并），父链按 sequence 重建，并校验 bundle 内全部对象可达，
缺失对象以 `E_CHAIN` 拒绝。

## 6. 业务码

| 码 | 触发场景 | 关键字段 |
| --- | --- | --- |
| E_VOLUME | 块大小/镜像长度/块号/十六进制载荷非法 | location, block_index |
| E_SNAPSHOT | 重名、位图几何、清单摘要、journal 冲突 | location, name |
| E_INTEGRITY | 引用块摘要与清单登记不符 | first_corrupt_block |
| E_CHAIN | 基线/位图/块对象引用缺失 | missing_object |

完整性校验发生在任何写入之前，失败时卷内容与回滚前逐字节一致；所有业务码写入
`logs/events.log.jsonl`，可用 `query_logs` 或 `blocksnap logs` 查询。

## 7. 审计

`run_audit` 校验：位图索引集合与 CoW 引用集合一致、changed_count 一致、
state_blocks 覆盖全卷且逐块摘要可验、链拓扑 parent 链接正确、清单 sequence
与快照版本号匹配、清单 content_digest 有效。结论按 `v<format_version>-s<sequence>`
持久化，同版本重审整体覆盖（结论逐项一致），版本变化产生新文件并移动 latest。
