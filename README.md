# 多路召回融合本地数据处理管线

面向搜索/推荐前链路的四路召回融合管线，完全基于固定种子的本地确定性数据，
不依赖网络或任何外部服务。覆盖召回、对齐去重、归一化加权融合、进程池并行
调度、三类预算准入、驻留内存淘汰、冲突仲裁与版本化快照。

## 环境准备

- Python 3.11+（已在 3.11.18 验证）
- 唯一第三方依赖：`numpy`

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Windows 下进程池使用 `spawn` 启动方式，所有可执行入口均带
`if __name__ == "__main__"` 保护，worker 入口 `recall_fusion/worker.py`
是模块级顶层函数，可被 pickle。

## 快速开始

```bash
# 1. 生成确定性样本与默认配置
.venv/bin/python -m recall_fusion.cli generate

# 2. 单进程运行（调试友好）
.venv/bin/python -m recall_fusion.cli run --in-process --out-dir artifacts/run

# 3. 本地进程池并行运行（默认 workers=2, batch_size=4）
.venv/bin/python -m recall_fusion.cli run --out-dir artifacts/run_pool

# 4. 端到端验收（可重算/预算/内存/并行/快照）
.venv/bin/python scripts/validate.py

# 5. Windows spawn 启动方式兼容性验证
.venv/bin/python scripts/check_spawn.py

# 6. 单元测试（31 个）
.venv/bin/python -m unittest discover -s tests -v
```

## 项目结构

```
recall_fusion/
  errors.py       结构化业务错误码（E_*）与 PipelineError
  models.py       Document/Query/Candidate/MergedCandidate/FusedCandidate 等
  config.py       四路通道、三类预算、调度与冲突优先级配置
  sample_data.py  固定种子样本生成与 JSONL 读写
  recall.py       四路召回 + 共享内存索引（RecallIndex）
  merge.py        跨路 doc_id 对齐去重（两种口径）
  fusion.py       归一化（scale / rank_reciprocal）与加权融合
  budget.py       代价估算、双预算准入、收缩/两段式仲裁
  memory.py       驻留字节模型与 shard-LRU 淘汰
  snapshot.py     版本号 + SHA-256 校验和快照、整体替换
  worker.py       可 pickle 的进程池顶层入口
  scheduler.py    确定性分块、并行调度、内存接入、报告聚合
  pipeline.py     端到端门面与产物落盘
  cli.py          命令行入口
tests/            覆盖八项业务要求的单元测试
scripts/          validate.py 验收脚本、check_spawn.py spawn 验证
config/           融合配置 JSON
data/             确定性文档与查询 JSONL
artifacts/        运行报告、逐查询结果与快照
```

## 业务要求与实现映射

1. **四路确定性召回**（`recall.py`）：倒排 `Σ tf/doc_len`、前缀按前缀长度
   加权、类别热门按 `quality_score`、向量按 L2 归一化后的余弦；统一
   “原始分降序、同分 doc_id 升序”，固定种子与排序保证逐字节可复现。
2. **对齐去重**（`merge.py`）：按 `doc_id` 归并，保留全部命中路名与原始分；
   `highest_normalized` 选最高原始分命中、`first_hit` 选最早通道命中；非法
   口径返回 `E_MERGE_POLICY`（含 query_id 与 field）。
3. **归一化与融合**（`fusion.py`）：min-max 缩放与排名倒数
   `1/(k+rank)`；逐候选记录每路原始分、归一化分、权重与加权贡献，
   `final_score = Σ weighted_contribution` 可由明细独立重算；非法归一化
   `E_NORM_INVALID`、负权重 `E_WEIGHT_INVALID`。
4. **并行调度**（`scheduler.py`/`worker.py`）：查询按 query_id 全局排序后
   确定性分块，派发与归并顺序固定；worker 内异常封装为带 code/stage/
   batch_id/query_id 的结构化错误，单查询失败不影响同批其他查询。
5. **双预算准入**（`budget.py`）：按过滤后文档数与各路 cap 估算召回/融合
   代价，超限时先按步长收缩候选规模（牺牲精度），仍超限再按优先级返回
   `E_RECALL_BUDGET`/`E_FUSION_BUDGET`，附剩余预算；被拒查询不进入已完成集合。
6. **驻留内存与淘汰**（`memory.py`）：确定性字节模型统计候选数组与融合中间表，
   超限时按 shard-LRU 淘汰最老已归并查询，仅释放大数组、保留 fused 明细与
   计数；报告记录触发点、释放量、保留量；单分片超过上限返回
   `E_RESOURCE_LIMIT`。淘汰后结果与无淘汰运行一致。
7. **冲突仲裁**（`budget.py`/`scheduler.py`）：扩批逼近内存→收缩批规模；
   两段式粗排+精排突破融合预算→回退单段；并行度推高峰值→按
   `conflict_priority` 决定降并行（牺牲延迟）或维持（牺牲内存）。每次仲裁
   记录依据、被牺牲指标与所选批规模，同观测下确定性一致。
8. **版本化快照**（`snapshot.py`）：召回统计、归一化参数、融合权重三类快照，
   各带单调版本号与 SHA-256 校验和，提交即整体替换；回退返回
   `E_SNAPSHOT_ROLLBACK`，校验和不符或文件损坏返回 `E_SNAPSHOT_CORRUPT`，
   错误中指明快照种类与版本号。

## 产物说明

- `scheduling_report.json`：批次布局、准入/拒绝清单、预算余额、淘汰事件、
  仲裁记录、worker 错误、驻留峰值、聚合统计、执行顺序。
- `query_results.jsonl`：每查询 top 候选、最终分、逐路明细与全部命中，
  用于独立重算与核对。
- `snapshots/*.json`：三类版本化快照（kind/version/checksum/payload）。
