# 动捕多设备录音后期处理引擎

纯本地命令行引擎，仅依赖 Python 3.10+ 标准库与 NumPy，无外部音频库、
无在线资源。

## 快速开始

```
pip install numpy
# Windows
run_demo.bat
# Linux/macOS
./run_demo.sh
```

或分步执行：

```
python scripts/generate_samples.py --spec scripts/demo_spec.json --out demo_workspace
python -m mocap_audio.cli --session demo_workspace/session.json
python -m unittest discover -s tests
```

## 会话 JSON 关键字段

`devices`（含参考设备标记、采样率、位深、段文件、声道布局、增益、段内脉冲
理论时刻与搜索窗）、`model_order`（0/1）、`stat_window_ms`、
`absolute_gate_db`、`relative_gate_db`、`dr_lower_db`、`dr_upper_db`、
`target_sample_rate`、`target_bits`、`timeslice_ms`、`concurrency`、
`memory_budget_bytes`、`state_dir`、`output_wav`、`output_report`、
`output_channels`，可选 `peak_threshold`、`tail_rule`、`gap_fill`。

## 业务码与退出码

E_FORMAT=20、E_RATE=21、E_MISSING=22、E_SYNC=23、E_BUDGET=24、
E_WR=25、E_INTR=130；成功为 0。错误以 JSON 写入 stderr 并携带设备/段标识。

## 文档

- 架构与算法：`docs/ARCHITECTURE.md`
- 验收与实测结果：`docs/VALIDATION.md`
- 演示生成规格：`scripts/demo_spec.json`

