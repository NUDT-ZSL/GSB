# 播客广告拼接工具（ad_inserter）

把赞助商广告按 JSON 插入计划拼接进已录制的 **16 位 PCM 单声道 WAV** 节目，
输出采样点级衔接的新 WAV 与 JSON 报告。纯 Python 标准库实现，无需第三方依赖。

## 1. 环境要求

- Python 3.10+（开发与验证环境：Windows 11 + Python 3.10.20）
- 不使用 Docker / 容器；不访问任何网络目标
- 建议用 `python -I`（隔离模式）运行，避免当前目录下被植入的同名模块（如假 `json.py`）干扰

## 2. 目录结构

```
project52/
├─ src/
│  ├─ ad_inserter.py    # 主工具（校验 + 拼接 + 报告 + 命令行）
│  ├─ make_samples.py   # 确定性生成节目/广告 WAV、插入计划、非法样本、占位文件
│  └─ verify.py         # 端到端验收脚本（37 项检查，含独立参考实现逐采样点比对）
└─ demo/                # 运行 make_samples.py 后生成（已加入 .gitignore，可随时重建）
   ├─ input/            # program.wav / ad.wav / short_program.wav / 立体声与异采样率非法样本
   ├─ plans/            # 5 份计划：正常、含无效项、空间不足、缺字段、时间非数字
   └─ output_keep/      # 占位文件 placeholder.txt，用于验证失败时不破坏已有文件
```

## 3. 可复现的本地启动方式

PowerShell（当前工作目录为项目根目录）：

```powershell
# ① 生成确定性样本（固定波形公式，重复运行字节级一致）
python -I src\make_samples.py --root demo

# ② 一键运行全部 37 项验收（自动建临时目录，结束后清理）
python -I src\verify.py

# ③ 业务运行：路径含空格也正常；建议先 chcp 65001 让控制台中文按 UTF-8 显示
chcp 65001
python -I src\ad_inserter.py `
  --program "demo\input\program.wav" `
  --ad      "demo\input\ad.wav" `
  --plan    "demo\plans\plan_valid.json" `
  --outdir  "demo\out 演示目录"
```

CMD / Git Bash 下把反斜杠换成正斜杠、去掉 PowerShell 的续行反引号即可。

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--program` | 节目 WAV 路径（16-bit PCM、单声道） |
| `--ad` | 广告 WAV 路径（要求同上，且采样率与节目一致） |
| `--plan` | 插入计划 JSON 路径 |
| `--outdir` | 输出目录（自动创建；可含空格） |

成功时标准输出打印摘要，含**插入段数**与**输出 WAV/报告的绝对路径**，退出码 0；
失败时错误信息打印到标准错误，退出码 1。

## 4. 插入计划 JSON 格式

```json
{
  "insertions": [
    {"time_sec": 2.0, "repeats": 1},
    {"time_sec": 8.5, "repeats": 2}
  ]
}
```

- `time_sec`：插入点（秒，从节目开头计），必须是有限数字，`0 <= time_sec <= 节目时长`
- `repeats`：重复次数，必须是**正整数**（布尔值、0、负数、小数均判无效）

校验分两级：

| 级别 | 情形 | 行为 |
|---|---|---|
| 致命错误，立即终止 | JSON 无法解析、缺 `insertions`、项缺 `time_sec`/`repeats`、`time_sec` 非数字、音频格式不符 | 打印具体错误，**不创建任何输出文件**，目录中已有同名文件保持原样（先写 `*.tmp` 再 `os.replace` 原子替换） |
| 逐项跳过 | 插入点为负、越界、次数非正整数 | 写入报告 `skipped_invalid_items`（含计划项下标与原因），其他有效项照常处理 |

## 5. 拼接语义（关键设计）

1. **采样点级衔接**：直接在 PCM 采样数组上切片拼接，不重采样、不引入静音采样、无重叠。
2. **输出时长不超过原节目**：广告插入后挤占等长的节目尾部内容；当从插入点到节目末尾
   的剩余空间放不下完整广告时，**整段跳过**（永远不会输出半段广告）。
   报告 `truncated_program_tail_samples` 记录被挤占的节目尾部采样数，
   `insertion_requests` 逐项给出 `requested_repeats / actual_inserted / not_inserted`。
3. 调度按插入位置排序处理（避免后处理的靠前插入点把先前广告推出边界），
   但**报告顺序与广告编号严格按计划中有效项的出现顺序**（编号从 1 起）。
4. 起止采样点索引从 0 开始；`end_sample` 为该广告最后一个采样点的索引（含尾），
   `sample_count = end_sample - start_sample + 1`。
5. 输出 WAV 的采样率、位深、声道数与输入完全一致。

## 6. 报告 JSON 字段

```json
{
  "sample_rate_hz": 8000,
  "bits_per_sample": 16,
  "channels": 1,
  "program_total_samples": 160000,
  "ad_total_samples": 16000,
  "output_total_samples": 160000,
  "inserted_segment_count": 4,
  "truncated_program_tail_samples": 64000,
  "insertion_requests": [
    {"plan_index": 0, "plan_time_sec": 2.0, "insert_sample_in_program": 16000,
     "requested_repeats": 1, "actual_inserted": 1, "not_inserted": 0, "ad_numbers": [1]}
  ],
  "insertions": [
    {"ad_number": 1, "plan_index": 0, "plan_time_sec": 2.0, "copy_index": 1,
     "insert_sample_in_program": 16000,
     "start_sample": 16000, "end_sample": 31999, "sample_count": 16000}
  ],
  "skipped_invalid_items": []
}
```

## 7. 业务操作样例（验收对照）

样本参数：节目 20.0 s（8000 Hz × 20 s = 160000 采样点，220 Hz 正弦）；
广告 2.0 s（16000 采样点，880 Hz 正弦 + 确定性扰动，与节目波形可区分）。

```powershell
chcp 65001

