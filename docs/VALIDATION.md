# 实际验证记录

验证日期：2026-11-23  
执行环境：WSL2 Ubuntu 22.04.5 LTS，非容器  
Python：3.11.18（`.venv`）  
NumPy：2.4.6  
样本种子：`20261123`

## 样本固定性

命令：

```bash
python -m corpus_dedup_workbench generate \
  --seed 20261123 --shards 4 --docs-per-shard 18 \
  --out data --config config/dedup_config.json
```

结果：

- 清单：`data/manifest.jsonl`
- 分片：4
- 每分片文档：18
- 总文档：72
- 每文档 token：192
- 每文档块：16
- 总块签名：1152

`data/sample_meta.json` 记录了完整文档族索引。重复生成会覆盖为相同 JSON 字节。

固定文件 SHA-256：

```text
f0768862e4ef8c2dc60d13fd420a70b25c3262b056d8af67255d5c7e64de970f  data/manifest.jsonl
2a8f12342f2ad4960bb1d67a77f3df94bfcc90c2f6718ac78834908370a8e9cd  config/dedup_config.json
```

将样本重新生成到临时目录后，上述两个文件哈希完全一致。

## 批量处理

命令：

```bash
python -m corpus_dedup_workbench batch --summary
```

关键结果：

| 指标 | 数值 |
|---|---:|
| 文档 | 72 |
| 保留 | 36 |
| 剔除 | 36 |
| 候选桶 | 36 |
| 空候选桶 | 24 |
| 候选对 | 72 |
| 实际比较 | 72 |
| 近重复对 | 72 |
| 签名块 | 1152 |
| 降级 | false |

首次批量结果写入 `.cache/dedup` 的内容寻址缓存；同配置 `refresh` 复用 4/4 分片、72/72 文档。

## 查询与一致性

命令：

```bash
python -m corpus_dedup_workbench consistency
```

结果：

```json
{
  "ok": true,
  "checked_docs": 72,
  "all_consistent": true,
  "precision": "1e-12",
  "max_actual_query_ms": 3.051709
}
```

逐文档查询返回块签名、摘要、完整候选轨迹、阈值留痕、判定、剔除原因和桶划分。声明精度为 `1e-12`。独立循环最大实测查询耗时约 3.05 ms；端到端自检期间的最大值为 3.23 ms，均低于配置的 50 ms 预算。

故意注入块签名偏差后，查询返回 `E_INCONSISTENT`，首个偏差阶段为 `block_signatures`，并包含两路取值与精度声明。

## 延迟预算

使用严格小于实际耗时的预算发起查询时，返回 `E_LATENCY_BUDGET`。错误包含：

- `completed_stages`
- `actual_elapsed_ms`
- `budget_ms`
- `estimated_ms`
- `candidate_comparisons_skipped = 0`

传入大于配置的预算时，有效预算仍被配置值封顶，无法通过放大请求预算掩盖延迟。

## 模式差异

命令：

```bash
python -m corpus_dedup_workbench compare-modes
```

结果：

| 指标 | 全量模式 | 摘要模式 |
|---|---:|---:|
| 剔除 | 36 | 27 |
| 保留 | 36 | 45 |
| 候选对 | 72 | 72 |
| 实际比较 | 72 | 48 |
| 摘要门控跳过 | 0 | 24 |

仅全量模式剔除 9 篇，仅摘要模式剔除 0 篇。归因：

- 摘要冲突遗漏：8
- 命中块减少：1
- 其他：0

命中位置差异覆盖 72 个候选对。样本中 `doc-000065` 展示了命中块减少：全量相似度 `0.75`，摘要锚点相似度 `0.625`；另有多篇文档因共同摘要带不足被门控遗漏。

## 快照与缓存

- 快照类型：`shard_statistics`、`removed_list`
- 同配置 refresh 后两个快照均由 v1 递增到 v2
- 快照内容校验和长度为 64 位十六进制 SHA-256
- 篡改冻结载荷后读取返回 `E_SNAPSHOT_CORRUPT`
- 尝试发布版本 2、3 覆盖版本 3 返回 `E_SNAPSHOT_ROLLBACK`
- 阈值变更后 4 个旧缓存键全部失效，72 篇文档由新键重算，复用集合为空

## 资源上限

- `max_docs_per_batch=10` 处理 72 篇文档时返回 `E_RESOURCE_LIMIT`
- `max_candidate_comparisons=1, resource_policy=error` 返回 `E_RESOURCE_LIMIT`
- 批级上限 30、`resource_policy=error` 时，在第二分片消耗剩余 12 个额度后返回 `E_RESOURCE_LIMIT`
- 批级上限 30、`resource_policy=degrade` 时，4 个分片实际比较数依次为 `18, 12, 0, 0`，总计 30，并标记 `candidate_comparisons_truncated`
- `max_cache_entries=1` 时发生 LRU 逐出并标记 `cache_lru_eviction`

## 自动化测试

命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果：

```text
Ran 34 tests in 3.885s

OK
```

测试覆盖：

- 签名确定性、非法签名长度、文档过短
- 精确阈值、容差带、次正规数保护
- 窗口边界、空候选桶、桶/候选数量复核
- 质量代表、命中位置、阈值留痕、模式差异归因
- 质量分相同时按 `doc_id` 升序保留代表
- 72 篇查询/批量一致性、`E_INCONSISTENT`
- 延迟预算与预算封顶
- 缓存复用、键变化、配置失效与重算集合
- 快照整对象替换、损坏、回退
- 文档/候选/缓存资源上限及降级类别
- 分片间批级候选额度递减与截断
- 模式切换导致快照校验和变化
- 并发查询与连续模式/快照切换
- Web 首页、dashboard、查询和配置 POST

## Web 实际启动验证

命令：

```bash
python -m corpus_dedup_workbench serve --host 127.0.0.1 --port 8876
curl -fsS http://127.0.0.1:8876/
curl -fsS http://127.0.0.1:8876/api/status
curl -fsS 'http://127.0.0.1:8876/api/query?doc_id=doc-000000&budget_ms=50'
```

结果：

- 首页返回 `index.html`，标题为“语料去重诊断工作台”
- `/api/status` 返回 `initialized=true`、72 篇文档、36 个剔除、两个快照 v1
- `/api/query` 返回 `doc-000000`，3 条候选轨迹、`removed=true`、一致性三项匹配，实测 3.92 ms

## 端到端自检

命令：

```bash
python -m corpus_dedup_workbench selftest \
  --out artifacts/selftest_report.json
```

结果：`ok=true`，全部 6 个自检项通过。

```json
{
  "batch_has_docs": true,
  "all_queries_consistent": true,
  "query_within_budget": true,
  "cache_refresh_reused_all": true,
  "snapshot_versions_monotonic": true,
  "mode_difference_observable": true
}
```

## 静态检查

```bash
uvx ruff check src tests
```

结果：`All checks passed!`

## Windows 说明

本执行环境实际为 WSL2 Ubuntu。项目不使用容器、CUDA、外部服务或 Linux 专属依赖；Windows 原生 PowerShell 安装、虚拟环境和运行命令已写入 README。Windows 原生环境未在当前执行上下文中直接实测。

