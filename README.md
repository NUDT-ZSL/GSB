# 离线多通道声场合成链路（镜像声源法 · 定点 + float64 双路径）

面向游戏配音节目的离线链路：给定六面体录制棚、发声点与多通道扬声器阵列，
用镜像声源法逐频段重建棚内冲激响应，再与各声源干信号逐频段卷积合成多通道
环绕声。核心关注能量守恒——吸声系数、路径衰减、频段增益与能量全部以 Q24
定点数参与累乘和“可忽略”裁定，同时维护 float64 参考路径用于诊断。

## 环境准备（Windows）

```powershell
python --version    # 需 3.11 或以上
pip --version
pip install -r requirements.txt    # numpy==1.26.4；WAV 仅用标准库 wave
```

## 快速开始

```bash
# 1. 固定种子生成场景 JSON 与干信号 WAV（无外部素材、无网络）
python scripts/generate_sample.py --root data --seed 20261125

# 2. 运行重建与合成（--clean 运行前清空历史状态）
python -m soundfield.main --scene data/scene.json --state-dir state --output-dir out --clean

# 3. 运行全部测试
python -m unittest discover -s tests -v
```

输出目录：

* `out/soundfield_multichannel.wav`：定点路径多声道正式输出（含 `.meta.json` 版本摘要）
* `out/soundfield_float64_reference.wav`：float64 参考诊断输出
* `out/residual_report.json`：业务码、版本标识、逐镜像裁定与残差溯源报告

状态目录：`state/version.json`、`state/blocks/`（分块 IR + 残差 + 元数据）、
`state/interrupted.json`（分块边界中断标记）。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 业务错误（stderr 输出带 E_xxx 码的 JSON，且不产生输出） |
| 2 | 未预期运行时错误 |
| 3 | 可处理终止信号，已在分块边界安全停止，可续算 |

## 文档

* 设计与定点/浮点、枚举、溯源、并发细节：`docs/TECHNICAL_DESIGN.md`
* 实际验证结果与数值：`docs/VERIFICATION.md`

## 约束遵守

* 不使用 Docker 或其他容器；仅本地确定性样本，不访问未授权目标
* 不引入音频库，WAV 读写只用 Python 标准库 `wave`
* 定点标度与分数延迟滤波器长度固定，内存超限时只让分块等待，不降级
