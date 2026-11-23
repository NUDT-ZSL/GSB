# 实际验证记录

## 环境

```text
Python 3.12.16
numpy 1.26.4
```

系统原有 Python 3.10.12 也成功执行过同一测试集；正式验收使用 `.venv` 中的 3.12.16。Python 3.10 与 3.12 重新生成样本后，`sessions.jsonl`、`vocabulary_v1.json`、`params_v1.json` 的 SHA-256 均未变化。

## 自动化测试

命令：

```bash
.venv/bin/python -m unittest discover -v
```

结果：`Ran 21 tests ... OK`。

覆盖：

- 分块边界与重复运行字节一致；
- 词频平局排序；
- 打分 contribution 独立重算；
- 非正权重业务码；
- 块合并与一次性整段合并一致；
- 人工篡改导致 `E_BLOCK_MISMATCH` 并给出首个差异位置；
- 缓存 69 节点全复用；
- 缓存键字段与内容哈希审计；
- 追加、强度、词表三类增量；
- 阶段中断恢复；
- 快照回滚、损坏、并发忙；
- 版本号逆序加载识别为快照回退；
- 配置、重复会话、词表缺失、资源上限与降级；
- 会话增删和 CLI 无效配置拒绝；
- 真实子进程强杀恢复演示。

## 全量结果

样本 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `sessions.jsonl` | `69e4c7597b1c3e8e5daacefa3630d7d20f019d139a94cea01ccf58aa81aa9373` |
| `vocabulary_v1.json` | `3d595f611b70d2d642ce0d48b9f57e87d7e63c88317d75392367765d078c936a` |
| `vocabulary_v2.json` | `90b06a8d93395336823f060e6512a299c7b70d29b1a2f222b40c005fc57a6498` |
| `params_v1.json` | `cba784c63f848594f70f55a229f4370f7b120cbc697b82dbe7338fdc05e2c4bb` |

全量摘要指纹：

```text
8975320d9f4b98a7ed7cf70aacc8e5360051f6cc97b5016d5a0d9873d766bfa6
```

- 首次运行：69 miss / 0 hit。
- 二次运行：0 miss / 69 hit。
- 两次摘要逐字节一致。
- 四项自检全部通过。

## 增量失效

| 场景 | 重算节点 | 复用节点 | 分块/打分/改写/合并重算数 | 与零缓存全量一致 |
|---|---:|---:|---|---|
| 追加 S001 两轮 | 6 | 68 | 1 / 1 / 2 / 2 | 是 |
| 强度 0.35 -> 0.65 | 45 | 24 | 0 / 0 / 29 / 16 | 是 |
| 词表 v1 -> v2 | 69 | 0 | 12 / 12 / 29 / 16 | 是 |

三个场景的重算集合与复用集合均无交集，且并集覆盖当前全部节点。增量结果与独立临时缓存中的零复用全量结果逐字节一致。

## 恢复演示

命令：

```bash
.venv/bin/python -m support_summary recovery-demo --root .
```

结果：

- 子进程在 `rewrite:partial` 后以退出码 `97` 强制终止；
- 强杀后已提交快照：`01_chunk_tokenize.json`、`02_unit_score.json`；
- 恢复阶段：`chunk_tokenize`、`unit_score` 从快照复用；
- 重建阶段：`rewrite`、`summary_merge`；
- 快照版本依次为 1、2、3、4；
- 恢复摘要指纹与完整运行相同：`8975320d...766bfa6`；
- 恢复摘要与完整运行摘要逐字节一致。

## 异常业务码

`scripts/verify_all.py` 实际捕获：

| 场景 | 业务码 | 定位字段 |
|---|---|---|
| 精度档非法 | `E_PRECISION_INVALID` | `precision` |
| 块大小非正 | `E_BLOCK_SIZE_INVALID` | `block_size` |
| 改写强度越界 | `E_STRENGTH_INVALID` | `rewrite_strength` |
| 权重非正 | `E_WEIGHT_INVALID` | `weights.coverage` |
| 重复会话 | `E_DUPLICATE_SESSION` | `session_id=S001` |
| 词表缺失 | `E_VOCAB_MISSING` | `S001 / turns[6].text` |
| 块合并不一致 | `E_BLOCK_MISMATCH` | `S001 / position 511` |
| 会话数超限 | `E_RESOURCE_LIMIT` | `max_sessions_per_stage` |
| 驻留词频超限 | `E_RESOURCE_LIMIT` | `max_resident_frequency` |
| 中间内存超限 | `E_RESOURCE_LIMIT` | `max_intermediate_memory_bytes` |
| 快照回退 | `E_SNAPSHOT_ROLLBACK` | `chunk_tokenize / version 1` |
| 快照损坏 | `E_SNAPSHOT_CORRUPT` | `chunk_tokenize / version 1` |
| 并发阶段提交 | `E_STAGE_BUSY` | `chunk_tokenize` |

降级模式标记：

```text
session_count
resident_frequency
intermediate_memory
```

## 复现

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python scripts/verify_all.py
```

机器可读完整记录位于 `artifacts/verification.json`。
