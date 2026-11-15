# 多轨 WAV 离线转码与采样级对齐混音工具

本地命令行工具：读取多条单声道 PCM WAV（8k/16k/44.1kHz、16/24bit）与
JSON 时间轴清单，在**目标采样率域**内完成重采样、位深归一、裁剪、对齐、
增益与混音，输出统一格式 WAV 与一次对齐报告。全程离线，无外部账号/网络。

## 环境

- Python 3.10+，依赖 `numpy`、`soundfile`（自带 libsndfile，PCM_16/PCM_24 可读写）。
- 本工作区实际版本：Python 3.10.12 / numpy 2.2.6 / soundfile 0.14.0 / libsndfile 1.2.2。

## 运行

```bash
python tests/make_samples.py           # 生成确定性样本与清单
PYTHONPATH=. python -m multitool.cli samples/timeline.json
```

进程退出码映射到业务码（见 `multitool/errors.py`），报告 JSON 同时打印到 stdout，
并原子写入清单 `report` 字段指定的文件。

## 清单格式

```json
{
  "target_sample_rate": 44100,
  "target_bit_depth": 16,
  "output": "samples/out/mix.wav",
  "report": "samples/out/report.json",
  "tracks": [
    {"file": "wav/a.wav", "offset": 1000, "gain": 0.5,
     "crop_start": 0, "crop_end": 1234}
  ]
}
```

- 所有时间轴字段（`offset`、`crop_start`、`crop_end`）都是**目标采样率帧索引**。
- `offset` 可为负：负偏移对应轨头部落在帧 0 之前，被裁掉；正偏移前补静音。
- `crop_end` 可省略，表示用到转换后轨的末尾。
- 轨 `file` 相对清单目录解析；`output`/`report` 相对调用工作目录解析，绝对路径亦可。

## 模块结构

- `multitool/pcm.py`：`wave` 读头校验（采样率/位深/单声道/无压缩）；24bit
  手写小端三字节解析并符号扩展，再除以满量程转 32bit float 中间表示；记录输入截断。
- `multitool/resample.py`：有理数比 `P/Q` 多相重采样。稠密高速网格窗函数 sinc
  原型低通，按模 `P` 拆成多相，每相单位直流增益；输出长度 `round(n·P/Q)`。
- `multitool/manifest.py`：清单存在性、JSON 语法与 schema 校验。
- `multitool/mixer.py`：线性增益、最终混音软限幅（单位带通内直通，带外 tanh）。
- `multitool/atomic.py`：同目录临时文件 + 备份/回滚的双文件整体替换。
- `multitool/engine.py`：端到端流水线与报告组装；`multitool/cli.py` 命令行入口。

## 关键语义

- **真值统一在目标采样率帧域**：报告中的 `start_frame/end_frame/converted_frames`
  全部是目标率帧索引，与时间轴真值一致。
- **测量点**：`post_gain_peak` 在每轨增益之后、混音之前测量；
  `mix_peak_pre_limit` 是逐帧求和后、软限幅前；`mix_peak_final` 是软限幅后。
  软限幅只作用于最终混音，单轨不做限幅。
- **位深**：输入 16/24bit PCM 统一转 float；输出按 `target_bit_depth` 量化为
  PCM_16 或 PCM_24。
- **原子性**：WAV 与报告先各自写同目录临时文件；替换前读取旧 WAV 的帧数与采样率，
  版本相同则两者都不替换，版本不同则备份后整体替换，任一失败回滚保留旧文件并返回
  `E_WRITE`。任一轨失败不会出现半成品混音污染正式输出。

## 业务码

| 码 | 含义 | 退出码 |
|----|------|--------|
| E_OK | 成功 | 0 |
| E_MISSING | 清单或轨文件缺失 | 2 |
| E_SCHEMA | 清单字段缺失/非法 JSON | 3 |
| E_RATE | 目标采样率不在允许集合 | 4 |
| E_FORMAT | 轨采样率/位深/声道不合规（隔离到该轨） | 5 |
| E_EMPTY | 所有轨失败或对齐后无有效音频 | 6 |
| E_WRITE | 输出写入/替换失败，旧文件保留 | 7 |

轨级部分失败时顶层 `status` 为 `E_PARTIAL`、进程返回 5；每轨带独立 `status`。

## 验证结果

测试：`python -m unittest tests.test_pipeline`（15 个用例，全部通过）。

- 24bit 手写小端解析往返、满量程截断标记；立体声/非法采样率/32bit 拒绝为 E_FORMAT。
- 多相重采样输出长度四舍五入，允许的三种率任意组合下与理论值最大误差 0.5 个目标帧（≤1）。
- 冲激锁定在帧 0（t=0 对齐）；直流信号内部增益偏差约 1e-16。
- 端到端冲激轨 `offset=5000`、冲激位于输入帧 100，混音峰值精确落在目标帧 5100。
- 负偏移轨报告 `start_frame=0`；版本一致二次运行 `output_replaced=false`。
- 软限幅仅作用最终混音：两路满轨求和前峰值 2.0，限幅后 <1.0，单轨峰值仍为 1.0。
- E_MISSING/E_SCHEMA/E_RATE/E_FORMAT/E_EMPTY/E_WRITE 退出码逐一复现；
  E_WRITE 场景旧 WAV 大小不变；运行后无 `.tmp`/`.bak` 残留。
