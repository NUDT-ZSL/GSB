# 验收记录

环境：Python 3.10.12、NumPy 2.2.6（Linux 容器内验证；代码仅用跨平台标准库，
Windows 下 `python` 命令等价运行，见 `run_demo.bat`）。

## 1. 自动化测试

`python -m unittest discover -s tests` 共 34 个用例全部 OK：

| 测试文件 | 覆盖要求 |
| --- | --- |
| `test_wavio.py` | 16/24bit 往返、24bit 小端三字节、坏头/缺文件 |
| `test_session_validation.py` | E_FORMAT/E_RATE/E_MISSING、脉冲段内与递增、增益有限数等 |
| `test_sync.py` | LS 估计精度、单观测降级、零观测 E_SYNC、段外搜索窗 |
| `test_mapping_accuracy.py` | 44.1k/96k 与 ±漂移下映射误差 ≤ 1 参考采样点 |
| `test_timeline.py` | 缝隙零填充与标记、空片、重叠区先声明优先 |
| `test_loudness.py` | 毫秒窗口对齐、双门限、全静音确定结果、段归属 |
| `test_resample.py` | 有理数比帧数误差 ≤ 1、直流信号保持 |
| `test_commit.py` | 版本相同不替换、版本变更双换、失败回滚 E_WR |
| `test_scheduler.py` | 失败取消待处理段、在途合法段保留、预算准入排队 |
| `test_integration.py` | 跨并发/预算字节一致、E_BUDGET、E_SYNC 降级、中断恢复、快照失效、CLI 退出码 |

## 2. 确定性样本端到端结果

演示数据：参考机 48kHz/24bit，44.1kHz/16bit 机（offset +2.5ms、+120ppm，
刻意缺失 1 个脉冲），96kHz/24bit 机（offset -1.5ms、-80ppm），段间留缝隙。

同步估计（报告实测）：

| 设备/段 | 模型阶数 | 观测数 | 参考域整数偏移 | 漂移 ppm | 残差 RMS(设备采样点) | 缺失标记 |
| --- | --- | --- | --- | --- | --- | --- |
| DEV-REF/SEG-A | 1 | 恒等 | 0 | 0.0 | 0.0 | 无 |
| DEV-441/SEG-B | 1（3 观测 ≥ 最小 2 点，未降级） | 3 | 120 | 94.48 | 0.0 | 800ms |
| DEV-96/SEG-C | 1 | 4 | -72 | -73.78 | 0.2739 | 无 |

偏移估计与生成参数偏差均小于 1 个参考采样点；缺失脉冲在
`missing_markers` 中给出搜索区间与段外标志。

拼接与测量：

- 时间轴 163200 参考帧，17 个 200ms 时间片（9600 帧/片，边界严格对齐）。
- 缝隙 1 段（约 2197.4–2501.5ms，14599 帧）零填充并标记，切片 11 为空片。
- 34 个 100ms 统计窗，31 个通过绝对+相对门限，积分响度 -29.933 dBFS。
- 三段动态范围 29.3–29.8dB，落在 [6, 40] 预算内，建议均为 `hold`。
- 输出 48kHz/24bit/3ch，帧数 163200，与理论值误差 0；每帧 9 字节（3 字节×3 声道）。

## 3. 跨并发度与内存预算字节一致性

同一份输入在三种调度下产出的 sha256（前 16 位）：

| 并发度 | 内存预算 | WAV sha256 | 报告 sha256（配置字段归一化后） |
| --- | --- | --- | --- |
| 1 | 16 MiB | 893b71ada365105b | 2d0beb96bda44541 |
| 4 | 16 MiB | 893b71ada365105b | 2d0beb96bda44541 |
| 8 | 4 MiB | 893b71ada365105b | 2d0beb96bda44541 |

WAV 与报告字节完全一致；报告中的并发度/预算为如实记录的会话配置。

## 4. 中断与恢复

`scripts/interrupt_demo.py` 预置终止请求：

- 首次运行退出码 130（E_INTR），在 segment 边界 #1 写入 `interrupt.flag`，
  未完成段无完成摘要。
- 二次运行退出码 0，复用 sha256 校验通过的已完成段（实测 `SEG-B` 恢复），
  正常提交 WAV 与报告。
- 修改任一输入 WAV 后快照标识改变，状态目录整体重做，无段被标记为恢复。

## 5. 错误码分支

非法会话均返回 JSON 错误且不产生任何输出文件，实测退出码：

| 场景 | 业务码 | 退出码 |
| --- | --- | --- |
| 目标采样率 22050 | E_RATE | 21 |
| 段起点 -5ms | E_FORMAT | 20 |
| 引用 WAV 不存在 | E_MISSING | 22 |
| 预算 1024 字节 | E_BUDGET | 24 |
| 段无任何脉冲 | E_SYNC | 23 |
| 提交阶段替换失败 | E_WR（单测回滚旧版本） | 25 |
| 安全中断 | E_INTR | 130 |

## 6. 复现命令

```
python scripts/generate_samples.py --spec scripts/demo_spec.json --out demo_workspace
python -m mocap_audio.cli --session demo_workspace/session.json
python -m unittest discover -s tests
python scripts/interrupt_demo.py demo_workspace/session.json
```

Windows 使用 `run_demo.bat`，Linux/macOS 使用 `run_demo.sh`。

