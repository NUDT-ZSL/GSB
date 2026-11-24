# memguard 技术说明

离线内存边界守卫组件：解析本地确定性生成的访问轨迹（JSONL）与区域声明（JSON），
在 SQLite 中维护区域越界账本与水位，输出区域级越界结论与证据链。

## 结构

- `memguard/parser.py` — 严格解析规范化层：仅接受 JSON 整数（拒绝 bool/float，不 coercion），
  非法记录产出 `(file_id, line_no, code)` 拒绝记录并继续处理其余记录；整文件不可读时产出
  单条 line_no=0 拒绝，不中断同批其他输入。
- `memguard/resolution.py` — 声明裁决：同 `region_id` 按 `(generation, tombstone)` 取权威；
  父引用不可解析 → `E_PARENT_UNKNOWN`；父链成环 → `E_REGION_CYCLE`（列出环内 region_id）；
  `affected_regions` 沿新旧父子图双向求传递闭包，得到需重判区域集合。
- `memguard/judge.py` — 单次访问判定，仅用规范化整数偏移。优先级（确定性）：
  `E_AFTER_LIFE` > `E_ANCESTOR_OVER` > `E_BOUND_OVER`。无父区域时祖先区间即整个 arena。
- `memguard/store.py` — SQLite 账本。`traces` 为事实源；`ledger`/`evidence` 对受影响区域
  按稳定键 `(access_seq, trace_id)` 确定性重算，因此乱序/重复/缺口/分批到达与从零全量
  串行处理逐条一致（构造性保证）。重复 `access_seq` 首现（稳定序）为真值，其余计 `dup_count`。
  每批导入为单事务；声明前置校验失败整体回滚，仅拒绝日志（旁路）落库。
- `memguard/checkpoint.py` — 快照原子替换（临时文件 → 重解析校验 → `os.replace`）、
  跨平台非阻塞文件锁（POSIX `flock` / Windows `msvcrt.locking`），冲突抛 `E_LOCK`。
- `memguard/engine.py` — 批编排与恢复边界：每批提交后在 SQLite `checkpoints` 表记录
  账本摘要（SHA-256），并原子替换 `guard.snapshot.json`。恢复时重放比对摘要：
  不一致判为半写残留并从 `traces` 事实源重建；DB 检查点一致而快照滞后/缺失时以 DB 为准
  刷新快照。`LEDGER_LAYOUT_VERSION`/`SNAPSHOT_VERSION`/`CHECKPOINT_STRUCT_VERSION`
  任一变化即整体重建，不做增量续接。
- `memguard/cli.py` — `import` / `query` / `export` / `verify` 四子命令。
  退出码：0 全部成功，1 部分失败（含拒绝记录、违规、锁冲突、检查点异常），2 参数错误。
- `tools/gen_samples.py` — 固定种子（默认 20261125）样本生成器，产出声明 v1/v2、
  两个乱序批次（含重复、缺口、4 条非法行）与 `truth.json` 真值同步标记
  （由生成器内独立参考实现计算，含 canonical 轨迹摘要）。

## 业务码

| 码 | 含义 |
| --- | --- |
| `E_BOUND_OVER` | 落在祖先区间内但越过自身区域边界 |
| `E_ANCESTOR_OVER` | 跨越祖先区间交集（无父时即 arena 界） |
| `E_AFTER_LIFE` | 访问时间戳晚于区域生命周期结束 |
| `E_SEQ_GAP` | `[min_seq, watermark]` 内存在缺失序号（报告给出缺失区间） |
| `E_REGION_CYCLE` | 区域父引用成环 |
| `E_PARENT_UNKNOWN` | 父区域引用不可解析 |
| `E_DUP_REGION` / `E_DUP_TRACE` | 文件内 region_id / trace_id 重复 |
| `E_JSON_LINE` / `E_SCHEMA_FIELD` / `E_KIND_UNKNOWN` | 解析/字段/枚举校验失败 |
| `E_LOCK` | 并发写锁冲突 |
| `E_CHECKPOINT` | 检查点摘要不一致（半写残留） |
| `E_VERSION` | 账本格局/快照/检查点结构版本漂移 |

## 检查点与恢复边界

- 每批单事务提交后写入 `(batch_seq, digest)` 检查点行并原子替换快照文件。
- 进程被强制终止后不承诺生成退出报告；下次启动只从最后一个完整检查点继续。
- 恢复时重算账本摘要与检查点比对：不一致 → 从 `traces` 重建账本并刷新检查点。

## 使用

```bash
python3.12 -m venv .venv && .venv/bin/pip install numpy pytest
.venv/bin/python tools/gen_samples.py --out artifacts/samples
.venv/bin/python -m memguard import --workdir work --declarations artifacts/samples/declarations.json
.venv/bin/python -m memguard import --workdir work --traces artifacts/samples/traces_batch1.jsonl
.venv/bin/python -m memguard import --workdir work --traces artifacts/samples/traces_batch2.jsonl
.venv/bin/python -m memguard import --workdir work --declarations artifacts/samples/declarations_v2.json
.venv/bin/python -m memguard query  --workdir work --region R_BUF
.venv/bin/python -m memguard export --workdir work --out artifacts/report.json
.venv/bin/python -m memguard verify --workdir work
.venv/bin/python -m pytest tests/ -q
```

Windows 下命令相同（`python` 替换解释器路径即可）；文件锁与原子替换均跨平台。

## 实际验证结果（2026-11-25，Python 3.12.13，numpy 2.5.5，pytest 9.2.0）

- `pytest tests/ -q`：**23 passed**（越界判定、解析拒绝、乱序/重复/缺口等价、
  版本演进与墓碑、环引用、检查点半写恢复、强制终止续跑、并发锁冲突、CLI 退出码）。
- 固定种子样本（78 条轨迹）：两批乱序导入后报告与 `truth.json` 独立真值**逐条一致**；
  随机重排 5 批导入与单批串行导入的区域结论、证据集合与拒绝清单完全一致。
- `R_BUF` 结论：`VIOLATION`，码集合 `{E_BOUND_OVER, E_ANCESTOR_OVER, E_AFTER_LIFE, E_SEQ_GAP}`，
  read 水位 41、重复 1、缺口 `[[7,7],[23,23]]`。
- 声明 v2 演进：`R_IO` 区间收缩后由 `CLEAN` 翻转为 `E_BOUND_OVER`（证据 base_offset=2440），
  `R_OLD` 被墓碑移除；受影响集合重判与从零全量重判逐条一致。
- 强制终止（提交后、快照替换前 `os._exit(86)`）：恢复后 `verify` 通过，结论与未中断全量一致。
- 锁冲突：持锁导入返回 `E_LOCK`，账本与快照零变化，无 `.tmp` 残留文件。
