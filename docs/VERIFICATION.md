# 实际验证记录

验证环境（本机 WSL Ubuntu 22.04，等价于 Windows 纯 Python 依赖栈）：

- Python 3.11.0rc1 独立虚拟环境 `.venv`（系统缺 `ensurepip`，用 `get-pip.py` 引导）
- numpy 2.4.6、soundfile 0.14.0（libsndfile 随 wheel）、pytest 9.2.0、PySide6 6.12.0
- 无音频设备；GUI 测试以 `QT_QPA_PLATFORM=offscreen` 运行

## 1. 测试结果

命令：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -v`

结果：**38 passed**（DSP 9、状态树 9、撤销重做 7、持久化 10、演示集成 2、GUI 冒烟 1）。覆盖：

- STFT 常量与内部往返重建误差 < 1e-5；int16 确定性量化与限幅。
- 四种操作的确定性、选区外样本不变、长度不变、字节稳定。
- 节点内容寻址、同选区叠加、部分重叠拒绝、非法选区 `E_SEL_RANGE`。
- 撤销/重做 PCM 与缓存一致、重做父参数防护、重做分支截断、200 步上限、分支切换不丢节点。
- 清单/缓存布局、重开状态一致、哈希篡改与缓存缺失报 `E_CACHE_HASH`、
  版本冲突报 `E_VERSION_CONFLICT` 且工作区零改动、失败提交无部分写入。
- 强制终止孤儿回收、受控退出标记真实性、跨重开重复重建逐字节一致。

## 2. 跨进程确定性

两个独立 Python 进程、不同工作区目录执行同一固定序列：

```
diff /tmp/A.txt /tmp/B.txt   -> 无差异
CROSS-PROCESS DETERMINISM: IDENTICAL
cmp <runA>/cache/<sha>.wav <runB>/cache/<sha>.wav -> WAV BYTE-IDENTICAL
```

节点 ID 与缓存 SHA-256 在进程间完全一致；内容相同的两个节点自动共享同一缓存文件。
重复运行 `python -m piano_workbench.demo` 得到相同 root 节点 ID（`n_152e0faed2ad1a21`）。

## 3. 一键演示实测

命令：`.venv/bin/python -m piano_workbench.demo --output-dir demo_output`

关键结论（来自 `demo_output/demo_report.json`）：

- `undo_redo.roundtrip_byte_identical = true`，`undo_matches_cache = true`
- `redo_truncation`：redo 深度 1 → 0，被截断节点仍保留在树中可重建
- `branch_switch.matches_original_head = true`，分支节点未丢失
- 强制终止：`orphan_quarantined = true`、
  `stable_state_byte_identical = true`、`verified_caches = 7`
- 错误码演示：`E_SEL_RANGE`（非法范围/重叠）、`E_NODE_MISSING`、
  `E_CACHE_HASH`、`E_VERSION_CONFLICT`、`E_PARAM` 均返回
  `{"ok": false, "code", "message", "detail"}`

### 音频指标（demo_report.json 实测）

| 节点操作 | 采样数 | RMS(dBFS) | 峰值(int16) | 平均绝对幅度 |
| --- | --- | --- | --- | --- |
| root | 96000 | -12.5995 | 27853 | 0.18462177 |
| denoise | 96000 | -15.9084 | 19061 | 0.11654409 |
| envelope | 96000 | -17.1328 | 19061 | 0.10129826 |
| fade | 96000 | -17.9415 | 19061 | 0.08993819 |
| patch | 96000 | -18.879 | 19061 | 0.07233395 |
| envelope | 96000 | -19.4421 | 19061 | 0.06851428 |

指标随操作单调合理变化（谱减降噪后 RMS 下降、包络/淡变进一步降低电平），
所有节点均为 96000 采样 = 2.0 秒 @ 48kHz。导出 `head_export.wav` 经
soundfile 复核为 48000 Hz、int16、单声道。

## 4. 强制终止附加健壮性测试

手工在已打开工作区中遗留 `manifest.json.*.tmp`（伪造 version=999）与
`cache/pending-*.wav`（非法 WAV），写入 `forced` 退出标记后重开：

- 暂存清单因文件名不匹配被忽略，正式清单完好；
- pending 缓存被移入 `orphan/`；
- 7 个已提交缓存全部哈希校验通过，当前节点 PCM 与终止前逐字节一致。

## 5. 复现步骤

```bash
python3.11 -m venv --without-pip .venv          # Windows 用 py -3.11 -m venv .venv
.venv/bin/python /tmp/get-pip.py                # 仅缺 pip 时需要
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m piano_workbench.demo
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
```
