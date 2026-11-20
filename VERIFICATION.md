# 实际验证记录

## 1. 环境

本次执行环境：

```text
OS:          Linux (Ubuntu 22.04 WSL)
Python:      3.10.12
NumPy:       1.26.4
pytest:      8.3.3
```

任务说明里的目标环境是 Windows + Python 3.11+。当前工作区实际提供的是
Python 3.10.12，因此本次在上述真实环境完成运行验证。代码未使用 3.11 专属
语法，`pyproject.toml` 声明 `>=3.10`；Windows/Python 3.11 可按 README 命令
直接安装和执行。

未使用 Docker、外部数据库、网络服务或真实音频附件。

## 2. 样本生成

执行：

```bash
python scripts/generate_samples.py --out data/samples
```

结果：

```text
status: ok
dialog_count: 3
registration_count: 6
sample_rate: 16000
```

生成文件：

```text
data/samples/dialogs.jsonl
data/samples/speakers.jsonl
data/samples/manifest.json
data/samples/pcm/dlg-001.pcm      96000 bytes
data/samples/pcm/dlg-002.pcm      76800 bytes
data/samples/pcm/dlg-003.pcm     115200 bytes
truth/dlg-001_20_10.jsonl          299 rows
truth/dlg-001_20_15.jsonl          199 rows
truth/dlg-002_20_10.jsonl          239 rows
truth/dlg-002_20_15.jsonl          159 rows
truth/dlg-003_20_10.jsonl          359 rows
truth/dlg-003_20_15.jsonl          239 rows
```

PCM 时长与字节数一致：`duration_ms * 16000 / 1000 * 2`。帧数满足
`1 + (samples - 320) // hop`，与对应真值 JSONL 行数一致。

PCM 内容哈希（由样本清单记录的规范化 PCM 计算）：

```text
dlg-001  7b6886c5bc5cff9fe25e62c5ba9720222a3af59b753aba18d8083319f6a5859e
dlg-002  678019144b98b5741ce4b63aaa243f284e3df16b0768512618b8edc27c18f46b
dlg-003  6179d917b60019e0bc4784c1dcb26e06a8dd178aec56c87912b37e8a062bf5d6
```

## 3. 自动化测试

执行：

```bash
pytest -q
```

结果：

```text
31 passed in 3.75s
```

覆盖点：

- 合成、写盘、重载的字节一致性；
- 帧边界、帧数/真值一致性、特征字节确定性；
- `E_FRAME_MISMATCH` 的实际帧数详情；
- 标签合法、段连续覆盖、短段合并；
- 区间顺序、独立聚合重算；
- identity/audit 键分离、条目版本、校验和损坏、资源上限；
- 并发同键提交返回 `E_STAGE_BUSY` 且不阻塞；
- 配置快照单调版本和整快照替换；
- 四类增量事件、复用范围、全量一致性；
- 旧段/对话分数重放、缺键追溯失败；
- `E_INCREMENTAL_DRIFT` 首个偏离节点；
- 采样率、帧参数、重复对话、缺失 speaker_id、候选比较上限等错误。

## 4. 完整演示

执行：

```bash
python scripts/demo.py \
  --samples data/samples \
  --work artifacts/demo \
  --json artifacts/demo-report.json
```

结果：`status = ok`，报告约 476 KiB，包含完整段分数、缓存键组件、失效报告、
追溯重放和业务码场景。

增量事件实际统计：

| 事件 | 复用节点 | 重算节点 | 受影响段数 | 自检 |
|---|---:|---:|---:|---|
| 追加 `spk_a` 注册样本 | 30 | 2 | 7 | 通过 |
| 修正 `dlg-002` 第 1 段真值 | 29 | 3 | 1 | 通过 |
| 帧移 10 ms -> 15 ms | 0 | 32 | 20 | 通过 |
| 切段阈值 200 ms -> 350 ms | 10 | 12 | 20 | 通过 |

说明：

