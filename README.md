# 游戏配音批量修复组件

这是一个可在 Windows 本地直接运行的确定性、可续算音频批处理组件。样本、任务描述、SQLite 账本和输出版本都落在工作目录内，不依赖 Docker、GPU、音频设备或网络服务。

## 快速开始

Windows 11 / Python 3.11：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
.\run_batch.bat
.\verify.bat
.\test_windows.bat
```

Linux 开发环境：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m voice_repair generate --data-dir data --seed 20261125 --force
.venv/bin/python -m voice_repair validate --task data/task.json
.venv/bin/python -m voice_repair run --task data/task.json
.venv/bin/python -m voice_repair verify --task data/task.json --workers 3
.venv/bin/python -m pytest -q
```

默认输出：

- `data/task.json` 与三条确定性生成 WAV。
- `state/ledger.sqlite`：帧块账本和块输出。
- `state/output/*.wav`：最终音频。
- `state/report.json`：批处理报告。
- `state/verification/verification.json`：串行/并行一致性验证。

## 任务格式

```json
{
  "schema_version": 1,
  "task_id": "game_voice_demo_batch",
  "seed": 20261125,
  "parallelism": 3,
  "round_time_budget_ns": 150000,
  "memory_budget_bytes": 512000,
  "recordings": [
    {
      "id": "voice_16k_dc_pop",
      "wav": "voice_16k_dc_pop.wav",
      "stages": {
        "dc": {"block_frames": 4096, "mode": "mean"},
        "declick": {"block_frames": 4096, "threshold": 0.19, "radius": 2},
        "denoise": {
          "block_frames": 4096,
          "frame_size": 512,
          "hop_size": 128,
          "noise_frames": 10,
          "oversubtraction": 1.35,
          "floor_ratio": 0.035
        },
        "normalize": {"block_frames": 4096, "target_peak": 0.92}
      }
    }
  ]
}
```

`wav` 相对任务 JSON 所在目录解析。每个合法录音必须包含 `dc`、`declick`、`denoise`、`normalize` 四个阶段。非法录音会产生独立的 `E_xxx` 业务码，合法录音继续处理。

主要校验码包括 `E_SAMPLE_RATE`、`E_BIT_DEPTH`、`E_CHANNELS`、`E_WAV_HEADER`、`E_FILE_MISSING`、`E_PARAM_FINITE`、`E_PARAM_RANGE`、`E_BLOCK_FRAMES`、`E_DENOISE_FRAME_SIZE`、`E_NORMALIZE_PEAK`、`E_ID_DUPLICATE` 等。

## 修复链路

1. `dc`：按整段输入均值去除直流偏置，也支持 `mode="fixed"` 与有限 `offset`。
2. `declick`：在时域用固定半径邻域中值检测并替换突发爆音，帧块边界携带完整半径上下文。
3. `denoise`：周期 Hann 窗、全局 STFT 帧网格、前若干帧噪声谱估计、谱减和重叠相加。每个帧块只截取全局帧编号覆盖的输出区间，因此块划分不影响边界累加顺序。
4. `normalize`：扫描整段降噪结果求峰值，按 `target_peak / peak` 统一增益。该阶段只在前一阶段全部块完成后开始。

四阶段浮点块输出与整段串行参考实现逐样本完全相等；最终 PCM16 WAV 也逐字节一致。

## 调度与预算

- 同一录音的块按录音、块序、阶段严格推进；阶段完成后才允许下一阶段。不同录音可由线程池并行处理。
- 每轮按 `(recording_id, block_index, stage)` 确定性排序就绪任务，再受 `parallelism`、`memory_budget_bytes` 和 `round_time_budget_ns` 三重约束准入。
- 内存准入使用按阶段、块采样数和 FFT 帧长计算的确定性预留值；不允许为适配预算而缩短帧长或降低谱减精度。
- 逻辑成本按阶段和实际块采样数计算，与线程完成顺序无关。相同任务配置下，实际工作线程数为 1 或多条时，轮次和准入序列相同。
- 未准入任务记录为 `deferred`，原因包括 `concurrency_limit`、`memory_budget`、`round_time_budget`。