# 正常计划：2.0s×1、8.5s×2、15.0s×1 → 4 段广告
python -I src\ad_inserter.py --program "demo\input\program.wav" --ad "demo\input\ad.wav" `
  --plan "demo\plans\plan_valid.json" --outdir "demo\out 演示目录"

# 含无效项（负时间/越界/2.5 次/0 次）：4 项跳过，2 个有效项共 4 段照常插入
python -I src\ad_inserter.py --program "demo\input\program.wav" --ad "demo\input\ad.wav" `
  --plan "demo\plans\plan_invalid_items.json" --outdir "demo\out 无效项演示"

# 空间不足：短节目 3 s；0.5s 处插入 1 次成功，2.5s 处剩 0.5s 放不下 2s 广告 → 实际 0/未插入 3
python -I src\ad_inserter.py --program "demo\input\short_program.wav" --ad "demo\input\ad.wav" `
  --plan "demo\plans\plan_overflow.json" --outdir "demo\out 空间不足"

# 非法音频：立体声 / 16k 广告 → 退出码 1，输出目录无新文件
python -I src\ad_inserter.py --program "demo\input\stereo_program.wav" --ad "demo\input\ad.wav" `
  --plan "demo\plans\plan_valid.json" --outdir "demo\out 非法"
python -I src\ad_inserter.py --program "demo\input\program.wav" --ad "demo\input\rate16k_ad.wav" `
  --plan "demo\plans\plan_valid.json" --outdir "demo\out 非法"

# 致命计划错误：缺字段 / 时间非数字 → 退出码 1、无新文件、占位文件原样保留
python -I src\ad_inserter.py --program "demo\input\program.wav" --ad "demo\input\ad.wav" `
  --plan "demo\plans\plan_missing_field.json" --outdir "demo\output_keep"
```

## 8. 实际验证结果

`python -I src\verify.py` 实测 **37/37 项全部通过**，覆盖任务全部 8 条验收：

1. 立体声 WAV 报“声道数为 2，仅支持单声道”；16 kHz 广告报“采样率不一致”；两次运行输出目录均零新增文件。
2. 含负值（-2s）、越界（99s > 20s）、非整数次数（2.5）、零次数（0）的计划：4 项写入 `skipped_invalid_items` 并附原因，有效项插入不受影响。
3. 用独立参考实现（另一套切片调度写法）对输出做**逐采样点比对完全一致**；报告区间内逐点等于广告公式波形，区间前后衔接点等于节目连续波形；最长连续零采样为 1，无静音填充；广告区间两两不重叠。
4. 报告条目数量、顺序、编号（1 起）、`start_sample/end_sample` 与参考实现及输出文件逐一相符。
5. 短节目场景输出严格等于 24000 采样点（未超原时长）；报告显式记录第一项 `actual=1/not_inserted=0`、第二项 `actual=0/not_inserted=3`。
6. 输出 WAV 头部实测 8000 Hz / 16-bit / 单声道 / PCM，与输入一致；`output_total_samples` 与文件实际帧数相等。
7. 含空格目录 `out 空格 目录` 处理成功；标准输出摘要含“插入段数: 4”及输出绝对路径。
8. 缺字段报“第 1 项 缺少必需字段 'repeats'”、时间非数字报“time_sec 必须是数字”；预置的同名 `output.wav`/`report.json` 与 `placeholder.txt` 哈希前后不变，目录无新增文件。
