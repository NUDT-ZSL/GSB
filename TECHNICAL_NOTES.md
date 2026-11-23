# MT 数据准备管线：技术说明与验证记录

面向机器翻译数据准备的本地确定性多阶段管线。语料由固定种子从受控词表
确定生成（Splitmix64 纯 Python PRNG，跨平台/跨版本稳定），不依赖网络。

## 运行方式

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install "numpy>=1.24,<2" "pytest>=7,<9"
python scripts/generate_data.py --n 3000            # 生成 data/（固定种子）
python scripts/run_pipeline.py                      # 全管线 -> reports/
python scripts/run_acceptance.py                    # 验收场景 -> reports/acceptance_report.json
python -m pytest tests/ -q                          # 49 个单元/集成测试
```

## 阶段与业务需求映射

| 需求 | 模块 | 要点 |
| --- | --- | --- |
| 1 分片摄入与对齐验证 | `mt_pipeline/ingest.py` | 按 pair_id 升序排序后校验；分片号 = 序号 // shard_size；每对输出码点区间；摘要带 sha256 |
| 2 子词切分与词表 | `mt_pipeline/subword.py` | 候选 = token 的全部合法切分（长度 ∈ [min_len,max_len]，字典序封顶）；打分 = 词频·log1p(freq) + 长度·len²；同分按边界数降序、再按切分字典序升序；词表由逐句切分结果构建（可重算） |
| 3 长度比与噪声过滤 | `mt_pipeline/filtering.py` | strict / tolerant 两种策略；tolerant 用容差带 + 次正规数保护（次正规刷零、容差以 8 ulp 兜底）+ Fraction 精确仲裁，门限附近判定不被舍入翻转 |
| 4 自适应取舍器 | `mt_pipeline/budget.py` | 观测（对数、词表、平均对齐长、平均候选数、内存余量）→ 估计精确/近似两路径比较次数与驻留内存 → exact / approx / streaming；取舍记录含观测与估计，同观测逐字节一致；覆盖性校验 |
| 5 跨路径一致性裁决 | `mt_pipeline/consistency.py` | 保留集合差异、切分一致率、词表规模偏差；差异分类 PRUNING_MISS（候选剪枝遗漏）/ TIE_RULE（平局规则不同）；保留集合分歧 → E_PATH_DIVERGENCE 含首个 pair_id 与类型 |
| 6 双重预算准入 | `mt_pipeline/budget.py` | 单对最小比较代价超总预算 → E_COMPARE_BUDGET；驻留词表超内存上限 → E_RESOURCE_LIMIT；全量装不下时降批（STREAM_BATCH 标记），不改已提交判定 |
| 7 快照版本化 | `mt_pipeline/snapshots.py` | 版本号单调递增 + 内容校验和；版本或校验和变化即整体原子替换（tmp+rename），不做增量修补；回退 → E_SNAPSHOT_ROLLBACK；损坏/校验和不符 → E_SNAPSHOT_CORRUPT |
| 8 导出与自检 | `mt_pipeline/export.py` | TSV 按 pair_id 升序；JSON 报告含取舍记录、逐样本明细、词表规模、长度比统计、保留率、业务码；自检：各阶段 pair_id 集合一致 / 剔除样本不在导出 / 词表可重算 / 指标可由明细重算 / 路径覆盖全部输入，否则 E_INCONSISTENT 指明首个分歧位置 |

## 确定性设计

- 所有 JSON 输出 `sort_keys=True`、无时间戳；校验和用 sha256（canonical JSON）。
- 数据生成用纯 Python Splitmix64，不依赖 `random` 模块的版本行为。
- 逐字节可复现口径：同一输入 + 同一词表版本 + 同一配置 + 同一初始快照状态。
  报告中 `stages.snapshots` 记录 created/replaced/noop，重跑时该字段随既有
  快照状态变化（noop），属预期的重启语义；全新状态下两次运行逐字节一致
  （`tests/test_pipeline.py::test_end_to_end_pass_and_byte_identical`）。

## 业务码

E_DUP_PAIR, E_EMPTY_SENT, E_ALIGN_RANGE, E_VOCAB_TOKEN, E_LEN_MISMATCH,
E_SCHEMA, E_VOCAB_EMPTY, E_CONFIG_INVALID, E_THRESHOLD_INVALID,
E_PATH_INCOMPLETE, E_PATH_DIVERGENCE, E_COMPARE_BUDGET, E_RESOURCE_LIMIT,
E_SNAPSHOT_ROLLBACK, E_SNAPSHOT_CORRUPT, E_INCONSISTENT

## 验证结果（2026-11-24，Python 3.10，WSL Ubuntu）

- `pytest`：49 passed。覆盖：摄入错误码、切分确定性与平局裁决、门限双策略
  与次正规数保护、预算三路径与双拒绝、快照替换/回退/损坏、跨路径分歧、
  端到端逐字节一致、自检 E_INCONSISTENT。
- 主运行（n=3000，种子 20240501）：path=exact，kept=2058/3000，
  keep_rate=0.686，自检 PASS；跨路径一致率 1.0，保留集合差异 0，
  词表规模偏差 0（reports/pipeline_report.json）。
- 规模实验（n=12000）：exact 估计比较 72,858,060 次 > 预算 5e7，取舍器自动
  切到 approx（估计 150,602 次），kept=8110/12000，自检 PASS，跨路径一致率
  1.0（reports/scale_12k/pipeline_report.json）。
- 验收场景（reports/acceptance_report.json，all_pass=true）：
  - 收紧 compare_budget=10 → E_COMPARE_BUDGET ✓
  - 收紧 memory_limit=8 → E_RESOURCE_LIMIT ✓
  - 旧版本快照写入 → E_SNAPSHOT_ROLLBACK ✓
  - 损坏快照读取 → E_SNAPSHOT_CORRUPT ✓

## 已知取舍

- TIE_RULE 分歧类型为防御性分类：两路径共享同一打分与平局裁决，实际运行
  中分歧均来自候选剪枝（PRUNING_MISS）；TIE_RULE 分支保留用于检测未来
  近似路径实现偏离平局规则的情形。
- 近似路径与精确路径共享全局词频统计，差异仅来自候选剪枝与签名去重；
  流式分批只改变执行批次，不改变判定结果。
