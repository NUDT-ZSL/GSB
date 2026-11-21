# 验证记录

## 环境

- Python：3.10.12
- NumPy：1.26.4
- pytest：8.3.3
- 平台：Linux 6.18 / WSL2（代码无平台专用路径，适用于 Windows Python 3.10+）
- 容器：未使用

## 自动化测试

执行：

```text
pytest -q
```

实际结果：

```text
36 passed in 1.69s
```

覆盖范围：

- 16 kHz、帧长、帧移、重复 clip、缺失 segment_id 的配置/清单错误。
- 样本生成两次逐字节一致。
- 包络重复计算一致、帧数精确、帧覆盖无遗漏。
- 帧数不匹配返回 `E_FRAME_MISMATCH` 并带 clip_id。
- 包络帧数上限与快照条目上限返回 `E_RESOURCE_LIMIT`。
- VAD 时间有序、范围内、非重叠；`E_OVERLAP`/`E_OUT_OF_RANGE` 带区间标识。
- 精修幅度不超过搜索窗口。
- 未知标签与非法置信区间业务码。
- 同分标签字典序裁决。
- 首跑全部重算、重复跑全部复用且版本不增长。
- 标注修正只重算 `speaker/export`，4/5 区间复用。
- 分位变化复用 envelope、重算 VAD 后缀。
- 帧移变化全链重算。
- 快照内容损坏返回 `E_CACHE_CORRUPT`，追溯文件缺失返回 `E_LINEAGE_BROKEN`。
- 快照/配置回退返回 `E_SNAPSHOT_ROLLBACK`。
- 快照代次不可变、每代条目完整、旧版本仍可回放。
- 回放逐项复现起止毫秒、归属、置信区间。
- 标注与运行期配置跨进程重启持久化。
- 精修吞并邻区间返回 `E_REFINE_INVALID`，增量与全量差异返回 `E_INCONSISTENT`。
- 不存在的回放缓存键返回 `E_LINEAGE_BROKEN`。
- 交互状态渲染与错误码保留。

## 验收脚本

执行：

```text
python scripts/run_acceptance.py
```

结果文件：`runtime/acceptance_report.json`。

### 增量结果

| 步骤 | 版本 | 重算节点 | 复用节点 | 受影响区间数 | 与全量重算逐字节一致 |
| --- | ---: | --- | --- | ---: | --- |
| 首次分析 | 1 | envelope, vad, refine, speaker, export | - | 0 | 是 |
| 重复分析 | 1 | - | envelope, vad, refine, speaker, export | 0 | 是 |
| 标注修正 `a-002 -> S01` | 2 | speaker, export | envelope, vad, refine | 1 | 是 |
| 底噪分位 `0.20 -> 0.25` | 3 | vad, refine, speaker, export | envelope | 2 | 是 |
| 帧移 `10ms -> 5ms` | 4 | envelope, vad, refine, speaker, export | - | 2 | 是 |

标注修正步骤统计：`interval_recompute_count=1`、`interval_reuse_count=4`。

### 缓存键与输出哈希

| 版本 | 导出缓存键 | JSONL SHA-256 |
| --- | --- | --- |
| 1 | `8a2c76ce0ae1f10472b202417fbd8afc8362ad650ed2da65865d32d4f061c45c` | `21c5e19fd61bd31fb8a4eb1261c814bba96991c7d5a97d038927d3cbc557496d` |
| 2 | `9caf127019b16ee34a2d57054d98f02512f561375a4f2dedf1ff99ac7ee9296b` | `5db8860cdfa5586c4f794194b392af5be1c683abcd45e91746afd4b058874094` |
| 3 | `f3bf983f51fa45617e876355b82a968a7b33fdba065f3b0c7f18b4c0efaba4d1` | `5db8860cdfa5586c4f794194b392af5be1c683abcd45e91746afd4b058874094` |
| 4 | `ff7a20105ef793c5a720d090df2bfbb5cc7f4c5b35679bd74157c26c4329043f` | `bdf9ea196abd9b51fd0e2cb97120eebfaadf9ee097da1f0f4a6bbdf4c9ecbddb` |

版本 2 与版本 3 的导出行内容相同，但底噪分位和门限版本已变化，因此缓存键不同，符合“输入版本变化即新键”的约定。

### 回放与版本审计

- 初始、标注修正、分位调整、帧移调整四个版本的导出均在后续版本提交后回放成功。
- 每个回放记录均重新校验五节点 lineage 和上游哈希，并从 speaker 快照重新导出；逐字节与逐项一致。
- 快照历史：`[1, 2, 3, 4]`。
- 当前代次条目数：5。
- 回退请求返回：`E_SNAPSHOT_ROLLBACK`。
- 旧代次目录保持不变，新代次通过整体 staging + 原子替换写入。

### 交互应用冒烟

执行 `python -m meeting_vad --check` 后，应用从运行期配置恢复 `frame_hop_ms=5.0`、`noise_quantile=0.25`，五节点全部命中缓存，重复分析快照版本保持 4。交互命令 `status`、`replays`、`replay` 均正常输出。
