# 实际验证结果

执行日期：2026-11-17  
执行环境：`Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.35`（Windows 11 WSL2）  
Python `3.11.0rc1`，NumPy `2.4.6`，Pillow `13.0.0`。

> 当前会话实际运行在 WSL2，而非独立 Windows 原生 shell。代码使用跨平台原子替换、Windows `SIGBREAK` 与 `.bat` 入口；Windows 安装和运行步骤见 README。

## 自动化测试

```text
Ran 10 tests in 12.4s
OK
```

覆盖：端到端重建与恢复复用、串行/并发字节一致、反向匹配一致、图像变化缓存失效、深度参数变化缓存失效、异常注入业务码、快照配置拒绝、信号中断恢复、强制终止恢复、视图校验失败继续。

完整验收脚本：

```powershell
.\.venv\Scripts\python scripts\validate_demo.py
```

结果：**39/39 项检查通过**。机器可读汇总在 `validation/acceptance/summary.json`。

## 基线重建

样本：8 个 640x480 PNG，种子 `20261117`，72 个三维纹理标记。配置：4 线程、特征阈值 0.08、最小内点 6、最小视差 0.75°、深度层 80x60/256 档、平滑 2 轮/权重 0.35、体素 0.15。

使用同一种子重新生成后，17/17 个样本文件（8 PNG、8 相机 JSON、1 清单）逐字节相同。

| 视图 | 特征数 | 匹配对数 | 重建点数 | 非空深度单元 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 102 | 2 | 140 | 92 |
| 2 | 102 | 2 | 159 | 100 |
| 3 | 98 | 2 | 149 | 92 |
| 4 | 98 | 3 | 198 | 90 |
| 5 | 99 | 3 | 198 | 96 |
| 6 | 103 | 2 | 149 | 95 |
| 7 | 98 | 2 | 154 | 94 |
| 8 | 93 | 2 | 135 | 91 |

总体：

- 成功匹配对：9；描述匹配和内点均由 `matches.json` 可审计；
- 三角化重建点：641；
- 全局点云体素：177；
- 业务失败：0；
- `result_digest`：`d38e026118ab39433a99a902c3d5a73990245564f0fdd0df686795d379ef57b3`；
- 点云内容摘要：`602c0ef5098a00651216db28a061178f36d5639e9b9efaf8d013cb97aada450b`；
- 快照版本：1。

基线产物 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `report.json` | `2891429d8dc83aa4fbe040501f228380e4d385b42e8575edaa7d83e60d982a1a` |
| `cloud.json` | `fd53c3c3d7940279f39464baf0d8308e32f615670ecfedeb3dcb411dc4f1ce15` |
| `features.json` | `8eace631aa6066a688cb5197ab8cda34f7dd0d5b19cbd76dd2af224d0fbe7ae0` |
| `matches.json` | `b03a1f8c6ef7e645c6e0cebd07d05ee0c2a3e3d65c0ef738cf0d24b2ab4429ac` |
| `depth_layers.json` | `562fe59c38e86fec8c3539af14cdfa208fec258da6419061fa0f2c80fe5aba73` |

## 一致性与恢复

| 场景 | 结果 |
| --- | --- |
| 同状态再次运行 | `result` 完全相同；特征 8/8、匹配 9/9、深度 8/8、点云 1/1 全部复用，无重算 |
| 1 线程 vs 4 线程 | `report.result` 完全相同；`features.json`、`matches.json`、`depth_layers.json`、`cloud.json` 逐字节相同 |
| SIGINT | 退出码 0；最新快照序号 5 的阶段部分进度；中断报告原子写出；恢复后 `result` 与基线相同 |
| SIGKILL | 退出码 -9；无最终报告；最新快照序号 6、阶段 `matching`、`stage_complete=false`；恢复后复用完整匹配产物，最终 `result` 与点云字节均和基线相同 |
| 修改并发配置后恢复旧快照 | 输出 `E_CONFIG_MISMATCH`，退出码 3，拒绝续跑 |

## 缓存精确失效

共享基线缓存，仅替换 `view_0004.png` 的背景像素：

| 产物 | 复用 | 重算 |
| --- | ---: | ---: |
| 特征 | 7 | 1 |
| 匹配 | 6 | 3 |
| 深度层 | 4 | 4 |
| 全局点云 | 0 | 1 |

修改 `view_0004` 相机 `cx` 得到相同失效范围。

仅调整深度平滑迭代次数：

| 产物 | 复用 | 重算 |
| --- | ---: | ---: |
| 特征 | 8 | 0 |
| 匹配 | 9 | 0 |
| 深度层 | 0 | 8 |
| 全局点云 | 0 | 1 |

## 异常注入

可恢复 profile 的基线注入：

- `INJ-PNG-001`：视图 2 损坏 PNG → `E_VIEW_INVALID`；
- `INJ-CAM-002`：视图 3 相机缺失 → `E_VIEW_INVALID`；
- `INJ-PAR-003`：匹配对 `[4,5]` 视差越界 → `E_MATCH_INSUFFICIENT`；
- `INJ-NAN-004`：视图 6 非有限描述子 → `E_NONFINITE_FEATURE`。

运行仍以 `completed` 结束，其余视图继续。物化样本中的视图 2 PNG 是实际写坏的字节，视图 3 相机文件实际不存在；运行报告同样命中上述 4 个 ID。

阶段异常 profile：

- `INJ-EXC-F-005`：视图 7 特征阶段异常；
- `INJ-EXC-M-006`：视图 8 匹配阶段异常；
- `INJ-EXC-D-007`：视图 4 深度阶段异常；
- 业务码均为 `E_STAGE_EXCEPTION`，其余任务继续完成。

配置非法 profile：

- `INJ-VOX-008`：体素尺寸为零；
- `INJ-WRK-009`：线程资源预算非法；
- 输出 `E_CONFIG_INVALID`，退出码 2，两个注入 ID 均进入命中清单。

## 证据文件

- `validation/acceptance/summary.json`：完整验收检查、计数、哈希和恢复详情；
- `validation/acceptance/baseline/`：基线报告、点云、特征、匹配、深度层；
- `validation/acceptance/injection_recoverable/report.json`：可恢复注入业务码与命中；
- `validation/acceptance/injection_stage_exceptions/report.json`：阶段异常继续执行；
- `validation/acceptance/injection_config_invalid/report.json`：配置非法；
- `validation/acceptance/graceful_sigint/interrupt_report.json`：信号中断报告；
- `validation/acceptance/forced_kill_resumed/report.json`：强杀后恢复报告。
