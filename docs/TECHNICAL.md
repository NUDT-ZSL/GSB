# 技术说明

## 1. 运行边界

- 入口：`python -m hdr_color <task.json>`，Windows 也可用 `python run_task.py <task.json>`。
- 运行环境：Python 3.11+，仅标准库；不需要 NumPy、网络、真实素材或容器。
- 所有路径相对任务 JSON 所在目录解析。
- 验证样本由 `generate_samples.py` 使用固定种子生成，并在任务清单中记录 SHA-256。
- 程序把输入帧解释为“源线性光 RGB，`1.0` 对应任务参考白”；三通道均为非负有限浮点数。

## 2. 任务格式

```json
{
  "schema_version": "hdr-color-task-v1",
  "task_id": "example",
  "frames_path": "frames.json",
  "output_dir": "out",
  "source": {
    "transfer": "PQ",
    "gamut": "BT2020",
    "white_point": "D65"
  },
  "target": {
    "transfer": "SDR_GAMMA",
    "gamut": "BT709",
    "white_point": {"name": "D65", "x": 0.3127, "y": 0.3290}
  },
  "luminance": {
    "reference_white_nits": 100.0,
    "source_peak_nits": 1000.0,
    "target_peak_nits": 100.0
  },
  "tone_mapping": {
    "curve": "reinhard_extended",
    "white_nits": 1000.0,
    "exposure": 1.0
  },
  "statistics": {
    "window_frames": 4,
    "tail_mode": "include"
  },
  "processing": {
    "chunk_frames": 3,
    "concurrency": 2,
    "memory_budget_bytes": 67108864,
    "curve_lut_size": 65537
  },
  "injections": []
}
```

帧文件：

```json
{
  "schema_version": "hdr-color-frames-v1",
  "frames": [
    [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
    [[4.0, 2.0, 0.5], [0.001, 0.002, 0.003]]
  ]
}
```

允许的传递函数：`SDR_GAMMA`、`HLG`、`PQ`。允许的色域：`BT709`、
`DISPLAY_P3`、`BT2020`、`ACES_AP0`、`ACES_AP1`。白点可用
`D65`、`D60`、`D50`、`DCI`、`E` 或 `{x,y}`。

约束：

- 参考白、源峰值、目标峰值均为有限正数，且两个峰值不小于参考白。
- PQ 目标峰值不得超过 ST 2084 的 10000 cd/m2 定义域。
- `reinhard_extended` 的 `white_nits >= target_peak_nits`，`exposure > 0`。
- `window_frames > 0`，`chunk_frames > 0`，`concurrency > 0`，
  `curve_lut_size >= 1025`。
- 所有帧像素数一致，每个像素恰好三通道，禁止 NaN/Inf、负数、布尔数值和重复 JSON 键。

## 3. 传递函数

报告中逐项列出以下常数和所用曲线。

### SDR

采用 ITU-R BT.709 OETF，精确连续性常数：

```text
alpha = 1.09929682680944
beta  = 0.018053968510807
slope = 4.5
gamma = 0.45

E' = 4.5L                               , L < beta
E' = alpha * L^gamma - (alpha - 1)      , L >= beta
```

逆变换使用 `beta_signal = 4.5 * beta` 分段，绝对亮度为相对线性值乘以任务参考白。

### HLG

采用 ITU-R BT.2100 HLG OETF：

```text
a = 0.17883277
b = 0.28466892
c = 0.55991073

E' = sqrt(3E)                     , 0 <= E <= 1/12
E' = a*ln(12E-b)+c                , E > 1/12
```

绝对解码使用 BT.2100 OOTF：

```text
Ys = 0.2627 R + 0.6780 G + 0.0593 B
Fd = alpha * Ys^(gamma-1) * E
alpha = 任务峰值亮度
gamma = clip(1.2 + 0.42*log10(alpha/1000), 1.2, 1.4)
```

其逆变换为解析解；本实现不建模 HLG 黑电平抬升项。

### PQ

采用 SMPTE ST 2084：

