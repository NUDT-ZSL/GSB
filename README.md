# 本地钢琴录音修复工作台

48kHz/16bit/单声道 WAV 的本地、确定性、可撤销/可恢复的钢琴录音修复工作台。
纯本地 NumPy/soundfile 实现，无远程服务、无真实录音资产、无声卡依赖；
钢琴样本由固定种子合成。

## 环境准备（Windows 10/11，Python 3.11/3.12）

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Linux 复现（本仓库验证环境）：

```bash
python3.11 -m venv --without-pip .venv   # 系统缺 ensurepip 时
.venv/bin/python get-pip.py              # 引导 pip（如需要）
.venv/bin/python -m pip install -r requirements.txt
```

## 一键演示（无需显示器/声卡）

```bash
.venv/bin/python -m piano_workbench.demo --output-dir demo_output
```

演示依次完成：合成钢琴 → 建工作区 → 谱减/包络/淡变/STFT 修补固定序列 →
同选区叠加 → 全节点确定性校验 → 撤销/重做字节一致性 → 新操作截断重做分支 →
分支切换 → 受控退出重开 → 模拟强制终止（孤儿缓存回收）→ 五类错误码演示 →
导出 WAV，并写出 `demo_report.json` 与 `state_snapshot.json`。

## 测试

```bash
.venv/bin/python -m pytest
```

35 个用例覆盖 DSP 确定性、STFT 往返、状态树内容寻址、选区冲突、
撤销/重做（含 200 步上限与分支）、持久化原子性、哈希/版本错误链路、
强制终止恢复与完整演示。

## 图形界面（可选）

```bash
.venv/bin/python -m piano_workbench.gui
```

GUI 仅通过控制器修改状态树，音频渲染统一由重建服务触发；
无头核心不依赖 PySide6。

## 模块与关键设计

见 `docs/ARCHITECTURE.md`。要点：

- STFT 固定 N=2048、H=512、周期 Hann；修补为幅度插值 + 固定 32 次
  Griffin-Lim 相位重建；谱减噪声估计使用固定种子。
- 节点内容寻址（规范化 JSON → SHA-256），缓存以 PCM 内容哈希命名；
  每个节点落盘前统一 int16 量化（半值远离零取整），重复重建逐字节一致。
- 缓存先落盘 fsync，清单再原子替换；强制终止后只恢复最后已提交状态，
  未引用缓存移入 `orphan/`，退出标记如实记录 `controlled/forced`。

## 验证结果

见 `docs/VERIFICATION.md`（含实际 pytest 与演示输出摘要）。
