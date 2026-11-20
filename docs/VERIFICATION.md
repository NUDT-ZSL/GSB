# 实际验证记录

验证日期：2026-11-21  
环境：Linux，Python 3.11.15，NumPy 2.4.6，pytest 9.2.0，无 GPU、无 Docker、无外部数据。

完整机器可读结果：

- `artifacts/verification_summary.json`
- `artifacts/selfcheck.json`
- `artifacts/decode_report.json`
- `artifacts/invalidation_*.json`
- `artifacts/pytest.txt`
- `artifacts/pool_check.json`

## 复现

```bash
./scripts/verify.sh
```

## 测试结果

```text
........................................                                 [100%]
```

共 40 项测试通过，覆盖：

- 块大小 `1, 2, 3, 4, 6, 20` 下分块与整段 token、置信度、投影、step 记录完全一致；
- 重复整段解码语义字节一致；
- 同分按目标 token 字典序裁决；
- 首块实际计时、预算超限和完成阶段/step 数；
- 人工注入漂移时 `E_CHUNK_MISMATCH` 的首个偏差 step/字段；
- 查询漂移时 `E_INCONSISTENT` 的块号/类别；
- 未对齐源词修改全部缓存复用且输出不变；
- 已对齐源词修改仅保留 boundary `<= t` 的前缀，实际候选打分次数严格等于后缀 step 数乘目标词表大小；
- 对齐版本和块大小变化全量失效；
- 快照单调版本、整体替换、回退拒绝、损坏检测；
- 缓存键/载荷/状态哈希损坏检测；
- 重复 case、未知源 token、非法对齐、非法块大小、上下文越界；
- 资源上限和块内存降级；降级前后 token/置信度/投影相同；
- 并发真实预览、计划提交 `E_STAGE_BUSY`、进程池和临时目录；
- CLI 整数退出码与 JSON 错误、HTTP 页面/API 与自检。

## 样本与一致性

固定样本：`local-seed-20261120`，源长度 14，目标长度 11，未对齐源位置 `[3, 7, 10]`。

```json
{
  "tokens_equal": true,
  "confidences_equal": true,
  "projections_equal": true
}
```

本机一次实跑：

| 项目 | 结果 |
|---|---:|
| 首块观测（chunk step loop） | 11.606654 ms |
| 首块估算 | 0.1869 ms |
| 整段观测（独立 whole path） | 32.728947 ms |
| 首块候选打分次数 | 96 |
| 整段候选打分次数 | 264 |
| 逐 step 前缀检查点 | 12 |
| 首块预算 | 250 ms，通过 |

语义摘要：

```text
semantic_hash       5383d2a838cfb09d64548e3a0077f5ba1c5cde9512199c8ba3cd5fa0477987b8
intermediate_digest 0cb3f58ad8140b3343f3ca288054b594e871afa83e6a2ae9021c53787a25dcc6
```

计时值随机器负载变化；语义哈希、中间量摘要和 token/置信度/投影不依赖计时。

## 自检

```json
{
  "whole_decode_byte_stable": true,
  "chunk_matches_whole": true,
  "cache_keys_and_hashes": true,
  "blocks_replay_from_cache": true,
  "invalidation_sets_and_reuse": true,
  "snapshot_versions_and_checksums": true
}
```

## 失效传播

目标长度 11，逐 step 缓存 12 条。

| 改动 | 首受影响 step | 复用 step | 重算 step | 复用条目 | 重算条目 |
|---|---:|---|---|---:|---:|
| 未对齐源词 | `null` | `0..10` | 无 | 12 | 0 |
| 已对齐源词（位置 5，目标 4） | 4 | `0..3` | `4..10` | 5 | 7 |
| 对齐版本 +1 | 0 | 无 | `0..10` | 0 | 12 |
| 块大小 4→3 | 0 | 无 | `0..10` | 0 | 12 |

四类报告的 `set_check` 均为：

```json
{"disjoint": true, "covers_all_steps": true}
```

## 异常码

测试实际触发并核对：

| 场景 | 业务码 |
|---|---|
| 重复 case | `E_DUP_CASE` |
| 源 token 不在词表 | `E_TOKEN_UNKNOWN` |
| 对齐引用不存在的目标词 | `E_ALIGN_INVALID` |
| 块大小 0 / 上下文 0 / 上下文 65 | `E_CONFIG_INVALID` |
| 首块 1 ns 预算 | `E_LATENCY_BUDGET` |
| 人工 chunk 漂移 | `E_CHUNK_MISMATCH` |
| 查询块漂移/版本变化/未就绪 | `E_INCONSISTENT` |
| 快照回退 | `E_SNAPSHOT_ROLLBACK` |
| 快照/缓存损坏 | `E_CACHE_CORRUPT` |
| 计划提交冲突 | `E_STAGE_BUSY` |
| 源长度/缓存条目超限 | `E_RESOURCE_LIMIT` |

## 进程池与本地 HTTP

进程池探针在两个本地 worker 中得到相同 SHA-256，临时目录读写回读一致。

HTTP 冒烟验证：`GET /` 返回 200 页面；`GET /api/status` 返回案例和计划；`POST /api/block` 返回首块；`POST /api/selfcheck` 返回 `ok:true`。

