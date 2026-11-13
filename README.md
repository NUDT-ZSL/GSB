# 播客对白剪辑工具（project46）

按 JSON 剪辑脚本删除单声道 16 位 WAV 对白中的无效段落，输出精简 WAV 与固定字段 JSON 审核报告。纯 Python 3.10+ 标准库，无第三方依赖。

## 文件

- `trim_audio.py` — 主工具（CLI）
- `make_samples.py` — 生成确定性测试样本（10 秒 16kHz 正弦 WAV、立体声/8 位非法样本、各类剪辑脚本）
- `run_acceptance.py` — 15 条验收测试
- `samples/` — 生成的样本与脚本

## 启动方式

```powershell
python make_samples.py        # 生成样本（首次）
python trim_audio.py 原始.wav 剪辑脚本.json 输出.wav 报告.json [--force]
python run_acceptance.py      # 运行全部验收
```

## 剪辑脚本格式

```json
{"sample_rate": 16000, "intervals": [[1, 2], [4.5, 5.25]]}
```

- `sample_rate` 必须与 WAV 实际采样率一致，否则拒绝执行（非零退出）。
- 区间为 `[起始秒, 结束秒]`，起始须小于结束，秒数最多三位小数（按帧精确换算）。
- 越界区间（下限 0、上限为音频时长）不裁任何音频，单独列入报告 `rejected`。
- 重叠/相邻区间自动合并，不重复删减。
- 非法区间不影响其余有效区间；存在被拒区间时 `complete=false`，已处理结果仍保留。

## 业务操作样例

```powershell
# 正常剪辑（mixed.json 含合法、非法、越界区间）
python trim_audio.py samples\dialog_16k_10s.wav samples\scripts\mixed.json out\trimmed.wav out\report.json
# 输出：摘要: 输出帧数=128000 生效区间数=2 被拒区间数=2

# 采样率不一致被拒绝
python trim_audio.py samples\dialog_16k_10s.wav samples\scripts\sr_mismatch.json out\a.wav out\a.json
# 错误: 采样率不一致: 脚本为 44100 Hz，音频为 16000 Hz，拒绝执行（退出码 1）

# 输出已存在默认拒绝覆盖，--force 可强制
python trim_audio.py in.wav s.json out\trimmed.wav out\report.json --force
```

## 报告字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `output_wav` | string | 输出 WAV 路径 |
| `original_frames` | int | 原始总帧数 |
| `output_frames` | int | 输出帧数 |
| `applied` | array | 实际删减区间（合并后、按时间升序，单位秒） |
| `rejected` | array | 被拒区间及具体原因 |
| `complete` | bool | 全部区间有效且输出成功时为 true |

## 验证结果

`python run_acceptance.py` 实际运行结果：**15/15 全部通过**，覆盖采样率校验、非法/越界区间、重叠合并、参数与帧数一致、坏脚本/坏 WAV 拒绝、报告字段、升序与原因、`complete` 语义、零帧输出、拒绝覆盖、毫秒精度、摘要一致性、报告写失败清理 WAV。
