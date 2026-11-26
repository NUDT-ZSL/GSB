# 语音情感与对话理解诊断引擎

本地、确定性、可复现的 16kHz 单声道 PCM 对话诊断引擎。系统由固定种子生成多说话人基频/共振峰/能量包络和话轮异常，完成帧特征、情感状态机、话轮异常检测、v1/v2 账本互迁、增量失效、并行预算调度和强制终止恢复。

## 快速开始

```bash
pip install -r requirements.txt
python3 generate_samples.py --out samples
python3 run.py --mode all --samples samples --output artifacts --workers 2
python3 -m unittest tests.test_engine -v
bash scripts/verify_acceptance.sh
```

环境：Python 3.10+、NumPy 1.26+；SQLite 使用 Python 标准库。

## 验收结果

当前固定样本：

- PCM：224000 samples，16kHz 单声道 s16le，时长 14s；
- 分帧：320 samples/帧，帧移 160 samples，共 1399 帧；
- 异常：`abnormal_pause [600,690)`、`floor_grab [910,940)`、`interruption [1160,1190)`；
- 迁移：v1/v2 语义等价、往返语义等价；
- 增量：真值第 650 帧修正后复用 650 帧、重算 749 帧；高代数账本从第 760 帧起复用 760 条、重算 639 条；
- 测试：15 个 unittest 全部通过；
- 恢复：真实 SIGKILL 后丢弃未提交 work，特征帧全部复用，状态/异常等核心产物与无中断运行逐字节一致。

实际验证记录见 `docs/VALIDATION.md`，设计与错误码见 `docs/TECHNICAL.md`。

## 主要目录

- `generate_samples.py`：固定种子样本生成器；
- `run.py`：全流程、增量和故障注入 CLI；
- `diagnostic_engine.py`：常用 API 门面；
- `emotion_diag/`：引擎实现；
- `tests/test_engine.py`：自动化验收测试；
- `scripts/verify_acceptance.sh`：端到端验收脚本；
- `samples/`：生成的 PCM、逐帧真值、v1 JSON、v2 分片和 SQLite；
- `artifacts/`：正式诊断报告、缓存、迁移结果和阶段快照。

## 常用命令

```bash
# 仅生成样本
python3 run.py --mode generate

# 单进程可复现运行
python3 run.py --mode full --no-process-pool

# 并行进程池
python3 run.py --mode full --workers 2

# 增量失效演示
python3 run.py --mode incremental

# 软中断演示（返回码 137）
python3 run.py --mode full --crash-stage state_anomaly --crash-after-frame 300

# 真实 SIGKILL 后恢复：重跑同一 --output 目录
python3 run.py --mode full --crash-stage state_anomaly --hard-crash
python3 run.py --mode full
```

## 结构化错误码

`E_VERSION_UNSUPPORTED`、`E_FORMAT_CORRUPT`、`E_PARSE_ERROR`、`E_CHECKSUM_MISMATCH`、`E_SEMANTIC_LOSS`、`E_LOOP_BUDGET`、`E_RESOURCE_LIMIT` 均以 `DiagnosticError.code` 或 CLI JSON `error.code` 返回。
