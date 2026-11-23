# AED Studio — 本地确定性多说话人会议音频事件检测应用

面向多说话人会议音频的事件检测（AED）本地专业应用，核心解决**检测链路的增量更新与依赖失效**问题。
不依赖 GPU、Docker 或任何外部服务，样本完全本地确定性合成。

## 环境

- Python 3.11 + NumPy（已在 `.venv` 中验证：Python 3.11.18 / NumPy 2.4.6 / pytest 9.2.0）
- 安装：`python3.11 -m venv .venv && .venv/bin/pip install numpy pytest`

## 计算图（DAG）

```
features(帧级声学特征) -> candidates(候选检测与分类) -> segments(时间段后处理) -> metrics(帧级/事件级指标)
```

| 模块 | 职责 |
| --- | --- |
| `aed/synth.py` | 固定种子合成 16kHz 单声道 PCM、四类事件模板（knock/cough/keyboard/laugh）、逐帧真值（20ms）、会话清单 JSONL |
| `aed/features.py` | 节点1：精确样本索引切帧，短时能量/过零率/谱质心/谱滚降，float64 矩阵逐字节确定 |
| `aed/detect.py` | 节点2：模板原型余弦打分 + 能量门控，同分按类型编号升序、帧索引升序破平 |
| `aed/postprocess.py` | 节点3：按最短时长/最短间隔合并，输出毫秒级有序不重叠区间，可由候选明细独立重建 |
| `aed/metrics.py` | 节点4：帧级 P/R/F1 与事件级（包含式重叠匹配）指标聚合 |
| `aed/cache.py` | 内容寻址缓存（键含音频分块哈希、模板版本、帧移、最短时长/间隔、上游节点哈希）+ 版本化快照存储 |
| `aed/pipeline.py` | DAG 执行、缓存键构造、失效传播计划（重算/复用节点清单 + 受影响区间与事件集合） |
| `aed/trace.py` | 检测结论溯源记录（缓存键、模板版本、特征版本、节点路径）、重放与过期拒绝 |
| `aed/studio.py` | 工作区编排：数据集生成、全量运行、增量变更、快照提交 |
| `aed/app.py` | 本地可交互终端应用（拓扑、节点命中/重算状态、失效区间、事件明细、指标、增量演示） |

## 运行

```bash
.venv/bin/python -m aed.app workspace     # 交互式应用
.venv/bin/python run_demo.py              # 可重复端到端演示（含断言）
.venv/bin/python -m pytest tests/ -q      # 22 项验收测试
```

## 结构化业务码

| 业务码 | 触发条件 |
| --- | --- |
| `E_FRAME_MISMATCH` | 样本数与 duration_ms 不一致（指明 dialog_id） |
| `E_EVENT_TYPE_INVALID` | 事件类型超出受控集合 |
| `E_OVERLAP` / `E_OUT_OF_RANGE` | 后处理区间重叠/越界 |
| `E_STALE_RESULT` | 结论依赖版本已变化仍来自旧键（指出事件编号与旧版本） |
| `E_SNAPSHOT_ROLLBACK` / `E_CACHE_CORRUPT` / `E_STAGE_BUSY` | 快照回退 / 校验和不符 / 并发提交冲突（非阻塞） |
| `E_SAMPLE_RATE_INVALID` / `E_HOP_EXCEEDS_FRAME` / `E_DUPLICATE_DIALOG` / `E_TEMPLATE_VERSION_UNKNOWN` | 输入与配置校验链 |
| `E_RESOURCE_LIMIT` | 帧驻留内存 / 缓存条目 / 并行重算节点数上限 |

## 实际验证结果（本机实测）

- `pytest`：**22 passed**（特征/合成逐字节确定性、四类增量场景与整体重算逐字节一致、追溯重放一致、全部异常业务码、快照回退/损坏/并发冲突、资源上限）
- `run_demo.py`：首次全量重算 → 二次运行 4 节点全部 `hit`；模板升级 t1→t2 仅重算 `[candidates, segments, metrics]` 复用 `[features]`；真值修正仅重算 `[metrics]`，受影响区间 `[1140, 1160]ms`；最短时长调整仅重算 `[segments, metrics]`；三种场景均与全新缓存整体重算**逐字节一致**；末条结论重放一致

## 增量失效传播语义

| 变更 | 重算节点 | 复用节点 | 受影响区间 |
| --- | --- | --- | --- |
| 模板库升级 | candidates, segments, metrics | features | 全量区间 |
| 单条真值修正 | metrics | features, candidates, segments | 该事件帧区间 |
| 帧移调整 | 全部节点 | — | 全量区间 |
| 最短时长/间隔调整 | segments, metrics | features, candidates | 候选事件区间并集 |

快照（缓存条目、模板库、结果摘要）携带单调递增版本号与内容校验和，变更即整体替换；回退、损坏、并发冲突分别返回对应业务码。