- 注册追加只重算受影响对话的指派；若标签不变，边界、段和聚合继续命中。
- 单段修正只有该段和对话聚合重算。
- 帧移改变帧数和全部下游产物。
- 阈值变化保留帧特征和逐帧指派，只重算切段后缀。

最终段数：

```text
dlg-001: 4
dlg-002: 3
dlg-003: 3
```

最终对话聚合：

```text
dlg-001  valence=0.488392763541  arousal=0.530678506804
dlg-002  valence=0.355266453221  arousal=0.543779146350
dlg-003  valence=0.482535725620  arousal=0.545552816350
```

## 5. 增量与全量一致性

演示在最终输入/配置/真值修正状态下：

1. 使用已有增量缓存得到最终结果；
2. 新建独立缓存目录和独立引擎；
3. 从零全量运行；
4. 按 DAG 拓扑顺序比较节点内容哈希。

结果：

```text
matched: true
node_count: 22
first_divergent_node: null
```

全部 22 个节点的增量和全量内容哈希一致，完整 `segments` 和
`dialog_aggregates` 也一致。

## 6. 旧分数追溯

演示在基线运行后保存：

- 一个 `dlg-002` 段级记录；
- 一个 `dlg-002` 对话级记录。

随后依次追加注册、修正真值、改变帧移和切段阈值，再查询旧记录：

- 段级重放从旧帧特征和旧真值快照重算，valence/arousal 与原记录相同；
- 对话级重放从旧段缓存键和权重重算，valence/arousal/总权重与原记录相同；
- 节点路径、每层缓存键、注册/真值/配置版本均可导出。

报告位置：

```text
artifacts/demo-report.json -> lineage
```

手动删除某一层缓存条目后的缺键测试也在 pytest 中覆盖，返回
`E_LINEAGE_BROKEN` 且 `missing_key_level = segment_regression`。

## 7. 业务码实测

`artifacts/demo-report.json -> business_codes` 共记录 14 个场景：

```text
E_SAMPLE_RATE
E_FRAME_INVALID
E_FRAME_SHIFT_INVALID
E_DUPLICATE_DIALOG
E_SPEAKER_MISSING
E_INTERVAL_INVALID
E_FRAME_MISMATCH
E_SPEAKER_UNKNOWN
E_SEGMENT_GAP
E_RESOURCE_LIMIT
E_CACHE_CORRUPT
E_STAGE_BUSY
E_LINEAGE_BROKEN
E_INCREMENTAL_DRIFT
```

所有错误均以 `{"code","message","details"}` 结构化输出。

## 8. 需求追踪

| 业务要求 | 实现/验证 |
|---|---|
| 帧级特征与帧数校验 | `features.py`、`test_samples_and_features.py` |
| 说话人嵌入、指派、歧义规则 | `speaker.py`，排序遍历 + 严格大于 |
| 标签/段覆盖校验 | `validate_assignment_labels()`、`validate_segments()` |
| 段回归、置信区间、加权聚合 | `scoring.py`，独立重算测试 |
| 内容寻址与完整键 | `cache.py`、`pipeline.py`，键组件可导出 |
| 四类增量失效 | `pipeline.py` 四个 change 类型及增量测试 |
| 增量/全量逐字节一致 | `compare_runs()`、演示独立全量缓存 |
| 旧分数追溯 | `lineage.py`，段/对话重放与缺键错误 |
| 版本、校验和、整体替换 | `ConfigSnapshotStore`、缓存 checksum |
| 并发提交 | `O_EXCL` 锁，线程测试返回 `E_STAGE_BUSY` |
| 输入校验与资源上限 | `config.py`、`features.py`、`speaker.py` |
| 自检 | 报告中的 `self_check`，示例每轮均为 true |
| CLI JSON 报告 | `scripts/demo.py`、`artifacts/demo-report.json` |

## 9. 剩余限制

- 本次不是 Windows 实机验证，原因是当前执行环境为 Linux/Python 3.10.12；
  代码与依赖声明兼容 Python 3.11，README 给出了 Windows 命令。
- 本地文件锁不覆盖跨主机共享文件系统。
- 生成器与回归器用于可控验收，不代表真实语音生产精度。
