# 本地触觉反馈指令流合成引擎

这是一个不依赖真实外设、驱动或网络的确定性示例工程。固定种子脚本生成设备清单、
触觉事件脚本与同步锚点；引擎把事件脚本合成为每台设备独立的驱动指令流，并在帧块
级别持久化、校验和恢复。

## 快速开始

要求 Python 3.11 或以上，以及 NumPy：

```powershell
python -m pip install -r requirements.txt
python generate_samples.py --workspace . --seed 20260214 --force
python run_pipeline.py --workspace .
python verify_results.py --workspace .
```

Windows 也可以直接双击或执行 `run_demo.bat`。如果工作区中的
`devices.json`、`events.json`、`config.json` 全都不存在，`run_pipeline.py`
会使用默认种子自动生成样本。

运行测试：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## 交付内容

- `generate_samples.py`：固定种子设备清单、事件脚本、锚点声明和配置生成器。
- `haptic_engine/models.py`：输入模型与业务校验。
- `haptic_engine/envelope.py`：采样点域分段线性包络与量化。
- `haptic_engine/packing.py`：确定性帧装箱、连续同强度合并与溢出续帧。
- `haptic_engine/budget.py`：全局在途指令预算和按设备标识的公平等待。
- `haptic_engine/state.py`：版本识别、原子块/版本/导出写入、旧状态归档。
- `haptic_engine/sync.py`：同步锚点双单位偏差诊断。
- `haptic_engine/engine.py`：有界线程池合成、恢复、摘要审计与报告。
- `run_pipeline.py` / `haptic_engine/cli.py`：命令行入口和可处理信号处理。
- `verify_results.py`：对导出流、块摘要、逐采样点重建和锚点进行独立复核。
- `tests/`：校验、确定性、并发预算、中断恢复、摘要损坏、版本归档和同步测试。

## 输入契约

### `config.json`

```json
{
  "schema_version": 1,
  "global_concurrency": 3,
  "inflight_command_limit": 4,
  "alignment_tolerance_ms": 0.01,
  "state_dir": "state",
  "export_dir": "export"
}
```

- `global_concurrency`：有界线程池的最大工作线程数，必须为正整数。
- `inflight_command_limit`：全局在途指令条数上限。为避免单个帧块无法入账，
  该值必须不小于任意设备的最大每帧指令数，否则返回 `E_BUDGET`。
- `alignment_tolerance_ms`：默认锚点毫秒容差，必须为非负有限数。
- `state_dir`、`export_dir`：均必须位于工作区内、可写、彼此独立且不可嵌套。

### `devices.json`

```json
{
  "devices": [
    {
      "id": "dev_glove_left",
      "sample_rate_hz": 1000,
      "frame_duration_ms": 10,
      "strength_min": 0.0,
      "strength_max": 255.0,
      "quantization_levels": 256,
      "max_commands_per_frame": 2
    }
  ]
}
```

设备标识必须非空且唯一；采样率、帧时长、每帧最大指令数为正整数；量化档位为
不小于 2 的整数；强度下限必须小于上限。

### `events.json`

```json
{
  "events": [
    {
      "id": "dev_glove_left_sync_0",
      "type": "impact",
      "start_ms": 20,
      "duration_ms": 18,
      "target_device": "dev_glove_left",
      "anchor": "sync_0",
      "envelope": [
        {"offset_ms": 0, "strength": 0},
        {"offset_ms": 9, "strength": 255},
        {"offset_ms": 18, "strength": 0}
      ]
    }
  ],
  "anchors": [
    {
      "id": "anchor_0",
      "marker": "sync_0",
      "reference_device": "dev_glove_left",
      "devices": [
        "dev_glove_left",
        "dev_glove_right",
        "dev_vest_core",
        "dev_wrist_band"
      ],
      "tolerance_ms": 0.01
    }
  ]
}
```

事件起始毫秒必须为非负整数，持续毫秒必须为正整数。包络时刻可以使用
`offset_ms`（也接受 `time_ms`），必须严格递增；强度必须位于目标设备的
`[strength_min, strength_max]` 范围内。事件中的 `anchor` 会把该事件关联到
同名的锚点标记。

## 确定性合成规则

1. 设备采样点区间使用“起始时刻之后或恰在起始时刻的首个采样点”：

   ```text
   start_sample = ceil(start_ms * sample_rate_hz / 1000)
   end_sample   = ceil((start_ms + duration_ms) * sample_rate_hz / 1000)
   ```

   区间为 `[start_sample, end_sample)`；持续采样点数严格等于两者之差。

2. 每个采样点的局部毫秒时间用于分段线性插值。插值结果按量化档位做四舍五入
   到整数级 `0..quantization_levels-1`，不降低档位。事件重叠时逐采样点取最大
   整数级；没有事件的采样点为静音级 0。

3. 每台设备从采样点 0 开始按帧时长切成物理帧。帧内对整数级做游程合并，只有
   同一物理帧内强度级相同且采样点连续的段才合并为一条指令。

4. 物理帧的指令段数超过 `max_commands_per_frame` 时，按顺序把后续完整段放入
   同一来源帧的续帧块。第一块记录 `split_count = 续帧数`，原因为
   `FRAME_COMMAND_LIMIT`；续帧块保留原始 `source_frame_index` 和原始采样点
   区间。任何采样点都不会被截短、重采样或丢弃。

