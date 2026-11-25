# Panoptic Audit Ledger

面向全景分割像素掩码的主动探测、三态真值语义裁决与账本一致性诊断引擎。所有样本均由固定种子 `20261126` 在本地生成，不访问网络目标，也不依赖 Docker。

## 快速开始

```bash
python3.10 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .

.venv/bin/python scripts/generate_sample.py --out samples/fixed_seed
.venv/bin/panoptic-audit run --config configs/run_config.json --fresh
.venv/bin/panoptic-audit verify --config configs/run_config.json
.venv/bin/python -m unittest discover -s tests -v
```

也可以不安装包，直接使用 `PYTHONPATH=src .venv/bin/python -m panoptic_audit.cli ...`。

## 本地样本

`samples/fixed_seed/` 包含：

- `rgb.png`：16×16 确定性 RGB 图像，使用标准库 `zlib/struct` 编码。
- `gt_v1.json`：v1 身份单通道、逐行式真值；每行有独立 SHA-256 校验和。
- `gt_v2/shard_000.json`、`gt_v2/shard_001.json`：v2 类别、实例、置信度三分量分片。
- `gt_v2/gt_v2_meta.sqlite`：SQLite 元数据、分片/行块校验和与实体登记表。
- `gt_rle.json`：COCO 风格 Fortran/列主序 RLE，类别、实例、忽略区三个游程块均有校验和。
- `probe_plan.json`：主动探测计划，包含正常、忽略、失配、重复、非法、低置信度回退和降级观测点。
- `manifest.json`：样本种子、实体和文件校验和清单。

样本注入了 4 个隔离语义偏差，首个行优先冲突位置是 `(x=15, y=1)`。这些冲突低于配置阈值，用于验证裁决而不触发致命冲突码。

## 运行配置

见 `configs/run_config.json`：

- `probe_time_budget_ms`：整轮探测总时间预算。
- `conflict_coverage_threshold`：冲突覆盖阈值；同时输出全局像素冲突率和“冲突像素数/有效探测点数”指标。
- `format_priority`：同权平局时使用的格式优先级，默认 `["RLE", "v2", "v1"]`。
- `ledger_version`：账本结构版本。
- `audit_output_path`：审计报告输出路径。
- `ledger_path`、`high_water_path`：SQLite 账本和版本高水位锚点。
- `probe_round_size`、`slow_budget_share_threshold`、`confidence_fast_threshold`：轮次、降级和快路径阈值。

## 核心行为

1. **三格式解析**：`src/panoptic_audit/parsers/formats.py` 独立解析 v1、v2、RLE，校验版本、必填字段、尺寸、通道长度、忽略区、实体编号、分片连续性和逐块校验和。
2. **探测队列**：`src/panoptic_audit/probes.py` 拒绝越界/缺字段/非法预算点，抑制同坐标低优先级或同优先级后出现的条目；有效点按“优先级降序、计划顺序升序”执行。
3. **语义裁决**：`src/panoptic_audit/ledger.py` 对每个像素汇总三态 `(category, instance, ignore)`，按像素可信度加权投票；同总权重时由 RLE、v2、v1 优先级破平，并记录忽略区/类别/实例冲突代价。
4. **主动探测**：`src/panoptic_audit/execution.py` 的高置信度点走内存账本快路径；低置信度点重新读取并校验源格式块，作为慢速回退路径。每个点使用 `time.perf_counter_ns()` 记录真实处理耗时。
5. **原子账本**：`src/panoptic_audit/store.py` 使用 SQLite 事务原子提交探测账本和进度账本，维护递增 revision、记录校验和、头校验和与高水位文件。
6. **降级调控**：慢路径耗时占已消耗预算比例超过阈值后，下一轮中无法直接区分格式来源的候选点只返回 `AGGREGATE_LEDGER` 聚合结果，并标记 `AGGREGATE_SOURCE_UNRESOLVED`；轮次结束时先提交账本，再进入下一轮。
7. **审计报告**：`artifacts/audit_report.json` 为每个已提交点保存坐标、三态原始字段、投票/裁决、最终账本结果、真实耗时、降级标记、业务码和重放命令。
8. **一致性自检**：`src/panoptic_audit/selfcheck.py` 重解析源格式并逐像素比对，检查报告与进度账本逐条对应、源块校验和一致、PNG 尺寸和清单一致；首个偏差会带坐标或探测点 ID 返回。

## 可复现性与计时

首次运行的 `elapsed_ns` 是本机实际处理时间，因此不同机器上的原始计时值可能不同。该值在当轮 SQLite 事务中提交并纳入记录校验和；之后执行 `panoptic-audit audit` 或重复 `run` 时不会对已确认点重新计费，而是从同一账本 revision 读取已冻结计时，所以同一账本版本重新生成的审计报告字节级一致。

## 错误码

- `E_FORMAT_UNSUPPORTED`：未知格式版本或不支持的编码/过滤器。
- `E_FORMAT_CORRUPT`：缺字段、JSON/SQLite/校验和/通道或实体校验失败。
- `E_GT_SEMANTIC_CONFLICT`：冲突覆盖超过阈值，详情含 `first_conflict`。
- `E_SNAPSHOT_ROLLBACK`：SQLite revision 低于高水位，或配置账本版本回退。
- `E_SNAPSHOT_CORRUPT`：数据库损坏、完整性检查、记录或进度校验和失败。
- `E_INCONSISTENT`：跨模块自检失败，详情含首个偏差坐标/探测点。
- `E_PLAN_INVALID`：单个探测点字段非法；非法点进入拒绝清单而不中断其他点。

## 验证记录

实际命令、输出摘要和验收结论见 `docs/VALIDATION.md`。
