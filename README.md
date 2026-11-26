# 多模态协同推理自适应路由诊断工作台

本项目在本地 Linux/WSL Ubuntu 上实现一个完全确定性的多模态路由诊断工作台。输入由固定种子脚本生成，运行只依赖 Python 3.10+ 标准库与 `numpy`，不使用 Docker、网络或外部数据集。

重点验证两条主线：

- v1 单文件 JSON 与 v2 分片 JSON + SQLite 元数据之间的门限配置互操作、填充来源、显式丢弃与语义损失检测。
- 四节点内容寻址路由缓存在配置代数升级、请求修订、资源裁决、并行调度和强制终止恢复下的一致性。

## 快速开始

```bash
cd /home/zsl/feedback/uid2d1b8ac8-default-482
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

# 固定种子样例
mm-router generate --out artifacts/demo/sample --seed 20261127 --count 16

# 完整验收：迁移、路由、失效、调度、恢复、提交、诊断
mm-router demo --out artifacts/demo/run --seed 20261127

# 标准库测试
python -m unittest discover -s tests -v
```

若不想安装包，可使用：

```bash
PYTHONPATH=src python -m mm_router demo --out artifacts/demo/run
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 四节点路由流水线

每个请求固定执行四个节点，每个节点独立内容寻址：

1. `N1_FEATURE`：从 `prompt_text`、`image_seed`、文本/图像修订和 `request_id` 派生确定性向量。
2. `N2_SIMILARITY`：按精度档计算 text/image/joint 相似度，并记录浮点操作数与舍入模式。
3. `N3_CANDIDATE`：按三模态门限、候选计划数与计划门限裕量筛选候选。
4. `N4_FUSION`：执行参数融合、归一化、分数融合和确定性择优；无候选时返回 `E_NO_ROUTE` 回退决策。

决策序列化不包含缓存命中状态，因此首次计算与缓存复用逐字节一致。缓存命中状态单独保存在调度跟踪中。

## 配置格式

### v1：单文件 JSON

```json
{
  "schema": "mm-threshold-config",
  "format_version": 1,
  "config": {
    "thresholds": {"text": 0.52, "image": 0.56, "joint": 0.58},
    "route_budget": 250000,
    "candidate_plan_count": 4,
    "scheduler_workers": 3,
    "precision": "fp32",
    "updated_at": "2026-11-20T08:00:00.123Z"
  }
}
```

### v2：分片 JSON + SQLite

目录包含：

- `manifest.json`：版本头、代数、修订号、绝对毫秒时间、分片声明及哈希、运行参数。
- `shards/*.json`：模态门限条目及各自增量修订号。
- `metadata.sqlite3`：`config_meta` 与 `tombstones` 表；文件哈希写入 manifest。

阅读器优先识别 `format_version` / `version_header` / `schema_version` 版本头；无版本头时再按字段集分派。v2 同时校验 manifest、SQLite 与分片哈希，禁止用缺省值掩盖冲突。

## 迁移语义

- v1 → v2：补全绝对毫秒时刻、`generation=1`、快照/条目 `revision=0`、空墓碑，并为每个补全字段写入 `ProvenanceRecord`。
- v2 → v1：条目增量修订号按显式规则 `v2_to_v1.entry_revision_discard` 丢弃并输出 `DiscardRecord`；无法表达的墓碑失效语义返回 `E_SEMANTIC_LOSS`。
- 降级投影会逐项检查 v1 可表达字段，阈值、预算、候选数、并发数、精度和快照时刻必须保持一致。

## 缓存与增量失效

缓存对象位于 `objects/<key[:2]>/<key>.json`，写入采用临时文件 + `os.replace` + 目录 `fsync`。每个缓存键显式包含：

- 输入摘要；
- 上游产物哈希；
- 门限配置版本及门限内容哈希；
- 路由预算；
- 精度档。

节点级依赖策略：

- 请求修订或全局预算/精度变化：从 `N1` 起重算。
- 配置代数、修订号、门限值或候选数变化：复用 `N1/N2`，只重算 `N3/N4`。
- 失效清单同时输出 `reused_nodes`、`recomputed_nodes` 与 `covered_nodes`，并校验两者无交集、并集覆盖全部受影响节点。

## 调度、预算与恢复

- 请求按 `request_id` 排序，以固定 `batch_size` 切批，再按连续区间切块送入 `ProcessPoolExecutor`；future 按提交顺序归并，最终按请求 ID 排序。
- 浮点预算使用精度权重（`fp16=0.5`、`fp32=1`、`fp64=2`）；执行后再次检查实测加权操作数。
- 驻留内存采用确定性调度上界模型（基础进程、worker、批内请求），用于可复现裁决；超限按显式 `priority` 在精度、延迟、内存之间选择可行组合。
- 资源超限但可调整返回 `E_RESOURCE_ADJUSTED`；无任何可行组合返回 `E_BUDGET_EXCEEDED`。
- 每个批次完整成功后，才以原子替换方式写入包含单调版本、完成请求集合、决策记录与内容校验和的检查点。
- 新缓存对象先写 staging，检查点成功后才提升为主缓存；失败批次的 staging 整体丢弃。
- 重启后按原始批次边界跳过已提交批次；未提交内存状态不会进入检查点。恢复决策与全量重放比较业务码和完整决策字节。

## 提交控制

`CommitController` 让配置账本与调度检查点共享同一个非阻塞 `flock`：

- 锁被占用时立即返回 `E_STAGE_BUSY`；
- 新版本低于账本版本返回 `E_SNAPSHOT_ROLLBACK`；
- 期望父版本或校验和漂移时检测为 drift，并使用完整快照原子替换，不做增量修补；
- 账本与检查点内容均带 SHA-256 校验和，篡改会返回格式或检查点损坏错误。

## 一致性诊断

`build_diagnostic_report()` 独立复算并检查：

1. 四节点缓存键与产物哈希；
2. 失效计划的重算/复用交集与覆盖；
3. 恢复决策与全量重放的业务码、节点产物和字节；
4. 同一输入与配置的两次路由字节一致性。

发现偏差返回 `E_INCONSISTENT`，并带 `first_request_id` 与 `first_node`。完整演示报告写入 `diagnostic_report.json`。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `OK` | 成功 |
| `E_NO_ROUTE` | 无路由计划通过门限，已输出确定性回退决策 |
| `E_VERSION_UNSUPPORTED` | 未知配置版本或无法分派格式 |
| `E_FORMAT_CORRUPT` | 字段缺失、数值不可解析、哈希/元数据/时间戳冲突 |
| `E_SEMANTIC_LOSS` | 目标格式无法保留源语义 |
| `E_RESOURCE_ADJUSTED` | 按优先级完成资源裁决并继续 |
| `E_BUDGET_EXCEEDED` | 没有满足预算的精度/并发/批次组合 |
| `E_STAGE_BUSY` | 配置或检查点提交阶段锁被占用 |
| `E_SNAPSHOT_ROLLBACK` | 尝试提交旧版本 |
| `E_LEDGER_CONFLICT` | 同版本账本内容冲突 |
| `E_CHECKPOINT_CORRUPT` | 检查点结构、版本或校验和异常 |
| `E_INCONSISTENT` | 跨模块一致性自检失败 |

## 验收结果

固定种子 `20261127`、16 请求首版 + 18 请求演进版的本地验收结果：

- 33 项 `unittest` 全部通过。
- 路由两次重放字节一致，首版总操作数 11747。
- 演进失效计划：复用 28 个节点、重算 44 个节点、覆盖 72 个节点；交集为空，覆盖完整。
- 强制终止于检查点版本 2，恢复时跳过前 8 个已提交请求，仅重算后 8 个；与全量重放的业务码及决策字节一致。
- 资源裁决将 `fp32` 降至 `fp16`，实测加权操作数低于 15000 上限。
- 配置提交与检查点提交竞争均返回 `E_STAGE_BUSY`；漂移提交执行完整快照替换；回退返回 `E_SNAPSHOT_ROLLBACK`。
- 最终诊断代码 `OK`。

更详细的设计说明见 [TECHNICAL.md](TECHNICAL.md)，具体运行记录见 [VERIFICATION.md](VERIFICATION.md)。

