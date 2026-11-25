# 验证记录

## 环境

- OS：WSL Ubuntu 22.04（无容器）
- Python：3.10.12
- NumPy：1.26.4（仅用于固定种子随机样本与向量预处理）
- SQLite：3.37.2（标准库 sqlite3）

## 复现步骤

```bash
pip install -r requirements.txt
python scripts/generate_sample.py            # 生成 sample_data/
python -m unittest discover -s tests -t .    # 32 个单元测试
python scripts/run_demo.py                   # 端到端双精度 + 快照
python scripts/collect_metrics.py            # 验收指标 -> artifacts/validation_metrics.json
python scripts/demo_invalidation.py          # 增量失效与部分复用
```

## 单元测试结果

```
Ran 32 tests in 1.8s
OK
```

覆盖映射：

| 测试文件 | 对应用例 |
| --- | --- |
| `test_recall_fusion.py` | 要求 1、2：四路打分、平局 doc_id 升序、两种去重口径、逐项重算 |
| `test_precision_diagnosis.py` | 要求 3、4：定点量化/舍入、顺序去重不变量、四级误差账本 |
| `test_scheduler.py` | 要求 5：进程池/内联同序、预算与内存业务码、非法预算前置拒绝 |
| `test_cache_snapshot.py` | 要求 6、7：失效传播、复用清单、单调版本、乐观锁、校验和、串行提交 |
| `test_validation.py` | 要求 8：四类结构化业务码与字段定位 |
| `test_selfcheck_determinism.py` | 要求 8：覆盖自检与样本逐字节确定性 |

## 固定种子样本

- 种子 `20261126`，100 篇文档、12 个查询、60 个词项、3 个类别、16 维向量。
- 两次独立生成目录 `diff -r` 无差异，manifest 记录各文件 SHA-256：
  - docs（100 条）：`1589b7fb44f85309...`
  - queries（12 条）：`668ba0c1aba9963d...`
  - term_weights（60 条）：`c0cd7dd15e43482f...`

## 双精度误差账本（scale=10^6，half_even）

12 个查询全部满足候选顺序与去重归属在 float64 / fixed 两档完全一致。

- 全局最大绝对偏差：`7.206890610012007e-06`
- 全局最大相对偏差：`1.0094586658700675e-04`
- top-k 门限在保护带 `1e-6` 内稳定：9 / 12
- 近门限候选集合两档完全一致：12 / 12
- 超保护带的 3 个查询在 `diagnoses[].threshold.stable=false` 中显式标记，
  近门限名单仍一致，说明误差未导致门限翻转，仅需关注数值裕度
- 主导误差来源均为 `inverted` 路（权重 / sqrt(doc_len) 链路量化点最多）

## 并行与预算裁决

- `workers=2, batch_size=4`：12 个查询确定性切为 3 批，进程池归并顺序与内联一致
- 候选估算 260、实际保留 65，均在上限 480 内
- 内存结构计数估算 206452 字节，上限 64 MiB
- 收紧 `candidate_budget=5` -> `E_RECALL_BUDGET`（field=`scheduler.candidate_budget`）
- 收紧 `memory_budget_bytes=1` -> `E_RESOURCE_LIMIT`（field=`scheduler.memory_budget_bytes`）

## 缓存失效与版本化

- 首次运行：12 个查询全部重算，`module_keys` 与 `recall_stats` 快照版本均为 1
- 二次无变更运行：`changed_modules=[]`，重算 0、复用 12，缓存命中全部为 true，
  快照内容幂等不推进版本
- 词项权重修订：`term_weights -> index -> recall -> fusion` 级联失效，全量重算
- 查询级融合权重修订（带 affinity）：仅前 4 个查询重算，后 8 个复用，覆盖自检通过
- 乐观锁过期提交抛 `E_VERSION_CONFLICT`；载荷损坏抛 `E_CHECKSUM`；
  全局 `commit_seq` 跨快照类型单调（1、2、3...）

## 自检结论

- 四路候选 doc_id 全部来自索引，向量路截断前全集严格等于索引文档集合
- 每个融合候选无重复 doc_id，最终分等于逐项加权贡献之和（容差 1e-9）
- 重算集合恰好等于受影响查询，且与复用集合互不相交
- 全部检查通过，状态 `ok`
