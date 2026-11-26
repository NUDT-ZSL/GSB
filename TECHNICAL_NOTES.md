# 本地诊断引擎技术说明

## 1. 交付范围

`seg_diag/` 是仅依赖 Python 标准库与 NumPy 的确定性诊断引擎，覆盖配置读取、双向迁移、掩码归一化、语义裁决、依赖失效、误差谱、版本快照、并发提交、检查点恢复和跨模块自检。

主要模块：

- `seg_diag/sample_generator.py`：固定种子 `20261127` 生成本地 16×16 RGB 样本、v1/v2 配置、掩码、float64 解算字段和真值模板。
- `seg_diag/config_io.py`：读取 v1 JSON；读取 v2 分片 JSON + SQLite 元数据库。
- `seg_diag/migration.py`：v1→v2 补全映射、默认阈值和校验和；v2→v1 支持 `discard` 与 `flatten`。
- `seg_diag/normalization.py`：按两代口径归一化，并执行严格/容差带阈值判决。
- `seg_diag/ledger.py`：优先级裁决、行级内容键、规范化语义账本和 SHA-256 校验和。
- `seg_diag/dependencies.py`：比较配置修订，输出受影响行、区域、复用集合和失效阶段后缀。
- `seg_diag/diagnostics.py`：归一化、阈值、语义归并三级绝对/相对偏差、放大比、分位数和直方图。
- `seg_diag/versioning.py`：单调版本、整体替换、进程内锁和可选 `flock` 跨进程锁。
- `seg_diag/recovery.py`：批量检查点、原子写入、重启续算和强杀恢复。
- `seg_diag/selfcheck.py`：迁移口径、独立重算、集合覆盖、检查点一致性自检。
- `scripts/verify_hard_recovery.py`：真实启动子进程并发送 `SIGKILL`，随后恢复和自检。

## 2. 运行命令

```bash
python3 --version          # 需要 3.10+
python3 -m pip install numpy
python3 scripts/generate_samples.py --force
python3 -m seg_diag.cli diagnose --workdir .
python3 -m pytest -q
python3 scripts/verify_hard_recovery.py
```

验收报告：

- `reports/diagnostic_report.json`：完整诊断报告。
- `reports/ledger.v1.json`、`reports/ledger.v2-threshold.json`、`reports/ledger.v3-mapping.json`：三代账本整体快照。
- `reports/dependency_plan_threshold.json`、`reports/dependency_plan_mapping.json`：依赖失效计划。
- `reports/hard_recovery_report.json`：真实 `SIGKILL` 后恢复报告。

## 3. 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_OK` | 执行和自检成功 |
| `E_VERSION_UNSUPPORTED` | 配置版本未知或不受支持 |
| `E_CONFIG_INVALID` | 缺字段、类别集合非法、SQLite 映射非法或分片不一致 |
| `E_STAGE_BUSY` | 已有配置快照正在提交；后到者非阻塞立即返回 |
| `E_INCONSISTENT` | 迁移口径、独立重算、覆盖集合或检查点不一致 |
| `E_MASK_INVALID` | 掩码形状、未知实例等输入数据非法 |
| `E_CHECKPOINT_INVALID` | 检查点缺失或结构损坏 |
| `E_VERSION_CONFLICT` | 用更旧版本覆盖更新快照 |

## 4. 关键口径

### 4.1 配置迁移

v1 快照保持题意中的扁平结构：`version=v1`、非负 `relative_effective_seconds` 和非空类别集合。交付样本的 v1 文件不展开映射；读取器按确定性实例编号约定 `instance_id = category_id * 100 + serial` 补全内部映射，迁移器再生成 v2 显式 `category_instance_map`、默认阈值 `0.75`、绝对毫秒时间戳和校验和。

v2→v1 时：

- `discard`：丢弃实例级阈值/置信度元数据，保留原始阈值判决生成的硬实例口径。
- `flatten`：把实例级阈值按配置规则展平（演示使用 `max`），并把已接受置信度展平为 `1.0`、拒绝为 `0.0`。

两种降级都通过“冻结阈值判决像素契约”校验，因此最终硬标签口径不变；被丢弃或展平的每个实例阈值均写入迁移动作明细。

### 4.2 语义裁决

每个像素生成候选列表，排序键为：

```text
(source_priority, instance_id)
source_priority: truth=0, v2=1, v1=2
```

因此真值模板优先于 v2，v2 优先于 v1；同一来源/多层真值模板重叠时取最小实例编号。无候选时为背景 `(category=0, instance=0, source=none)`。

