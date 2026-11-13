# 播客采访录音批量剪辑工具

读取 JSON 剪辑清单(EDL),从 16 位 PCM 单声道 WAV 源文件裁剪片段、可选峰值归一化、按清单顺序拼接为成片 WAV,并生成逐条 JSON 处理报告。仅依赖 Python 3.10+ 标准库,无第三方依赖、无容器。

## 文件

| 文件 | 说明 |
|---|---|
| `podcast_edl.py` | 主程序(CLI) |
| `make_samples.py` | 确定性生成演示样本 WAV 与 EDL |
| `test_podcast_edl.py` | 16 个单元测试,覆盖全部 10 条验收标准 |
| `demo/` | 运行 `make_samples.py` 后生成的样本与输出 |

## 启动方式

```powershell
# 1. 生成演示样本(demo/src/*.wav 与 demo/edl_*.json)
python make_samples.py

# 2. 执行剪辑: python podcast_edl.py <清单.json> <输出目录>
python podcast_edl.py demo/edl_valid.json demo/out_valid
echo $LASTEXITCODE   # 全部成功 -> 0
```

输出目录不存在时自动创建;`output.wav` 与 `report.json` 同名覆盖写入。

## EDL 格式

```json
[
  {"source": "demo/src/interview_16k.wav", "start": 0.0, "end": 1.0, "normalize": true},
  {"source": "demo/src/interview_16k.wav", "start": 1.0, "end": 2.0, "normalize": false}
]
```

每条四个字段:`source`(源文件路径)、`start`(起始秒)、`end`(结束秒)、`normalize`(是否峰值归一化到 29491,即 16 位 90% 满量程)。

## 业务操作样例(均已实际运行验证)

```powershell
# 全合法清单 -> 退出码 0,产出 2 秒成片(16kHz/16位/单声道)
python podcast_edl.py demo/edl_valid.json demo/out_valid      # exit 0

# 混合清单(缺失文件/损坏文件/起点>终点/结束越界/2条合法)
# -> 退出码 1;合法 2 条照常产出,问题条目在报告中如实标注
python podcast_edl.py demo/edl_mixed.json demo/out_mixed      # exit 1

# 8kHz 与 16kHz 混用 -> 整体拒绝,错误含"采样率不一致",不产出半成品
python podcast_edl.py demo/edl_rate_mismatch.json demo/out_rate  # exit 1

# 立体声与 8 位源 -> 条目 error,含"不支持的音频格式"
python podcast_edl.py demo/edl_bad_format.json demo/out_fmt   # exit 1

# 运行测试
python -m unittest test_podcast_edl -v
```

## 处理规则与退出码

- 仅接受 16 位 PCM 单声道 WAV,采样率 8kHz–48kHz;其余格式条目记 `error`,信息含"不支持的音频格式"。
- 缺字段条目记 `error` 并点名字段名;`start >= end`、`end` 超出源时长、`start < 0` 记 `skipped`。
- 源文件不存在/损坏记 `error` 并说明原因,其余条目继续处理直至完成。
- 清单引用的所有可读源必须同一采样率,否则整体拒绝(退出码 1,不写出音频)。
- `normalize: true` 的片段峰值缩放到 29491;`false` 保持原样。
- 退出码:`0` 全部成功;`1` 任一条目 skipped/error(含采样率不一致);`2` 清单本身无法解析。

## 报告格式(report.json)

汇总字段(edl、output、sample_rate、total、success、skipped、error)+ 逐条 `entries`,每条含:

```json
{"index": 0, "source": "...", "status": "success|skipped|error",
 "error": null, "output": "demo\\out\\output.wav"}
```

`success` 条目 `error` 为 `null`;`skipped`/`error` 条目带非空描述且 `output` 为 `null`。

## 实际验证结果(2026-11-14,Python 3.10.20,Windows 11)

- `python -m unittest test_podcast_edl` — 16 个测试全部 OK。
- `edl_valid.json` — 退出码 0;成片 2.0 秒、16000Hz、单声道、16 位;第 1 秒(归一化)峰值 29491,第 2 秒(未归一化)峰值 20000。
- `edl_mixed.json` — 退出码 1;报告 6 条:成功 2、跳过 2(起点>终点、结束越界)、错误 2(文件不存在、文件损坏),合法片段仍拼出成片。
- `edl_rate_mismatch.json` — 退出码 1;stderr 与报告均含"采样率不一致",未生成 output.wav。
- `edl_bad_format.json` — 退出码 1;立体声与 8 位条目均报"不支持的音频格式"。
