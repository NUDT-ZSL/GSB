# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）。

## 环境

- Python：`Python 3.10.12`
- 虚拟环境：`.venv/`
- numpy：`2.2.6`
- 持久化：Python 标准库 `sqlite3`
- 未使用 Docker 或其他容器；所有输入均来自本地固定种子样本。

初始化命令：

```bash
virtualenv --clear -p python3.10 .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

## 确定性样本

```bash
.venv/bin/python scripts/generate_sample.py --out samples/fixed_seed
```

实际输出：

```text
{"generated": "samples/fixed_seed", "seed": 20261126, "files": 7}
```

连续生成到两个临时目录后，以下文件逐字节比较一致：

```text
rgb.png
gt_v1.json
gt_rle.json
gt_v2/shard_000.json
gt_v2/shard_001.json
DETERMINISTIC_SAMPLE_JSON_PNG=OK
```

## 端到端审计

```bash
.venv/bin/panoptic-audit run --config configs/run_config.json --fresh
```

实际结果：

```text
ledger_revision: 5
probes: 10
failures: 3
self_check: PASS
report_checksum: sha256:fdf3778c021736aea221276011b8878f27fd8348c7b88fa8e7174f58aac71c11
```

3 条 failures 对应 1 个像素归属失配探测点和 2 个被拒绝的非法计划点；重复坐标点进入 `suppressed_probes`。

实际探测顺序与路径：

```text
0 P-IGNORE     fast
1 P-PERSON-1   fast
2 P-PERSON-2   fast
3 P-VEHICLE    fast
4 P-MISMATCH   fast
5 P-ROAD       fast
6 P-LOW-1      slow
7 P-LOW-2      slow
8 P-LOW-3      degraded_aggregate
9 P-LOW-4      degraded_aggregate
```

计划处理结果：

```text
rejected:   P-BAD-OUT-OF-RANGE, P-BAD-MISSING
suppressed: P-DUP-LOW
```

首个语义冲突位置：

```json
{"x": 15, "y": 1}
```

样本共 4 个冲突像素，`conflict_per_probe_ratio=0.4`，低于配置阈值 `0.5`，因此正常生成裁决账本；阈值改为 `0.0` 时会返回 `E_GT_SEMANTIC_CONFLICT`。

## SQLite 账本与重放

账本表：

```text
commit_head
ledger_meta
pixel_ledger
probe_ledger
progress_snapshot
source_checksum
```

本轮 2 个探测点提交一次，revision 链实际为：

```text
[(0, -1), (1, 0), (2, 1), (3, 2), (4, 3), (5, 4)]
probe_rows = 10
```

重放示例：

```bash
.venv/bin/panoptic-audit replay \
  --config configs/run_config.json \
  --x 4 --y 6 --ledger-revision 5
```

实际返回要点：

```text
{'x': 4, 'y': 6} OK_PROBE_CONFIRMED_DEGRADED degraded_aggregate ['AGGREGATE_LEDGER']
```

## 审计报告再生成一致性

连续执行：

```bash
sha256sum artifacts/audit_report.json
.venv/bin/panoptic-audit audit --config configs/run_config.json
sha256sum artifacts/audit_report.json
```

两次原始文件 SHA-256 相同：

```text
32729aff8bc9daac86bfa2d866541f918fffb46712c0784be67a0609699c5a87
```

重复执行 `run` 时已确认 UID 被跳过，revision 保持 `5`，不会重复计费。首次运行的 `elapsed_ns` 来自 `time.perf_counter_ns()` 实测；该值随 SQLite 提交冻结，之后从同一账本版本再生成的报告保持一致。

## 自动化测试

```bash
.venv/bin/python -m compileall -q src scripts tests
.venv/bin/python -m unittest discover -s tests -v
```

实际结果：

```text
COMPILEALL_OK
Ran 16 tests in 0.804s

OK
```

测试覆盖：

- v1/v2/RLE 正常解析、未知版本 `E_FORMAT_UNSUPPORTED`、缺字段和校验和篡改 `E_FORMAT_CORRUPT`。
- 重复坐标优先级、同优先级计划顺序、非法点拒绝。
- 同权重 RLE 优先级破平。
- `E_GT_SEMANTIC_CONFLICT` 与首个冲突位置。
- SQLite 原子提交、重复 UID 防护、重开账本。
- 高水位版本回退检测 `E_SNAPSHOT_ROLLBACK`。
- 记录校验和破坏和垃圾数据库检测 `E_SNAPSHOT_CORRUPT`。
- 端到端报告、降级、审计再生成、重复运行不计费。
- 报告/账本偏差返回 `E_INCONSISTENT` 并定位首个探测点。

## 验收结论

本地样本已验证三格式解析、主动探测、语义冲突裁决、快慢路径、预算降级、SQLite 持久化、版本/校验和保护、审计报告再生成以及跨模块一致性自检；16 个自动化测试全部通过。
