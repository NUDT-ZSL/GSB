# 验收记录

验证时间：2026-11-21（Asia/Shanghai）

## 环境

- Python `3.11.15`
- NumPy `2.4.6`
- CPU `32` 逻辑核
- 本地进程池与文件写入可用
- 无容器、无外部服务

```text
$ .venv/bin/python -c "import numpy; print(numpy.__version__)"
2.4.6
```

## 自动化结果

命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/verify.py
```

结果：

- 单元/集成测试：`22` 项全部通过；
- 验收脚本退出码：`0`；
- 固定样本：`20` 篇文档、`12` 条查询；
- 语言分区：`en`、`es`、`fr`、`zh`；
- 当前索引：`61` posting，解码大小 `17,347` bytes；
- 自检：`checked_postings=61`、`checked_contributions=81`、`ok=true`。

完整机器可读报告：

```text
artifacts/verification/verification_report.json
artifacts/verification/tests.log
artifacts/verification/demo/demo_report.json
```

## 三格式与迁移

样本三格式各 8 条规则，均可解析和稳定写出。

- v1→v2：补全 `8 × 3 = 24` 个默认项，全部记录 `schema_default:<field>` 来源；
- v1→v2→v1：v1 可表达字段逐字节一致；
- v2→v3→v2：v2 可表达字段逐字节一致；
- v3→v2：多目标规则展平，`any`/`min_freq` 条件丢弃并写入 `drops`；
- v3→v1：条件、可选字段和多余目标逐项写入丢弃明细；
- 所有迁移产物均通过立即回读。

## 展开与受影响分区

查询 `q-zh-car` 在规则 v3、桥接 v1 下：

- 中文“汽车”通过 `zh→en` 直接到达 `car`（桥接置信度 `0.96`）；
- 通过 `zh→en→fr` 到达 `voiture`（`0.96 × 0.89 = 0.8544`，深度 2）；
- 该结果高于 `zh→fr` 的直连置信度 `0.70`；
- 有效候选置信度继续乘规则权重 `0.95`；
- 候选按置信度降序与目标词项字典序稳定排序。

桥接版本差异：

```text
v1 -> v2: affected = ["en", "fr"]
v2 -> v3: affected = ["fr"]
v1 -> v3: affected = ["en", "fr"]
```

`es`、`zh` 不在差异目标语言中，因此不会被误判为受影响分区。样本提交：

| 桥接/索引版本 | affected | reused | recomputed |
| --- | --- | --- | --- |
| v1 / v1 | en, es, fr, zh | — | en, es, fr, zh |
| v2 / v2 | en, fr | es, zh | en, fr |
| v3 / v3 | fr | en, es, zh | fr |

## 打分与差谱

查询 `q-zh-car` 当前快照：

- 基线 top-3：`doc-zh-004=0.673685582683`、`doc-zh-005=0.654252344721`、`doc-zh-001=0.56233259381`；
- 改写 top-3：`doc-en-001=1.285841665182`、`doc-fr-002=1.260626833549`、`doc-fr-001=1.183192752863`；
- 候选数 `5`；
- 差谱行数 `9`；
- 每条结果分数由贡献明细按 `sum` 或 `max` 独立重算一致；
- 流式与驻留路径的基线、改写和差谱完全一致。

## 版本协同与故障恢复

强制终止流程：

1. 桥接 v1 / 索引 v1 完整提交；
2. 构建桥接 v2 / 索引 v2，在完成第 1 个新分区后 `os._exit(97)`；
3. 终止后 `CURRENT` 仍指向索引 v1；
4. 改用桥接 v3 恢复，索引 v2 完成提交；
5. `v2→v3` 只涉及 `fr`，因此复用 `en`、`es`、`zh`，重算 `fr`；
6. 与不间断 v1→v3 完整运行比较。

恢复与完整运行：

```text
index_checksum_equal = true
index_checksum        = 2c48ddadca48dd3476b8314b1906075e9c44b832b7c982d9e04d3876037baa10
queries_compared      = 12
queries_byte_equal    = 12
```

恢复提交：

```text
affected_partitions = ["en", "fr"]
reused_partitions   = ["en", "es", "zh"]
recomputed_partitions = ["fr"]
```

不间断完整运行：

```text
affected_partitions = ["en", "fr"]
reused_partitions   = ["es", "zh"]
recomputed_partitions = ["en", "fr"]
```

复用与重算无交集，并集覆盖受影响分区；恢复结果与完整运行逐字节一致。

## 异常码探针

`scripts/verify.py` 实际触发并核对：

| 场景 | 错误码 |
| --- | --- |
| 规则版本头 `v9` | `E_VERSION_UNSUPPORTED` |
| v1 行字段数不足 | `E_FORMAT_CORRUPT` |
| 查询 posting 上限设为 1 | `E_RESOURCE_LIMIT` |
| 活动桥接 v3，请求桥接 v1 | `E_VERSION_SKEW` |
| 篡改索引分区后自检 | `E_INCONSISTENT` |

五类探针均返回预期码。

## 资源降级

已覆盖：

- `max_parallel_partitions=1, parallel_policy=degrade` → `DEGRADED_LIMITED_PARALLELISM`；
- `parallel_policy=error` → `E_RESOURCE_LIMIT`；
- `max_postings=5` 构建 → `E_RESOURCE_LIMIT`；
- `max_resident_bytes=1` 查询 → `DEGRADED_STREAMING_PARTITION_SCORING`；
- 流式与驻留路径分数/贡献/差谱相同。
