# 语音情感与对话理解领域组件

纯 Python + NumPy 的可集成组件：从本地确定性合成对话音频出发，完成帧级声学特征、
说话人分离、逐段 valence/arousal 回归与对话级聚合，并随数据规模在**精细路径**与
**逼近路径**之间自适应取舍。无外部音频、模型权重或线上服务，结果逐字节可复现。

## 环境准备

需要 Python 3.10+。推荐在工作区内使用虚拟环境：

```bash
python3 -m venv .venv            # 或使用 uv venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## 一键示例

```bash
bash scripts/run_demo.sh
```

或分步执行：

```bash
# 1. 生成确定性样本（PCM + 逐帧真值 CSV + manifest.jsonl）
.venv/bin/python -m voice_dialogue generate --out-dir data/samples

# 2. 小规模 -> 精细路径
head -n 3 data/samples/manifest.jsonl > data/samples/manifest_small.jsonl
.venv/bin/python -m voice_dialogue run \
  --manifest data/samples/manifest_small.jsonl \
  --config config.example.json \
  --report out/report_fine.json \
  --snapshot-dir .cache/snapshots

# 3. 全量混合规模 -> 逼近路径
.venv/bin/python -m voice_dialogue run \
  --manifest data/samples/manifest.jsonl \
  --config config.example.json \
  --report out/report_approx.json

# 4. 校验快照版本与校验和
.venv/bin/python -m voice_dialogue verify-snapshot --snapshot-dir .cache/snapshots
```

## 作为库集成

```python
from voice_dialogue import (
    ComponentConfig, extract_features, separate_speakers,
    score_segments, aggregate_dialogue, observe_scale, decide_path,
    run_pipeline,
)

config = ComponentConfig()                       # 非法值会抛 E_CONFIG_INVALID
report = run_pipeline("data/samples/manifest.jsonl", config,
                      report_path="out/report.json",
                      snapshot_dir=".cache/snapshots")
print(report["path_decision"]["path"])           # fine / approximate
```

## 输出报告内容

`out/report.json` 包含：配置、规模观测、路径决策（含取舍理由、降级类别、资源投影与
优先级规则）、路径定义、每个对话的逐段分数与置信区间、说话人切换时间轴、逐帧标签、
特征/比较/回归资源用量、聚合权重口径，以及快照版本与校验和。业务失败时输出
`status=error` 与 `business_code`，CLI 退出码为 2。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

覆盖：分帧精确性、特征/报告逐字节复现、两条路径选择、资源预算 error/degrade、
全部业务错误码、快照校验和/版本回退/损坏检测、跨阶段一致性自检。

更多设计细节见 `docs/ARCHITECTURE.md`。