```text
m1 = 2610/16384
m2 = 2523/4096 * 128
c1 = 3424/4096
c2 = 2413/4096 * 32
c3 = 2392/4096 * 32

L  = 10000 * (max(E^(1/m2)-c1, 0) / (c2-c3*E^(1/m2)))^(1/m1)
```

PQ 使用解析高精度路径；其共享 LUT 仍构建并作为可选预览表，避免在暗部引入查表量化偏差。

SDR/HLG 的共享 LUT 只读，并在共享对象中仅构建一次。报告给出曲线常数、
分段连续性、往返最大绝对/相对误差以及 LUT 大小和共享状态。

## 4. 色域与白点

RGB 到 XYZ 矩阵完全由任务色域的三基色色度坐标和任务白点计算：

1. 把每个主色的 `(x,y)` 转为 `(X,1,Z)`。
2. 用主色矩阵的逆和归一化白点 XYZ 求三通道缩放。
3. 构造源/目标 RGB-to-XYZ 及 XYZ-to-RGB。
4. 用 Bradford 矩阵把源白点适配到目标白点。
5. 组合为“源 RGB -> 源 XYZ -> 目标白点适配 -> 目标 RGB”。

报告给出全部矩阵、目标亮度系数、组合矩阵和矩阵往返最大误差。

负 RGB 通道视为超出目标色域。映射采用保持亮度的向中性色消饱和：

```text
C' = Y + a(C-Y)
```

选择最大的 `a in [0,1]` 使所有通道非负。每帧和总体报告都会给出
`out_of_gamut_pixel_ratio` 与 `gamut_desaturated_pixel_ratio`。

## 5. 色调映射与回压

默认曲线为扩展 Reinhard：

```text
x = Y * exposure / target_peak
w = white_nits / target_peak
y = x * (1 + x/w^2) / (1 + x)
Y' = min(y, 1) * target_peak
```

也支持 `linear_clip`。映射只对亮度施加单一缩放：

```text
RGB' = RGB * (Y' / Y)
```

因此映射阶段不改变三通道比例。之后逐通道确定性钳位到
`[0, target_peak_nits]`，并记录：

- `channel_clipped_pixel_ratio`
- `luminance_retention_ratio`
- `mapping_luminance_conservation`
- 黑像素数与色域消饱和比例

亮度为 0 时直接输出全黑；任意大的有限输入也有确定的有界结果。目标编码信号
若因 SDR 定义域产生超出 `[0,1]` 的通道，钳位并计数
`encoded_signal_clipped_channel_ratio`。

## 6. 统计

- 逐帧：亮度均值、峰值、超目标峰值像素比例、色域外比例、消饱和比例、
  通道回压比例、映射前后亮度均值和亮度保留率。
- 窗口：`[start_frame, end_frame_exclusive)`。
- `tail_mode=include` 保留末尾不足一窗的帧；`drop` 丢弃。
- 亮度使用目标色域 RGB-to-XYZ 矩阵的 Y 行，因此与目标色域一致。
- 所有亮度均值/峰值/超峰值比例只从转换后的最终目标线性光帧计算，不使用
  映射前中间量；映射前后数值仅作为独立诊断字段。
- 注入失败帧保持原始帧序号，但不进入亮度统计；窗口同时报告有效/无效帧数。

## 7. 异常注入

注入在内存副本上执行，不修改任务或帧文件。支持：

| 类型 | 帧级业务码 | 行为 |
|---|---|---|
| `NAN` | `E_INJECT_NAN` | 指定帧首像素指定通道置 NaN |
| `NEGATIVE` | `E_INJECT_NEGATIVE` | 置为负值 |
| `PEAK_OVERFLOW` | `E_INJECT_PEAK_OVERFLOW` | 放大到超过源峰值 |
| `BAD_TRANSFER` | `E_INJECT_BAD_TRANSFER` | 调用非法传递函数解析并捕获 |

受影响帧输出 `status=error`，记录所有注入记录；其余合法帧继续处理。报告汇总
注入位置、错误码和最小错误帧号 `first_error_frame`。

## 8. 并发、内存和确定性

