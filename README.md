# 双人播客后期拼接工具（16bit PCM WAV，纯标准库）

把两位主播的单声道 16 位 PCM WAV 按固定偏移拼成对话成品：统一采样率、多声道取第一声道、
压缩长静音、逐段峰值归一化、重叠处饱和限幅，并产出一份可核对来源/标签/跳过原因的 JSON 报告。

- 仅依赖 Python 3.10+ 标准库（`array` / `struct` / `json` / `argparse` / `tempfile` 等），无第三方包、无外部音频工具、不用容器。
- 全部测试素材由代码确定性生成（整数三角波，无随机数），总量约 0.7MB（< 10MB），可重复字节级复现。

## 目录结构

| 文件 | 作用 |
|---|---|
| `podcast_mixer.py` | 主工具：CLI 入口 + 全部处理流水线 |
| `make_samples.py` | 确定性生成测试 WAV 素材与标记文本到 `samples/` |
| `run_demo.py` | 15 条验收标准的端到端自动化测试（子进程调用主工具） |
| `samples/` | 生成的素材（WAV + 标记 txt + 一个假 WAV） |
| `demo_runs/Txx_*/` | 每个验收用例的独立运行目录（含 out.wav / out.json） |

## 一、本地启动（可复现）

```powershell
# 要求 Python 3.10+（已在 Windows Python 3.14 验证）
python --version

# 1) 生成确定性素材（约 0.7MB，写入 samples/）
python -I make_samples.py

# 2) 运行全部 15 项验收（通过退出码 0，任一失败退出码 1）
python -I run_demo.py
```

> `-I` 为隔离模式：忽略环境变量与用户站点包，防止当前/脚本目录被植入 `json.py` 之类的同名模块劫持。

## 二、业务操作样例

### 1. 标准双轨拼接（带标记、显式偏移、覆盖输出）

```powershell
python -I podcast_mixer.py `
  --track-a samples/trackA_44100_mono.wav `
  --track-b samples/trackB_22050_mono.wav `
  --start-a 0 --end-a 246960 `
  --markers samples/markers_demo.txt `
  --output demo_manual/podcast_final.wav --overwrite
```

产物（同一目录，仅两个文件，无临时残留）：

- `demo_manual/podcast_final.wav`：44100Hz / 16bit / 单声道成品
- `demo_manual/podcast_final.json`：报告（每段起止采样、来源轨、标签、动作、跳过原因等）

### 2. 单轨独立成片（另一主播缺轨/废轨时）

```powershell
python -I podcast_mixer.py --track-b samples/trackB_22050_mono.wav --output demo_manual/only_b.wav
```

### 3. 故意让两轨重叠以验证限幅（无回绕爆音）

```powershell
python -I podcast_mixer.py --track-a samples/clipA_44100.wav `
  --track-b samples/clipB_44100.wav --place-b 0 --output demo_manual/clip.wav --overwrite
# 报告 clipping.event_count > 0，样本被饱和钳制在 [-32768,32767]，无符号翻转
```

### 4. 覆盖保护演示

```powershell
python -I podcast_mixer.py --track-a samples/lowpeak_44100_mono.wav --output demo_manual/x.wav
# 再次运行且不加 --overwrite -> 退出码 1，原文件不变
python -I podcast_mixer.py --track-a samples/trackA_44100_mono.wav --output demo_manual/x.wav
```

### 5. 全部可调参数

```powershell
python -I podcast_mixer.py --help
```

| 参数 | 默认 | 含义 |
|---|---|---|
| `--target-rate` | 44100 | 目标采样率下限；两轨不一致时实际取较高者 |
| `--target-peak` | 28000 | 每段归一化目标峰值（满量程 32767） |
| `--silence-threshold` | 500 | `|样本| <=` 该值判为静音 |
| `--min-silence` | 4410 | 触发压缩的最短连续静音采样数（0.1s@44.1k） |
| `--pause-samples` | 13230 | 长静音压缩后保留的短停顿（0.3s@44.1k） |
| `--min-voice` | 256 | 判定极短片段的最小帧数 |
| `--place-b` | 紧接 A 后 | B 轨在成品时间线上的起始位置（可制造重叠） |
| `--overwrite` | 关 | 显式允许覆盖同名 WAV/JSON |

