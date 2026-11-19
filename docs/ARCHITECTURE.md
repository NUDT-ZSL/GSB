# Architecture and Consistency Model

## Module Map

| Module | Responsibility |
| --- | --- |
| `synth.py` | 固定种子音频、真值、对话清单、注册清单和注册追加 |
| `dataset.py` | 清单校验、WAV/PCM 加载、真值校验、数据指纹 |
| `features.py` | 精确分帧、能量/过零率、频谱描述量、确定性说话人距离 |
| `embeddings.py` | 注册片段合成、嵌入统计、版本化缓存及缓存校验 |
| `pipeline.py` | 四个纯阶段计算和资源估算 |
| `regression.py` | 连续段级情感回归、置信区间 |
| `aggregation.py` | 段时长加权、排序、独立重算 |
| `snapshot.py` | 原子清单、内容寻址快照、提交锁、回退与整体归档 |
| `engine.py` | 恢复编排、阶段复用、事件/批次进度 |
| `selfcheck.py` | 跨阶段与跨运行一致性检查 |
| `app.py` / `cli.py` | 交互应用与控制台入口 |
| `demo.py` | 真实子进程强杀、重启和逐字节恢复比较 |

## State Machine

```text
framing -> segmentation -> regression -> aggregation
   |            |              |              |
 commit        commit         commit         commit
 full set      full set       full set       full set
```

阶段开始前检查对话数和内存估算；任何超限都不会产生快照。阶段内部可以按 `batch_size` 报告批次进度，但只有在全部对话完成后才调用 `commit_stage`。

恢复算法只查看 `manifest.json` 中的连续已提交前缀：

```text
manifest generation = 0      -> restart at framing
manifest [framing]           -> restart at segmentation
manifest [framing, seg]      -> restart at regression
manifest [framing, seg, reg] -> restart at aggregation
manifest [all four]          -> reuse committed report, compute nothing
```

未提交阶段没有可加载的权威快照。即使进程曾写出临时文件，清单也不会引用它，因此不会被误认为完成结果。

## Atomic Commit Protocol

1. 使用 `mkdir(state/.commit.lock)` 获取进程间独占锁；目录已存在立即返回 `E_STAGE_BUSY`。死进程遗留锁通过 PID 检测后原子改名清理。
2. 读取并校验 `manifest.json`，确认分析上下文、顺序、版本和回退约束。
3. 生成 payload 校验和、快照校验和和全局下一版本号。
4. 写临时快照文件、`fsync`，再 `os.replace` 到内容寻址文件名。
5. 构造新清单，原子替换 `manifest.json`。
6. 释放锁。

若步骤 4 后、步骤 5 前崩溃，清单仍是旧版本，只引用旧快照。若步骤 5 完成后崩溃，新清单已经完整可见。

## Versioning and Registry Evolution

分析上下文键包含：

- 引擎 schema；
- 分析配置；
- 对话清单、音频、真值、注册清单的数据指纹；
- 注册版本与校验和；
- 回归模型校验和。

注册样本、逐说话人嵌入统计、嵌入缓存、阶段快照均带版本与内容校验和。注册样本追加会提高 `registry_version` 并改变分析上下文键。下一次打开状态目录时：

1. 旧清单和快照整体移动到 `archive/epoch-.../`；
2. 全局 generation 继续保留并递增；
3. 所有阶段重新从头提交，避免增量混用旧嵌入；
4. 新注册版本写入新的缓存文件，旧缓存不再被引用。

注册版本与分析上下文不变时，已提交四阶段直接复用，不重新分帧、分段、回归或计分。

## Segmentation

每个注册片段由 `speaker_id + clip_seed + clip_ms` 确定性合成，并按 20 ms 帧生成八维描述量。每个说话人的均值、标准差和帧数作为嵌入统计量。

逐帧距离使用标准化平方欧氏距离。说话人列表按 `speaker_id` 升序迭代，仅当新距离严格更小时替换，因此精确同分由最小 `speaker_id` 获胜。静音帧延续上一指派，最终由相邻相同标签合并为连续段并生成切换时间轴。

真值说话人集合必须在注册控制集合内；否则错误携带 `dialog_id` 和零基 `frame_index`。

## Regression and Aggregation

回归阶段按连续段读取声学描述量，使用固定确定性线性系数输出逐帧分数，再汇总为段级均值和 95% 预测区间。区间口径：

```text
z * sqrt(model_sigma^2 + sample_variance / frame_count)
```

对话聚合权重口径显式写为 `segment_duration_ms`：

```text
weighted_score = sum(duration_ms * segment_score) / sum(duration_ms)
```

分段明细同时保留 `duration_ms` 和 `aggregation_weight_ms`。自检从这些明细独立重算并核对存储的对话分数。排序键为：

```text
arousal desc, valence desc, dialog_id asc
```

报告哈希基于删除自身 `report_hash` 后的规范化对象计算，因此可重复校验且不会自引用。

## Resource Limits

`AnalysisConfig` 提供：

- `max_dialogs_per_stage`；
- `max_resident_feature_bytes`；
- `max_segment_memory_bytes`；
- `batch_size`。

超限错误包含 `stage`、`degradation_category`、`limit`/`observed` 或对应字节数，便于调用方降级或拆批。

## Windows Notes

- 提交锁使用目录创建，不依赖 POSIX `fcntl`。
- 原子替换使用 `os.replace`。
- 过期的 PID 通过 Win32 `OpenProcess` 检查。
- 安装后提供 `voice-emotion.exe`；仓库内提供 `voice-emotion.bat`。
- 所有路径使用 `pathlib.Path`。