5. 指令的 `sample_start`、`sample_end`、`sample_count`、`level`、`strength`
   仅由事件、设备采样率、强度范围和量化档位决定，不受线程数、预算或提交顺序
   影响。同一输入下，串行与任意合法并发度的逐条指令流完全相同。

## 并发与预算

引擎为每台设备提交一个顺序工作单元到 `ThreadPoolExecutor`，线程数不超过
`global_concurrency`。每个待原子写入的帧块在提交前按指令条数申请全局预算，
块完成原子替换后才释放。

预算不足时，等待队列以 `(device_id, block_index)` 为优先级；低设备标识先获得
可用额度。该规则只改变提交先后，不改变任何设备的指令序列，也不能通过降低
量化精度、截短采样区间或丢指令来满足预算。

## 帧块与恢复

状态目录布局：

```text
state/
  _version.json
  interrupted.json           # 仅优雅中断时存在
  devices/
    <设备安全目录>/
      block_00000000.json
      ...
export/
  commands.json
  report.json
```

每个块包含设备标识、块/帧序号、原始采样范围、已完成采样范围、指令列表、声明
指令条数、拆分计数/原因、`sha256` 摘要和版本标识。所有写入都先在同一目录创建
临时文件并 `fsync`，再用 `os.replace` 整体替换。

版本标识由规范化设备清单摘要、规范化事件/锚点摘要和引擎模式版本共同计算，
不包含并发度、预算、容差或状态路径。启动时：

- 版本一致：逐块校验摘要和声明条数，只重算缺失或无效块。
- 版本不一致：把旧 `state` 和 `export` 目录改名归档，再重做全部设备。
- 全部块完成：再次逐块校验；缺失、摘要不符或条数不符会回退补算，最终报告在
  `business_codes` 中保留 `E_DIGEST` 诊断。无法修复时返回致命 `E_DIGEST`。

块文件整体替换保证强制终止不会留下“半个已完成块”。重启时临时文件会被清理，
只信任摘要和内容均有效的持久化块。

## 同步锚点

对每个锚点标记，每台目标设备取最早声明的对应事件，并以事件起始采样点作为实际
锚点位置。参考设备使用自身采样点时间；其他设备同时报告：

- `actual_sample` 与映射后的 `expected_sample`；
- `sample_delta`（采样点偏差）；
- `actual_time_ms`、`reference_time_ms` 与 `delta_ms`（毫秒偏差）；
- 容差和该采样点所在的指令序号。

任一设备的 `abs(delta_ms)` 超过锚点容差（或全局默认容差）即返回 `E_SYNC`，
详情包含首个越限锚点、设备、采样点/毫秒偏差。不会用平均值掩盖单点越限。

## 中断与退出码

- `SIGINT`：只在帧块边界生效，写 `state/interrupted.json`，退出码 `130`。
- `SIGTERM`：同样只到块边界，退出码 `143`。
- 强制终止：不承诺退出报告或中断标记；重启只恢复摘要有效块。
- 正常完成：退出码 `0`，命令行输出导出路径和摘要统计。
- 业务/配置/输入错误：退出码 `2`，标准输出为 JSON，含 `E_xxx` 业务码。

优雅中断不写最终指令流或报告；恢复后的指令流与不中断一次跑完的结果逐条一致。

## 业务错误码

| 业务码 | 含义 |
| --- | --- |
| `E_JSON` | JSON 无法读取或解析 |
| `E_SCHEMA` | 顶层结构或字段类型错误 |
| `E_DEVICE` | 设备标识空、重复或设备列表为空 |
| `E_RATE` | 采样率非正整数 |
| `E_FRAME` | 帧时长非正整数 |
| `E_MAXCMD` | 每帧最大指令数非正整数 |
| `E_QUANT` | 量化档位小于 2 或非整数 |
| `E_RANGE` | 设备强度范围非法 |
| `E_EVENT` | 事件标识/类型/结构错误 |
| `E_EVENT_START` | 事件起始毫秒非法 |
| `E_EVENT_DURATION` | 事件持续毫秒非法 |
| `E_ENVELOPE` | 包络结构错误 |
| `E_ENVELOPE_TIME` | 包络时刻非递增 |
| `E_ENVELOPE_RANGE` | 包络强度越界 |
| `E_TARGET` | 目标设备不存在 |
| `E_ANCHOR` | 锚点声明非法 |
| `E_CONFIG` | 配置或工作区路径非法 |
| `E_BUDGET` | 在途预算无法容纳单个帧块或预算非法 |
| `E_SYNC` | 锚点缺失或超过对齐容差 |
| `E_DIGEST` | 块摘要/条数/内容校验失败或无法修复 |
| `E_INTERRUPTED` | 被可处理信号中断 |
| `E_IO` | 本地读写失败 |
| `E_INTERNAL` | 确定性内部不变量失败 |

## 报告与导出

`export/commands.json` 按设备导出全部指令、采样范围、强度级和量化强度。
`export/report.json` 至少包含：

- 版本标识、规范化摘要和工作区；
- 每设备采样点/帧块/指令/拆分计数与原因；
- 全部锚点双单位偏差；
- `business_codes`；
- 块摘要校验/修复/缺失补算计数；
- 线程池配置、预算峰值和等待统计。

`verify_results.py` 不信任导出结果，会从当前输入重新规划，并逐块校验摘要、每帧
上限、逐采样点重建、导出指令一致性和锚点诊断。

详细设计见 `TECHNICAL_NOTES.md`，实际验证记录见 `VALIDATION.md`。
