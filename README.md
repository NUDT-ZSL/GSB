# Seraph — 对话语音情感回归本地训练与诊断引擎

面向本地确定性合成对话语音的分块线性回归训练引擎，覆盖分帧/特征/切段/池化、
多精度分块训练、中断续算、逐级误差归因与跨模块一致性自检。仅依赖
`numpy` 与 `pytest`，不使用 GPU、容器、外部网络或真实账号。

## 1. 环境准备（Windows / WSL 通用）

要求 Python 3.10 及以上。

```powershell
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

Linux / WSL：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

依赖版本固定在 `requirements.txt`（`numpy==1.26.4`、`pytest==8.3.3`）。

## 2. 生成确定性样本

对话时间轴与种子写在 `config/corpus_spec.json`，默认产出 6 段对话、
16kHz 单声道 int16 PCM、逐帧真值 CSV（20ms 帧长、10ms 帧移）与
`manifest.jsonl` 清单。

```bash
python -m scripts.generate_data
# 产物：data/corpus/pcm/*.pcm, data/corpus/truth/*.csv,
#       data/corpus/manifest.jsonl
```

生成过程只依赖显式种子与时间轴，不读取系统时间；同一 spec 重复生成
逐字节一致。

## 3. 演示入口（覆盖三类必演场景）

```bash
# 一次完整训练 + 三档精度变体 + 误差谱 + 自检，落 reports/full_run_report.json
python -m scripts.demo --scenario full

# 第 3 轮提交后强制终止，再重启续算，与完整运行逐字节比对
python -m scripts.demo --scenario recovery

# 注入非法采样率，演示 E_CONFIG_INVALID 与字段定位
python -m scripts.demo --scenario invalid

# 依次执行以上三个场景
python -m scripts.demo --scenario all
```

中断轮次可用 `--kill-epoch N` 调整；快照写入 `snapshots/`，
JSON 报告写入 `reports/`。

交互诊断应用：

```bash
python -m scripts.interactive
# status / spectrum / chunk / selfcheck / kill <epoch> / resume / full / errors
```

## 4. 运行测试

```bash
python -m pytest
```

61 个用例覆盖八项业务需求，包括逐字节确定性、三档精度算子、分块/整段
对照、快照损坏与血缘漂移、强制终止续算、资源上限降级、业务错误码与
跨模块自检。

## 5. 目录结构

```
seraph_core/
  config/config.py        # 类型化配置与字段级 E_CONFIG_INVALID 校验
  data/synth.py           # 确定性 PCM/真值/清单合成
  data/dataset.py         # 清单/PCM/真值读取与帧数一致性校验
  engine/features.py      # 分帧、短时能量/过零率/F0、静音切段、池化、标准化
  engine/precision.py     # float64 / float32 / 定点缩放整数三档统一算子
  engine/training.py      # 分块线性回归、显式顺序归约、收敛门限、资源上限
  engine/pipeline.py      # 分帧->切段->池化->对话级汇总编排
  state/snapshot.py       # 单调版本、SHA-256 校验和、原子提交、非阻塞锁
  state/runner.py         # 四阶段状态机、续算、强制终止钩子
  diag/error_spectrum.py  # 分块/整段对照与逐级误差谱、主导来源归因
  diag/consistency.py     # 跨模块一致性自检（E_INCONSISTENT）
  diag/app.py             # 本地诊断应用与 JSON 报告
config/                   # 默认配置与语料规格（纳入版本管理）
tests/                    # pytest 用例
scripts/                  # 生成、演示、交互入口
docs/TECHNICAL_NOTES.md   # 设计与验证说明
```

## 6. 关键业务码

| 代码 | 触发场景 |
| --- | --- |
| `E_CONFIG_INVALID` | 采样率、帧长/帧移、精度档、定点缩放、正则、容差等字段非法 |
| `E_DUPLICATE_DIALOG` | 清单或规格中出现重复 `dialog_id` |
| `E_TRUTH_MISMATCH` | 真值帧数与分帧音频数、样本数或 frame_index 序列不一致 |
| `E_RESOURCE_LIMIT` | 对话数/驻留段/梯度内存超上限（可先降级分块） |
| `E_STAGE_BUSY` | 阶段提交锁被占用，后到者立即失败不等待 |
| `E_SNAPSHOT_CORRUPT` | 校验和不符、文件缺失或配置/schema 血缘漂移 |
| `E_INCONSISTENT` | 对话集合、损失复算、偏差谱、保护/跳过记录自检失败 |

## 7. 验收产物

- `reports/full_run_report.json`：完整训练、分块/整段对照、五级误差谱、
  主导归因与全部自检条目。
- `reports/recovery_report.json`：强制终止与续算事件、跳过/保护轮次、
  权重与损失轨迹逐字节一致性结论。
- `reports/invalid_config_report.json`：非法配置业务码与字段。

更多设计依据见 `docs/TECHNICAL_NOTES.md`。
