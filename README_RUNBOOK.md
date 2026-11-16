# 运行手册

## 环境准备（全程离线）

需要 Python 3.11+。本工作区已在 `.venv` 内离线安装 numpy 2.4.6、soundfile 0.14.0、
pytest 9.2.0（来自本地 wheel 与本机已有站点包，未访问网络）。

如需重建：

```bash
# 1) 用本地 virtualenv 引导（系统 python3 无 ensurepip）
PYTHONPATH=<本地 virtualenv site-packages> python3 -m virtualenv .venv
# 2) 离线安装 numpy wheel
.venv/bin/pip install --no-index /tmp/wheelout/numpy-*.whl
# 3) soundfile/pytest 及其依赖从本机离线站点复制到 .venv/lib/python3.11/site-packages
```

## 生成确定性样本

```bash
.venv/bin/python data/generate_samples.py
# 重复运行产出字节一致的 WAV（已用 sha256 校验）
```

## 运行工作台

```bash
PYTHONPATH=src .venv/bin/python -m voiceworkbench.cli \
  --config configs/config.json \
  --audio-dir data/audio \
  --commands commands/edits.jsonl \
  --snapshot-dir snapshots/final \
  --report artifacts/diagnostic_report.json \
  --baseline-report artifacts/serial_baseline.json
```

可选环境变量：

- `WORKBENCH_EXIT_AFTER_SEQ=3`：处理到第 3 条命令后完成快照并以 0 退出（可恢复）。
- `WORKBENCH_SLOW_MS=5`：在预算检查点注入延时，用于稳定复现预算挂起与信号中断。

## 测试与验收

```bash
.venv/bin/python -m pytest -q          # 41 个用例
.venv/bin/python scripts_verify.py     # 4 组配置矩阵 + 中断恢复，overall_ok=true
```

## 配置与命令

- 配置：`configs/config.json`（8 档量化）、`config_quant4.json`（4 档）、
  `config_threshold_loose.json`（宽松聚类阈值）。
- 命令流：`commands/edits.jsonl` 覆盖改阈值、裁区间、合并、拆分、区间替换五类。
- 异常清单：`commands/anomalies.jsonl` + `commands/anomaly_manifest.json`，
  坏音频样本在 `data/bad_audio/`（对应 `E_AUDIO_FORMAT`）。

## Windows 原子替换说明

实现统一使用 `tempfile.mkstemp(同目录)` + `os.replace`，两者在 Windows NTFS 与
POSIX 上均为原子替换；快照写入附带 `flush + fsync`。无跨文件系统 rename，
不依赖符号链接或 POSIX 专有语义。
