# 实际验证记录

验证日期：2026-11-18（Asia/Shanghai）

本机环境：Python 3.12.13，未安装 NumPy。实现只使用标准库；因此同一代码可在
Python 3.11+（包括 Windows 本机 Python 3.11）直接运行。

交付引擎版本：`hdr-color 1.0.2`。

## 1. 样本生成

```bash
python generate_samples.py --output samples
```

生成结果：

- 固定种子：`20261118`
- 17 帧，每帧 32 像素，三通道浮点线性光
- 帧文件 SHA-256：`c88db745483dae4d7e1065484f344f5679098cb628966a80aa2485e62429dd6a`
- 任务：PQ/BT2020 -> SDR/BT709、HLG/BT2020 -> PQ/P3、
  SDR/BT709 -> HLG/BT2020、PQ 四类注入任务

## 2. 自动化测试

执行：

```bash
python -m unittest discover -v
```

结果：`Ran 32 tests`，全部通过。

覆盖项：

- 三条曲线的规格常数、分段连续性、HLG 断点、PQ 绝对端点
- SDR/HLG/PQ 正逆往返和报告诊断
- 色度坐标矩阵构造、逆矩阵、Bradford、白点与宽色域检测
- 色调映射单调性、有界性、比例保持、回压和全黑
- 非法任务、非法色域/亮度/色调参数、NaN、负值、宽度不一致、重复键
- 黑帧、超高峰值帧、逐帧/窗口统计的最终帧重算一致性
- 四类注入的错误码、继续处理、首错帧和源文件不修改
- 帧块失败、取消和“无完整输出”
- 并发 1 与 4 的帧数据、逐帧统计、窗口统计和映射摘要逐元素相等
- 末尾不足一窗的 `include`/`drop` 规则和无效帧排除
- 内存预算不足以容纳一帧加共享矩阵/LUT 时返回 `E_MEMORY_BUDGET`
- 已完整输出但状态文件仍为 `interrupted` 时只修复状态门，不重写帧/报告
- 同版本不替换、版本变化仅通过原子替换生效
- 进程级 SIGTERM：退出码 143、`interrupted` 状态、无完整输出、恢复后完成

## 3. 本地样本结果

### PQ/BT2020 -> SDR/BT709

- 版本：`1178caa1ea5ddc279d33f1ad0104ff44`
- 17 帧全部有效
- 亮度均值：48.792144 cd/m2
- 亮度峰值：95.483723 cd/m2
- 最终帧超目标峰值比例：0
- 源 PQ 往返最大绝对误差：`7.621565600857139e-10` cd/m2
- 目标 SDR 往返最大绝对误差：`3.062693254562987e-08` cd/m2
- 色域外像素比例：0.299632
- 逐通道回压像素比例：0.529412

### HLG/BT2020 -> PQ/P3

- 版本：`65412012a1445bbc2c412f94bc0281a0`
- 17 帧全部有效
- 亮度均值：286.888415 cd/m2
- 亮度峰值：961.805407 cd/m2
- 最终帧超目标峰值比例：0
- 源 HLG 往返最大绝对误差：`1.286969563141227e-06` cd/m2
- 目标 PQ 往返最大绝对误差：`7.621565600857139e-10` cd/m2
- 色域外像素比例：0.1875
- `tail_mode=drop` 的最后 2 帧被规格化排除出窗口

### SDR/BT709 -> HLG/BT2020

- 版本：`53ef48cc7f9363a5935ce89b0358ab6f`
- 17 帧全部有效
- 亮度均值：190.105272 cd/m2
- 亮度峰值：951.599485 cd/m2
- 最终帧超目标峰值比例：0
- 源 SDR 往返最大绝对误差：`3.062693254562987e-08` cd/m2
- 目标 HLG 往返最大绝对误差：`1.286969563141227e-06` cd/m2
- 末尾 5 帧窗口按 `include` 生成，`start_frame=12, end_frame_exclusive=17`

### 异常注入链

- 版本：`d629ff2be72a67bdcc060270f8a8b2c9`
- 17 帧中 13 帧有效、4 帧失败，其余帧继续处理
- 首错帧：2
- 注入错误：
  - 帧 2：`E_INJECT_NAN`
  - 帧 4：`E_INJECT_NEGATIVE`
  - 帧 6：`E_INJECT_PEAK_OVERFLOW`
  - 帧 8：`E_INJECT_BAD_TRANSFER`
- 源帧文件执行前后 SHA-256 相同
- 窗口 0 的 `start_frame=0, end_frame_exclusive=3`，其中 2 个有效帧、1 个无效帧

## 4. 中断恢复实测

自动化测试使用 80 帧、20 个持久化块和每块 70 ms 测试延迟：

1. 等待 `chunk-000000.json` 与 `run-state.json` 出现。
2. 发送 SIGTERM。
3. 进程退出码为 `143`，stdout 为 `status=interrupted`，`run-state.json`
   的 `status=interrupted`，没有 `converted_frames.json` 或
   `statistics_report.json`。
4. 再次启动同一任务，报告
   `processing.resumed_from_persisted_chunks` 非空，最终输出 80 帧并标记
   `complete`。

## 5. 原子与版本行为实测

- 同一任务第二次运行返回 `status=unchanged`，两份输出的 `mtime` 不变。
- 修改色调映射参数后版本变化，任务重新计算并替换输出。
- 输出目录内无 `.tmp` 残留；完成后 `.hdr-color-work` 被清理。