## 三、标记文件格式

每行一条，采样坐标统一为**成品目标采样率**下的采样序号；`#` 开头与空行忽略：

```text
起始采样  结束采样  来源轨(A|B)  [标签文本...]
0 13230 A 开场白
39690 57330 B B主播发言
0 13230 A            # 省略标签 -> 报告 label 为 null
```

非法行（负采样、起止颠倒、非整数、来源轨非 A/B、越界）跳过并在报告 `skipped` 中逐行写明原因，合法行照常生效。

## 四、处理流水线与关键设计

1. **严格 WAV 校验**：逐块解析 RIFF，校验 `fmt/data` 块长度、PCM 标记、16 位、声道数、字节率自洽；非 WAV/损坏抛 `AudioFormatError`，退出码 1，不写任何半成品。
2. **取第一声道**：立体声交错数据按步长取左声道，报告 `downmixed_to_mono=true`。
3. **重采样**：线性插值；整数倍（如 22050→44100）走精确分支，帧数严格按比例翻倍，零帧边界不渗入近零样本。
4. **偏移截断**：起始/结束偏移钳制到 `[0, 帧数]`，每次截断写入 `offset_warnings`。
5. **静音压缩**：识别长静音，内部长静音压成 `--pause-samples` 短停顿，首尾长静音直接裁掉；每次跳过的原始起止采样写入 `silence_cuts`，停顿段在报告中动作记为“跳过”。
6. **逐段归一化**：每个语音段独立按峰值缩放到目标峰值；全静音段峰值为 0 时原样保留，杜绝除零。
7. **饱和限幅**：时间线重叠相加用饱和运算钳制到 `[-32768,32767]`，绝不回绕；每个限幅位置记录原值与钳制值（上限 200 条）。
8. **原子写出 + 读回校验**：先写 `mkstemp` 临时文件，读回校验帧数/数据字节/位宽/RIFF 尺寸后 `os.replace` 原子替换；任何异常清理临时文件并以非零退出。
9. **报告排序**：`segments` 按成品时间线起始采样严格升序（流水线内有自检），跳过记录单独放在 `skipped`，不参与排序。

## 五、15 条验收对照

| # | 验收点 | 自动化用例 |
|---|---|---|
| 1 | 非 WAV/损坏头拒绝且无半成品 | `T01_reject_non_wav`（另有截断头手工验证） |
| 2 | 采样率统一到较高者并记录原始/目标 | `T02_resample_to_higher_rate` |
| 3 | 立体声取第一声道并标注降混 | `T03_downmix_stereo_first_channel` |
| 4 | 越界偏移截断并注明 | `T04_clamp_out_of_range_offsets`（含整体越界） |
| 5 | 5s 静音压成 0.3s 且记录起止采样 | `T05_silence_compression` |
| 6 | 峰值归一化 + 全静音防除零 | `T06_peak_normalization` |
| 7 | 重叠相加限幅、无符号翻转 | `T07_saturating_clipping`（与独立参考逐样本比对） |
| 8 | 标记驱动 source/label | `T08_marker_labels` |
| 9 | 非法标记行跳过、合法行生效 | `T09_skip_invalid_marker_rows` |
| 10 | 全静音轨无效、另一轨独立成片 | `T10_silent_track_invalid_standalone`（逐字节比对） |
| 11 | 输出读回头部/数据/位宽自洽 | `T11_output_roundtrip_verify` |
| 12 | 默认拒绝覆盖、开关后写入 | `T12_overwrite_protection`（校验原文件未改） |
| 13 | 字段完整、null 标签、动作仅合并/跳过 | `T13_report_schema_null_label` |
| 14 | segments 严格升序、乱序标记正确匹配 | `T14_segments_sorted` |
| 15 | 退出码 0、目录仅 1 WAV + 1 JSON、无临时残留 | `T15_end_to_end_clean_dir` |

最近一次全量结果：**15/15 PASS，退出码 0**。