账本使用 `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 生成规范字节，再计算 SHA-256。相同种子、输入和配置下，账本字节逐字节一致。

### 4.3 依赖失效

每行的内容寻址键只包含该行实际依赖的真值、v1 实例及映射片段、v2 原始通道值、置信度、相关映射阈值、默认阈值和裁决规则：

```text
row_key = sha256(canonical_json(row_dependencies))
```

配置修订后逐行比较旧/新键：

- 键变化：进入 `recompute_set`。
- 键不变：进入 `reuse_set`，可直接复用已提交行或内容缓存。
- 行所属 4×4 区域进入 `affected_regions`。
- 映射修订的阶段后缀从 `normalize_v2` 开始；阈值修订从 `threshold_decision` 开始。

覆盖检查强制：

```text
recompute_set ∩ reuse_set = ∅
recompute_set ∪ reuse_set = 全部 256 行
full_image_recompute = false
```

### 4.4 误差谱

设参考场为真值模板经 3×3 平均得到的 float64 支撑场，偏差指标包括：

```text
MAE  = mean(|candidate - reference|)
L1rel = sum(|error|) / max(sum(|reference|), tiny_protected_denominator)
gain = current_level_abs / previous_level_abs
```

输出 p50/p90/p95/p99、误差直方图、误报/漏报数和主导误差项。次正规数保护将绝对值小于 `np.finfo(float64).tiny` 的值刷新为有符号零；相对偏差分母也使用同一保护阈值。

严格策略为 `score >= threshold`。容差带策略把 `abs(score-threshold) <= tolerance` 标记为带状样本，带内默认按阈值等价/达到阈值处理，也可配置为 `strict` 或 `reject`。

### 4.5 版本、并发与恢复

账本携带单调递增 `ledger_version` 与 `checksum`。配置快照由 `VersionedSnapshot` 整体替换；同版本不同摘要返回 `E_INCONSISTENT`，旧版本覆盖返回 `E_VERSION_CONFLICT`。

`SnapshotStore.load_json_file()` 发现版本号或校验和变化时整体装入新 JSON，不做字段级补丁。`SnapshotCoordinator.begin_submission()` 使用非阻塞 `threading.Lock.acquire(blocking=False)`；配置 `lock_path` 后同时使用 `fcntl.flock(LOCK_EX | LOCK_NB)`，因此线程和跨进程竞争都会立即得到 `E_STAGE_BUSY`。

恢复状态写入：

```text
runtime/recovery/checkpoint.json
runtime/recovery/ledgers/<ledger_checksum>.json
```

检查点包含 `completed_regions`、`ledger_version`、`ledger_checksum` 和账本路径。账本与检查点均通过临时文件 + `os.replace()` 原子发布。批量边界前产生但未检查点化的内存行视为未提交中间状态，重启后整批重建；已完成区域直接跳过。

## 5. 实际验证结果

验证环境：Python 3.10.12、NumPy 1.26.4、Linux/WSL 兼容环境。

- `python3 -m pytest -q`：`20 passed`。
- `python3 -m seg_diag.cli diagnose --workdir . --seed 20261127`：退出码 0，`business_code=E_OK`。
- 最终映射修订账本：`ledger_version=3`，校验和 `36217f42b0575f00a7ca6b12c4958b2b7711cdc3c8d812fe2413b7af1824b54b`。
- 阈值修订：重算 `36` 行、复用 `220` 行，缓存 `36` 次未命中、`220` 次命中。
- 映射修订：重算 `10` 行、复用 `246` 行，缓存 `10` 次未命中、`246` 次命中。
- 两次修订的重算/复用集合均无交集，并集覆盖全部 `256` 行；未退化为整幅重算。
- 固定种子独立重建：数组摘要、账本摘要和规范字节全部匹配。
- 严格阈值误判 `30` 像素；`tolerance=0.02` 容差带策略误判 `22` 像素，其中 `12` 像素位于容差带。
- 语义归并平均语义距离 `0.01171875`，像素准确率 `0.98828125`；真值优先级把阈值级误差放大比衰减到 `0.1`。
- 并发冲突返回 `E_STAGE_BUSY`，演示中的非阻塞等待约 `0.0002` 秒。
- 进程内故障演示：提交 `R:0:0`、`R:0:1` 后，在 `R:0:2`、`R:0:3` 批次提交前终止；重启计算剩余 `14` 个区域，不重算已提交的 `2` 个区域；再次重启计算 `0` 个区域。
- 真实 `SIGKILL` 子进程退出码为 `-9`；恢复报告 `reports/hard_recovery_report.json` 为 `E_OK`，恢复自检通过。

## 6. 设计说明

本实现不使用 Docker、外部服务或网络。样本和运行状态均位于工作目录；SQLite 仅使用 Python 标准库 `sqlite3`。所有报告中的路径是当前工作区相对路径，校验和可用于复核交付时的实际运行结果。