- `ThreadPoolExecutor` 的 worker 数取任务并发数。
- 提交队列严格有界：同时提交数不超过
  `min(concurrency, memory_budget / estimated_chunk_bytes)`。
- 若预算不足以容纳配置的帧块，帧块会缩小；仍不足一帧则返回
  `E_MEMORY_BUDGET`。
- 内存估算按每像素 768 字节并计入共享矩阵/LUT；报告记录有效帧块大小、
  有效并发、峰值在途帧块数与峰值字节估算。
- 曲线 LUT 和矩阵只在共享对象中构建一次，worker 只读。
- 每个帧块独立、按帧索引拼装，因此并发度 1 和 4 的帧与统计逐元素相同。
- 任一帧块失败会取消未开始的块、等待已运行块到达帧块边界，并返回最小块号的
  首个错误。

## 9. 中断、恢复与原子写出

- 捕获 `SIGINT`、`SIGTERM`，只设置停止事件。
- 正在运行的帧块完成后到达边界；之后不再提交新块。
- 完整帧块以同目录临时文件 + `fsync` + `os.replace` 持久化到
  `.hdr-color-work/chunk-NNNNNN.json`。
- 中断写 `run-state.json`，`status=interrupted`，含信号、退出码和已完成块。
- 恢复时扫描持久化块并校验任务版本、块起止和帧数；不依赖内存状态。
- `SIGINT` 退出码 130，`SIGTERM` 退出码 143；其他业务错误为 2。
- 最终 `converted_frames.json` 和 `statistics_report.json` 均先写同目录临时文件
  再替换。`run-state.json` 最后原子标记 `complete`，作为成对提交的完成门。
- 开始替换前读取已有两份输出的帧数、参数摘要和版本；相同版本且帧数相同则
  返回 `unchanged`，不重写。只有版本不同才替换。
- 版本摘要包含引擎版本、规范化任务参数和帧文件 SHA-256。

手动中断测试：

```bash
python -m hdr_color samples/task_pq-injection-chain.json --test-chunk-delay-ms 100
# POSIX: kill -TERM <pid>; Windows: Ctrl+C
```

随后不带延迟再次运行同一命令即可从持久化块恢复。

## 10. 业务错误

任务/帧校验错误向 stdout 输出：

```json
{
  "status": "error",
  "error": {
    "code": "E_TRANSFER_FUNCTION",
    "message": "...",
    "details": {}
  },
  "exit_code": 2
}
```

常见全局码：`E_JSON_PARSE`、`E_TASK_SCHEMA`、`E_TRANSFER_FUNCTION`、
`E_GAMUT`、`E_WHITE_POINT`、`E_LUMINANCE`、`E_TONE_MAPPING`、
`E_STATS_CONFIG`、`E_PROCESSING_CONFIG`、`E_FRAMES_SCHEMA`、
`E_FRAME_WIDTH`、`E_FRAME_NONFINITE`、`E_FRAME_NEGATIVE`、
`E_INJECTION_SCHEMA`、`E_MEMORY_BUDGET`、`E_OUTPUT_DIR`、`E_IO`、
`E_TRANSFER_ROUNDTRIP`、`E_CHECKPOINT`、`E_FRAME_PROCESSING`、
`E_CHUNK_FAILURE`、`E_ASSEMBLY`、`E_INTERNAL`。

基础输入校验失败发生在输出目录创建之前，因此不会产生输出文件。

## 11. 代码布局

- `hdr_color/curves.py`: 三条曲线、LUT、连续性/往返诊断。
- `hdr_color/color.py`: 色度矩阵、Bradford、色域检测和消饱和。
- `hdr_color/tone.py`: 色调映射和逐通道回压。
- `hdr_color/task.py`: 任务/帧 JSON 解析与校验。
- `hdr_color/pipeline.py`: 单帧/帧块转换与注入。
- `hdr_color/processor.py`: 有界并发、检查点、恢复与最终提交。
- `hdr_color/storage.py`: 原子 JSON 写出及已存在输出元数据检查。
- `generate_samples.py`: 固定种子样本生成。
- `tests/`: 曲线、矩阵、色调、校验、统计、并发、原子性、注入和信号测试。
