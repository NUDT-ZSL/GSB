# 运行入口与本地验证说明

## 环境准备（Windows / Linux 均可，无容器、无网络依赖）

需要 Python 3.10+（实测 Python 3.11）、NumPy、Pillow。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux: source .venv/bin/activate
pip install numpy pillow
```

## 1. 生成确定性样本

```bash
python scripts/generate_samples.py --out sample_data
```

固定种子 `20261117`，产出 5 个场景的 PNG、v1 二进制包、v2 JSON 包、
`metadata.json`、`pipeline_config.json`、`fault_injection.json`，以及
`corrupt/` 下三份用于业务码验证的损坏包。

## 2. 运行管线（带信号处理入口）

```bash
python -m panseg.cli \
  --sample-dir sample_data \
  --work-dir validation/run \
  --config sample_data/pipeline_config.json \
  [--fault-config sample_data/fault_injection.json] \
  [--report validation/run/validation_report.json]
```

退出码见 `docs/ARCHITECTURE.md` 第 8 节。报告以临时文件 + 原子替换写盘。

信号：POSIX 下 SIGTERM/SIGINT、Windows 下 SIGBREAK 由入口注册处理器，
收到后完成当前实例产物与 manifest 原子提交并以 0 退出。

## 3. 一键验收

```bash
python scripts/run_acceptance.py --sample sample_data --out validation/acceptance
```

串联：干净端到端、三类损坏业务码、单线程/线程池确定性、边界诊断、
两压缩档 x 两来源 x 五步编辑的增量/全量一致性矩阵，输出
`validation/acceptance/acceptance_report.json`，`passed=true` 即全部通过。

## 4. 测试套件

```bash
python -m unittest discover -s tests -v
```

31 个测试覆盖 RLE 可逆与越界、v1/v2 包往返与三类业务码、双向转换损失、
融合/crowd/保留策略、指纹失效、快照原子性、异常隔离、强制终止恢复与真实
SIGTERM 子进程信号处理。

## 5. 异常注入（独立配置，不改源码）

`fault_injection.json` 声明阶段、目标实例与损坏模式；样本生成器据此在
`corrupt/` 物化 RLE 越界、base64 长度不符、调色板缺失三类包，
`snapshot_hard_crash` 用于模拟快照前强制终止。