## SQLite 账本与恢复

`block_ledger` 的主键为 `(recording_id, stage, block_index)`。每行在同一事务内包含：

- 采样区间与采样数。
- 浮点块输出 BLOB。
- 输出的 SHA-256 内容摘要。
- 逻辑成本、创建运行 ID 与时间。

数据库采用 WAL、`synchronous=FULL` 和显式 `BEGIN IMMEDIATE`。块输出、区间和摘要原子提交；强制终止最多丢失正在处理但未提交的块，不会出现“有账本行但没有完整块载荷”的状态。恢复时只读取账本中已有块，只调度缺失块，已提交行通过 `INSERT OR IGNORE` 保护，绝不改写其摘要。

阶段完成由“该阶段全部预期块均存在且区间正确”推导，不依赖块处理顺序。账本还会校验任务 JSON 摘要和每条输入 WAV 摘要，防止旧状态被错误复用。

## 终止与输出版本

- 可处理信号：`Ctrl+C`、`SIGTERM`，Windows 下额外尝试 `SIGBREAK`。信号在当前批次的块提交完成后生效，写入 `state/interrupted.json` 和账本 `interruptions` 表，退出码 `130`。
- 强制终止：不承诺报告或状态文件；下次运行仅依赖 SQLite 已提交事务续算。
- 最终 WAV 先写入同目录临时文件并 `fsync`，计算整文件 SHA-256；仅当与旧文件摘要不同时使用 `os.replace` 原子替换，报告中以 `output_changed` 表示是否发生覆盖。

退出码：

| 码 | 含义 |
|---:|---|
| 0 | 全部完成 |
| 2 | 任务/录音存在业务错误，合法部分已处理 |
| 3 | 在当前块大小、时间或内存预算下不可调度 |
| 4 | 账本配置或读写一致性错误 |
| 130 | 可处理终止 |
| 137 | 测试用强制退出钩子，不代表正常交付退出码 |

## 报告与验证

`state/report.json` 包含：

- 每条录音各阶段总块数、完成块数、块摘要和续算补算块数。
- 每轮逻辑成本、准入块、延后块及延后原因。
- 谱减相邻帧块重叠探针的最大绝对误差。
- 归一化增益、最终音频摘要、业务码和汇总。

`verify` 会在独立工作目录分别用 1 个工作线程和多个工作线程运行同一任务，比较：

- 确定性报告视图与完整准入/延后序列。
- 各阶段块 SHA-256。
- 最终 WAV 摘要与原始字节。
- 解码后的逐采样值。

## 实际验证结果

本机环境：Python 3.11.18、numpy 2.4.6、pytest 9.2.0，Ubuntu WSL2 上执行；代码与脚本同时兼容 Windows Python 3.11。

- `pytest -q`：16 项测试全部通过。
- 最终主批次：128/128 个帧块完成，81 轮，33 次延后（27 次单轮时间预算、6 次在途内存预算），128 条账本记录，3 条输出均完成；谱减重叠探针最大绝对误差均为 `0.0`。
- 四阶段整段串行参考对比：三条录音各阶段浮点输出 `max_abs_delta = 0.0`，PCM16 输出逐字节相等。
- 串行/并行验证：`deterministic_report_equal`、`output_digest_equal`、`output_bytes_equal`、`sample_exact_equal`、`ledger_block_digests_equal`、`schedule_equal` 全部为 `true`，最大采样差 `0.0`。
- 强制终止注入：第 10 次账本提交后 `os._exit(137)`，无 `report.json`；10 条行的载荷摘要全部可验证。续算补算 118 块，旧行摘要不变，三条最终 WAV 摘要与不中断基线一致（`state/evidence/crash_resume.json`）。
- 可处理 `SIGTERM`：在 3 个块完整提交后于块边界退出，退出码 `130`，生成 `interrupted.json` 和中断报告；续算补算 125 块后输出与基线一致，成功完成后中断标记被清除（`state/evidence/graceful_signal.json`）。

最终一次端到端运行与验证结果见 `state/report.json` 和 `state/verification/verification.json`。
