# 说话人识别与分离交互工作台 — 技术说明

## 1. 目标与约束

面向本地确定性合成 WAV（16 kHz / 单声道 / 16-bit PCM），完成四阶段管线：

1. 逐帧声纹特征抽取（能量对数、过零率、谱平坦度、量化谱形状声纹、帧指纹）
2. 双门限迟滞语音活动切分（VAD）、最短语音/静音合并、疑似重叠标注
3. 确定性凝聚聚类（欧氏距离、同分按簇内最小帧序号决断）
4. 区间归属判定（最近簇、距离同分按簇标识字典序决断、并发 + 异常隔离）

所有计算无随机性：固定种子合成、固定量化网格、确定性排序键、规范化 JSON 哈希。
不访问网络、GPU 或外部服务；线程仅用于满足并发语义，GIL 下结果与单线程逐字段一致。

## 2. 目录结构

```
configs/                 配置 JSON（两种量化档位、两种聚类阈值）
commands/edits.jsonl     四类编辑命令样例流
commands/anomalies.jsonl 异常注入命令流
data/generate_samples.py 固定种子合成器（字节级可复现）
data/audio/              3 个合规 WAV + manifest
data/bad_audio/          3 个坏样本（8kHz / 立体声 / 截断）
src/voiceworkbench/      实现包
tests/                   pytest 套件（41 用例）
artifacts/               诊断报告、串行基线、验证矩阵
snapshots/               阶段快照、材料 npz、阶段缓存、最新摘要
scripts_verify.py        端到端验收脚本
```

## 3. 确定性设计

- 规范化 JSON：`sort_keys=True`、紧凑分隔符、`allow_nan=False`，SHA-256 指纹。
- 声纹：每帧 rFFT 幅度谱（去 DC）→ 16 个等宽对数频带 → 逐帧 z-score →
  固定对称网格（[-2, 2]）量化为 4 或 8 档整数码。逐帧 min/max 无关、无 RNG。
- 帧指纹：对（帧序号、四舍五入特征、整数声纹）做短哈希；同音频任意运行逐帧一致。
- 矩阵化实现与逐帧标量实现的等价性由 `test_vectorized_matches_scalar` 守护。
- 排序键：帧 `[file_name, frame_index]`，区间 `[file_name, start_frame, segment_id]`，
  线程完成顺序不影响最终顺序；失败清单按同一排序键稳定排列。

## 4. VAD 与重叠标注

- 进入门限：能量 ≥ `energy_db_floor` 且平坦度 < `flatness_high`。
- 保持门限（迟滞）：已激活时要求平坦度 < `flatness_low`（更严）才维持语音。
- 最短静音间隔并入相邻语音；短于最短语音的片段并入相邻静音；结果按起始帧升序。
- 疑似重叠：同文件相邻语音段，间隔短于 `1.5 × min_silence_frames`
  且声纹欧氏距离 ≤ `overlap_distance_gate`，双向标记并记录观测距离与门限值。

## 5. 聚类、归属与结构编辑

- 距离：量化声纹均值向量上的欧氏距离（余弦在近同向整数码上区分度不足）。
- 凝聚：complete-linkage，超阈值不合并；距离同分按簇内最小（文件, 帧）决断。
- 簇标识：按最早成员帧重排为规范 `SPK-xx`；成员按稳定键排序。
- 归属：最近质心，距离同分按标签字典序；得分 = 1 − 距离/码空间跨度，裁剪到 [0,1]。
- 合并：两簇并集后重算质心；拆分：确定性最远对为种子，其余按距离（同分按稳定键）分配。
- 覆盖链按命令顺序在“当前簇集”上逐步解析标签，增量与空白串行重放完全同构。

## 6. 内容指纹缓存与增量失效

每阶段缓存键 = `derived_key(上游键集合, 参数指纹)`：

| 阶段 | 上游 | 参数指纹内容 |
| --- | --- | --- |
| features | 材料指纹 | 帧长/帧移、维度、量化档、采样率 |
| vad | features 键 | 能量/平坦度门限、重叠门限、最短段、帧移 |
| clustering(base) | vad 键 | 有效聚类阈值 |
| clustering(override) | base 键 | 合并/拆分覆盖链 |
| assignment | clustering 键 | 并发数、覆盖指纹 |

失效规则（仅影响阶段及其下游，复用清单与失效清单均写入响应）：

- 改聚类阈值 → clustering + assignment
- 裁区间 / 替换音频 → features + vad + clustering + assignment
- 合并 / 拆分 → clustering(override) + assignment（base 凝聚结果仍可复用）

## 7. 交互延迟预算语义

- 预算只影响摘要返回时机，不影响最终计算内容。
- 预算检查点在各阶段计算前；超时抛 `E_INTERACT_BUDGET_PENDING`，
  引擎随即在同一命令处理中完成剩余计算（“后台继续”），再原子替换摘要版本。
- 该标记只表示“未在预算内完成”，从不声称后台计算被中止；无任务丢弃。
- 摘要文件 `summary-latest.json` 带 `in_budget` 标志与单调 `summary_version`。

## 8. 快照、恢复与退出

- 所有持久化均为“同目录临时文件 + `os.replace` 原子替换”（POSIX/Windows 均原子）。
- `snapshot.json`：阶段名、配置哈希、命令游标、已完成区间、产物版本、材料指纹，
  并带自校验 `snapshot_hash`；损坏或缺字段时回退干净引导。
- `material.npz`：材料化音频（全精度 float64）；`material-ops.json`：编辑操作溯源；
  `stage-cache.json`：阶段缓存。
- 可处理退出信号（SIGINT/SIGTERM）：完成当前命令的快照提交后以退出码 0 退出。
- 强制终止后仅信任完整替换的快照；恢复后跳过 `seq ≤ 游标` 的命令继续执行，
  结果与不中断运行逐字段一致（见 `test_sigterm_graceful_exit_then_resume`）。

## 9. 异常隔离

- 音频容器校验失败 → `E_AUDIO_FORMAT`，进入失败清单且不阻断其他文件。
- 单帧 / 单区间异常 → 记录标识、阶段名、异常类型、错误码、消息、排序键后继续。
- 命令格式错误 → 整行拒绝（不推进游标）；运行期错误（未知文件/标签等）→
  记录到 `rejected_commands`，当前状态保持一致。

## 10. 验证结果（实际运行）

- `pytest`：41 passed。
- `scripts_verify.py`：`overall_ok = true`
  - 量化 4/8 档 × 聚类阈值紧/松 共 4 组，增量与串行全量在
    `stage_keys / clusters / assignments / segments / rejected_commands` 全部相等。
  - 子进程在 seq=3 后退出（退出码 0），新进程恢复续跑，最终与不中断基线相等。
  - 预算挂起序列为音频编辑命令（2/5/7），均带 `E_INTERACT_BUDGET_PENDING`，
    最终归属数 = 区间数，无丢失。
