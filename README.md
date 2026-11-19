# Voice Emotion State Lab

面向本地确定性对话语音的领域分析与状态恢复应用。项目只依赖 Python 标准库和固定版本的 NumPy，不访问网络，也不需要 GPU、外部服务或容器。

应用将分析建模为四个严格顺序的阶段：

1. `framing`：16 kHz PCM 精确样本分帧，计算短时能量与过零率。
2. `segmentation`：合成注册片段、缓存说话人嵌入统计量，按确定性相似度逐帧指派说话人。
3. `regression`：按连续说话人段回归 `valence` / `arousal`，输出 95% 置信区间。
4. `aggregation`：按段时长加权聚合为对话分数，生成确定性排序和报告。

核心保证是：阶段只在全部对话处理完成后提交；提交是原子可见的；进程可在任意批次间被强制终止；重启只复用已提交阶段，未提交的中间状态全部丢弃；注册版本变化时整体替换旧分析状态。

## Environment

固定运行时：

```text
Python 3.11
numpy==1.26.4
```

Linux/macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

Windows:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .
.venv\Scripts\voice-emotion.exe app
```

安装后会生成跨平台的 `voice-emotion` 控制台入口。Windows 也可直接运行仓库根目录的 `voice-emotion.bat`。

## Quick Start

```bash
# 1. 生成本地确定性样本（WAV、逐帧真值、对话清单、注册清单）
voice-emotion --workdir ./voice_state_data generate --dialogs 5 --speakers 3

# 2. 完整执行四阶段
voice-emotion --workdir ./voice_state_data --json run

# 3. 查看状态、阶段版本、对话计数和最近错误码
voice-emotion --workdir ./voice_state_data status

# 4. 导出某一对话的逐帧特征 JSONL
voice-emotion --workdir ./voice_state_data features \
  --dialog-id dlg_0001 --output dlg_0001.features.jsonl

# 5. 跨模块一致性自检
voice-emotion --workdir ./voice_state_data --json selfcheck

# 6. 强杀/恢复演示
voice-emotion --workdir ./voice_state_data --json demo --stop-after segmentation

# 7. 启动交互应用
voice-emotion --workdir ./voice_state_data app
```

交互应用命令：

```text
status / generate / run / step <stage> / demo [stage]
selfcheck / report / errors / help / quit
```

控制台会显示当前阶段、最后已提交阶段、恢复起点、全局快照版本、各阶段对话数、报告哈希和最近业务错误码。

## Commands

| Command | Purpose |
| --- | --- |
| `generate` | 固定种子生成本地 WAV、逐帧真值和两份 JSONL 清单 |
| `run` / `resume` | 完整执行或从已提交前缀恢复 |
| `status` | 显示阶段、版本、计数、恢复位置、报告哈希和错误日志 |
| `report` | 读取已提交聚合快照，可输出规范 JSON 报告 |
| `features` | 导出指定对话的确定性逐帧特征 JSONL |
| `selfcheck` | 校验阶段集合、分段连续性、聚合重算和报告哈希 |
| `demo` | 子进程提交指定阶段后调用 `os._exit(75)`，重启恢复并逐字节比较 |
| `append-registry` | 追加注册样本并提升 `registry_version` |
| `app` | 启动本地交互式应用 |

`run` 的恢复是隐式的：只要状态目录中存在连续已提交阶段，就从第一个未提交阶段继续。已提交阶段不会重新执行，已提交回归分数不会重复计算。

## Determinism

- 音频、说话人参数、注册片段、时间轴、能量包络和真值全部由 `numpy.random.Generator` 的固定种子生成。
- 帧边界由样本索引确定为 `start = frame_index * hop_samples`、`end = start + frame_samples`；只使用完整帧。
- 特征、分数、报告均经过固定精度量化并通过规范化 JSON（排序键、紧凑分隔符、UTF-8 换行）序列化。
- 报告不含时间戳或临时路径。相同数据、相同配置和相同注册版本在完整运行与恢复运行中产生逐字节一致报告。
- `valence` / `arousal` 使用固定的确定性线性模型；本应用不等同于生产级训练模型，重点是状态演进、数据契约与恢复一致性。

## Persistent State

状态目录形如：

```text
state/
  manifest.json                 # 原子提交的当前阶段清单
  snapshots/                    # 每个已提交阶段的内容寻址快照
  cache/                        # 注册嵌入缓存，文件名含版本与注册校验和
  archive/                      # 分析上下文/注册版本变化时整体归档的旧前缀
  diagnostics.jsonl             # 最近业务错误码
  report.json                   # run 成功后导出的规范报告
```

每次提交先原子写入快照，再原子替换 `manifest.json`。若进程死在两次写之间，旧清单仍指向旧快照，新文件属于未引用临时结果，重启不会将其视为完成。

快照携带：

- 全局单调递增 `version` 和阶段内 `revision`；
- 阶段名、分析上下文键、注册版本与注册校验和；
- payload 校验和与快照整体校验和；
- 完整 `dialog_ids` 集合和对话计数。

提交锁使用同目录原子 `mkdir`，不阻塞等待；冲突立即返回 `E_STAGE_BUSY`。已提交阶段不可重写，回退尝试返回 `E_SNAPSHOT_ROLLBACK`。注册版本或分析上下文变化时，旧快照前缀整体移动到 `archive/`，新阶段版本继续递增。

## Error Codes

| Code | Meaning |
| --- | --- |
| `E_CONFIG_INVALID` | 采样率、帧长/帧移、清单字段、帧数对齐或配置值无效；`details.field` 指明字段 |
| `E_SPEAKER_INVALID` | 真值或指派说话人不在注册控制集合；包含 `dialog_id` 与 `frame_index` |
| `E_RESOURCE_LIMIT` | 阶段对话数或估算内存超限；`degradation_category` 标记类别 |
| `E_STAGE_BUSY` | 另一个提交持锁；立即返回，不等待 |
| `E_SNAPSHOT_CORRUPT` | 清单、快照、payload 或嵌入缓存校验失败；包含阶段名与版本 |
| `E_SNAPSHOT_ROLLBACK` | 尝试覆盖已提交阶段或使用旧版本 |
| `E_INCONSISTENT` | 阶段集合、连续段、重算结果或恢复报告不一致 |

## Data Formats

对话清单 `manifest.jsonl`：

```json
{"dialog_id":"dlg_0001","sample_rate":16000,"duration_ms":6400,"seed":20261120,"speaker_count":3,"frame_ms":20.0}
```

注册清单 `registrations.jsonl`：

```json
{"speaker_id":0,"clip_seed":20261120,"clip_ms":760,"registry_version":1,"sample_checksum":"..."}
```

逐帧真值 `truth/<dialog_id>.jsonl`：

```json
{"frame_index":0,"speaker_id":0,"valence":0.25,"arousal":0.61,"is_speech":true}
```

## Verification

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python scripts/acceptance.py --out artifacts/validation
```

测试覆盖确定性分帧、配置错误、真值对齐、说话人越界、同分规则、回归/聚合重算、阶段集合不一致、原子锁、回退、快照/缓存损坏、资源上限、注册版本演进、提交后强杀恢复、阶段中强杀丢弃未提交状态，以及完整交互演示。实际结果见 `docs/VALIDATION.md`。

更细的状态机和事务说明见 `docs/ARCHITECTURE.md`。
