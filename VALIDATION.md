# 实际验证记录

日期：2026-11-17。环境：Linux 容器内 Python 3.11.0rc1，项目 `.venv` 安装 `numpy 2.4.6`、`pytest 9.2.0`；业务代码不依赖 numpy，Windows Python 3.10+ 使用相同的标准库实现。

## 1. 数据生成

命令：

```bash
.venv/bin/python scripts/generate_data.py --out data --seed 42
```

结果：15 条有效中文语料、7 行无效语料、6 条注入配置、6 类意图、34 个实体词条、4 条同义词规则、3 条冲突规则。固定种子重复生成的文件 SHA-256 完全一致（由 pytest 验证）。

## 2. 输入校验

命令：

```bash
.venv/bin/python -m intent_engine validate --corpus data/invalid_corpus.jsonl --resources data/resources --inject data/inject.json
```

实际失败清单包含：

- `BAD-EMPTY`：`E_EMPTY_SENTENCE`
- 第 2 行非法 JSON：`E_BAD_RECORD` / `JSONDecodeError`
- `BAD-INTENT`：`E_UNKNOWN_INTENT`
- `BAD-SPAN`：`E_SPAN_OUT_OF_RANGE`
- 重复 `C001`：`E_BAD_RECORD`

同时仍加载了 `BAD-OK` 与第一个 `C001`，证明坏记录被隔离且其余样本继续处理。

## 3. 基线运行

命令：

```bash
.venv/bin/python -m intent_engine run --corpus data/corpus.jsonl --resources data/resources --state-dir /tmp/baseline --report /tmp/baseline.json --fresh
```

实际结果：

```text
status=ok, complete=true
sample_count=15, success_count=15, failure_count=0
意图分布：修改地址2、投诉服务2、查询库存3、查询订单3、申请退款3、预约安装2
置信度：min=0.693023, max=0.998805, mean=0.915123, threshold=0.55
```

重点样本：

- `C003` 选中 `北京市`，候选 `北京` 因重叠被抑制。
- `C006` 的 BIESO 标签为 `BEOOBEOOOOOOO`，序列片段 `深圳/小米` 被词典同跨度结果覆盖，并记录在 `merge.covered_fragments`。
- `C013` 的 `北京` 因“查询订单”不兼容“地点”而降权到 `0.45`。
- 所有最终槽位按起点排序且互不重叠。

## 4. 注入运行

命令：

```bash
.venv/bin/python -m intent_engine run --corpus data/corpus.jsonl --resources data/resources --inject data/inject.json --state-dir /tmp/injected --report /tmp/injected.json --fresh
```

实际结果：

```text
sample_count=15, success_count=12, failure_count=3
E_EMPTY_SENTENCE: C001 / normalize / INJ-EMPTY
E_BAD_RECORD:     C002 / normalize / INJ-CHARS
E_NONFINITE_WEIGHT: C006 / sequence / INJ-NONFINITE
C003 删除“北京市”后选中“北京”
C010 confidence == 0.55（阈值注入）
6 个注入项全部出现在 injection_hits
```

同一注入配置以单线程与三线程分别运行，逐样本结果、失败清单和注入命中清单一致。

## 5. 缓存复用与增量失效

相同资源和输入第二次运行：

```text
cache hit_count=75, miss_count=0, invalidation_count=0
每个阶段 computed=0, cache_hits=15
```

随后只从实体词典删除 `华为`，使用同一缓存并执行 `--fresh`：

```text
removed entries: ["entity:品牌:华为"]
affected samples: ["C001", "C009"]
slots computed=2, sequence computed=2, merge computed=2
intent computed=0, normalize computed=0
其余 13 个样本直接复用缓存
```

## 6. 中断快照恢复

先以 `--workers 2 --interrupt-after 4 --fresh` 运行，命令退出码 0，快照停留在部分结果；随后不带 `--fresh` 运行：

```text
resumed complete=true, sample_count=15
resumed.results == uninterrupted.results
resumed.failures == uninterrupted.failures
resumed.intent_distribution == uninterrupted.intent_distribution
resumed.confidence_stats == uninterrupted.confidence_stats
```

篡改配置阈值后使用旧快照且不带 `--fresh`，实际抛出/报告 `E_CONFIG_MISMATCH`，拒绝续跑。

CLI 实测：命令返回整数退出码 `2`，报告 `status=error`，失败项业务码 `E_CONFIG_MISMATCH`，原因 `snapshot_fingerprint_mismatch`。

运行期真实发送 `SIGINT` 的实测：进程返回退出码 `0`，报告标记 `status=interrupted / complete=false`，同时 `latest.json` 是完整发布的版本 3 快照，可直接按其完成集合续跑。

## 7. pytest

```text
$ .venv/bin/python -m pytest -q
................
16 passed
```

覆盖加载业务码、稳定打分、同分字典序、槽位消解/降权、BIESO 合法性、注入复现、并发一致性、缓存复用、单实体增量失效、快照恢复、配置不匹配、快照结构及固定种子数据生成。
