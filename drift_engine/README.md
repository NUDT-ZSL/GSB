# 离线配置基线漂移归因与处置裁决引擎

纯本地、确定性的两代证据快照合并、窗口量化裁决、漂移归因闭包与 SQLite 账本引擎。
不访问网络、账号或真实主机；所有样本由固定种子（`SEED=20261125`）生成。

## 环境准备

要求 Python 3.11+。本机默认解释器为 3.10，使用 `uv` 安装无 root 的预编译 3.11：

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python numpy pytest
uv pip install -e . --python .venv/bin/python
```

验证：`.venv/bin/python --version`（3.11.18）、`.venv/bin/python -m pytest`。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `codes.py` | 稳定业务码、关系码、处置结论、退出码 |
| `atomic.py` | 临时文件 + `os.replace` 原子写、规范化 JSON |
| `values.py` | 五类型（int/bool/enum/interval/mask）归一化与顺序无关三态比较 |
| `spec.py` | 基线规范、扁平键→规范键映射、冻结集合、依赖图、主机→服务 |
| `parsers.py` | v1 扁平 JSON、v2 分片 JSON + SQLite 索引的校验/拒绝清单 |
| `merge.py` | 权威裁决、墓碑覆盖、`E_CONFLICT` 冲突诊断、统一漂移视图 |
| `adjudication.py` | 窗口量化误差界、双向判定余量、冻结/墓碑/宽限/立即裁决 |
| `attribution.py` | 依赖传播闭包、主因/从因、服务级根因、`E_DEP_CYCLE` |
| `store.py` | SQLite 账本、单事务增量推进、检查点、版本重建、重放校验 |
| `concurrency.py` | 线程池解析，结果按输入规范化排序，并发结论与串行一致 |
| `cli.py` | `import` / `decide` / `attribute` / `verify` 四类子命令 |
| `samplegen.py` | 固定种子样本生成器（含冲突、量化不确定、环、墓碑、非法记录） |

## 关键设计

### 时间基准与量化

- v1 相对秒仅以 `spec.observation_clock_ms` 换算：`abs_ms = clock + rel_seconds*1000`，
  全程不读取系统当前时间。
- v1 秒级证据量化宽度 `quantum_ms=1000`，真实首越界时刻落在存储值 ±999ms 带内；
  v2 毫秒证据 `quantum_ms=0`。
- 账龄区间 `[center_age-quantum, center_age+quantum]` 与窗口 `window_seconds*1000` 比较：
  整段在阈值外为 `IMMEDIATE`，整段在内为 `GRACE`，跨阈值为 `AMBIGUOUS`
  （`E_WINDOW_AMBIGUOUS`），并同时给出确定超窗余量 `margin_over_ms`
  与确定在窗余量 `margin_inside_ms`。

### 合并与冲突

- 权威排序：证据代次 → 代数 → 观测时刻 → 快照/记录序号，全部确定性，与导入顺序无关。
- 墓碑仅覆盖更早的非墓碑记录；无更早证据的墓碑不产生撤销态。
- 两代同键的值或漂移结论不一致输出 `E_CONFLICT`，附两侧代次/代数/来源，绝不静默取值。

### 值可比化

- v1 字符串与 v2 归化形式对齐到同一形状；类型不符 `E_TYPE_MISMATCH`，
  区间端点倒置 `E_INTERVAL_REVERSED`，枚举越界 `E_ENUM_NOT_ALLOWED`。
- 关系三态 `R_EQUAL` / `R_CONTAINS` / `R_EXCLUSIVE`，交换入参只改变 `container_side`，
  不改变关系码；每条关系附两侧证据来源。

### 账本、增量与检查点

- 账本主键 `(host, config_key)`；证据表主键 `(host, config_key, generation, algebra)`，
  支持同分片多批次叠加而不互相覆盖。
- 每轮合并在单个 SQLite 事务内推进，提交失败整体回滚，账本不被半写。
- 增量只重写受影响 `(host, key)`，未触达行逐字节不变；结果与从零全量重判逐字段一致。
- 检查点先写临时文件再 `os.replace`；`verify` 重放最后完整检查点并按账本摘要与逐行比对，
  识别损坏/半写残留 `E_CHECKPOINT_RESIDUE`，孤立临时文件不影响已提交检查点。
- 合并口径版本或归化白名单版本变化时清空账本整体重建（`E_VERSION_REBUILD`）。

## CLI 用法

```bash
# 1. 生成固定种子样本
.venv/bin/python -m drift_engine.samplegen --output-dir sample_output/demo

COMMON="--spec sample_output/demo/spec.json --db run/ledger.db --checkpoint-dir run/cp"

# 2. 导入批次（workers 可调并发，结论与串行一致）
.venv/bin/drift-engine $COMMON import \
  --manifest sample_output/demo/manifest.json --batch-id batch-1 \
  --window-seconds 60 --decision-clock-ms 1700000120000 --workers 4 \
  --report run/import.json

# 3. 裁决查询 / 归因导出 / 检查点校验
.venv/bin/drift-engine $COMMON decide    --window-seconds 12 --decision-clock-ms 1700000020500 --report run/decide.json
.venv/bin/drift-engine $COMMON attribute --window-seconds 60 --decision-clock-ms 1700000120000 --report run/attr.json
.venv/bin/drift-engine $COMMON verify --report run/verify.json
```

退出码：`0` 全部成功；`1` 部分失败（存在拒绝记录、致命快照、依赖环或检查点残留）；
`2` 参数/文件错误；`3` 未预期致命错误。

## 验证结果

`.venv/bin/python -m pytest` 共 21 个用例全部通过，覆盖：

- 两代解析拒绝（负时间戳、未知主机/键、重复、代数非单调、摘要不符、区间倒置）；
- 跨代 `E_CONFLICT`、值三态关系顺序无关、合并顺序无关；
- 窗口 `E_WINDOW_AMBIGUOUS` 量化带与双向余量、冻结不立即处置、墓碑撤销；
- 依赖环 `E_DEP_CYCLE`、主因/从因稳定排序、服务级根因；
- 增量只重判受影响项且与全量逐字段一致、并发 4 与串行结论一致；
- 单事务回滚、检查点半写/损坏残留检测、孤立临时文件隔离、版本变化整体重建；
- CLI 端到端退出码与可解析 JSON 报告。

固定样本实跑（`run/` 目录）观测到的代表性结论：

- `host-c/edge.buffer` 在 12 秒窗口、判定时刻 `1700000020500` 下量化带
  `[11500,13500]ms` 横跨 `12000ms`，判 `AMBIGUOUS / E_WINDOW_AMBIGUOUS`；
- `host-a/auth.mode` 被更高代数墓碑覆盖，判 `REVOKED`；冻结键在超窗时判 `FROZEN`；
- `host-c` 依赖环 `queue.depth → backlog.size → worker.count` 触发 `E_DEP_CYCLE`；
- 导入批次接受 14 条、拒绝 5 类业务码、0 个致命快照，退出码 `1`（部分失败）。
