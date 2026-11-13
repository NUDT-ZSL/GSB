# 播客 WAV 批量编辑工具（podcast-editor）

面向播客制作人的单声道 16-bit PCM WAV 批处理工具：按编辑指令 JSON 依次完成
**裁剪（crop）/ 拼接（splice）/ 音量调整（volume）**，输出合并 WAV 与结构化
编辑报告。纯 Python 标准库实现，无第三方依赖，无需容器。

## 1. 环境要求

- Python 3.10+（开发验证版本：3.10.20，Windows 11）
- 仅使用标准库：`wave`、`array`、`json`、`argparse`、`subprocess` 等
- 输入约束：最多 10 个 WAV，每个 ≤ 2MB、≤ 30 秒；须为单声道 16-bit PCM 且采样率一致

## 2. 目录结构

```
project51/
├─ podcast_editor/          # 核心包
│  ├─ __init__.py
│  ├─ errors.py             # 错误码与 EditError（结构化错误）
│  ├─ wavio.py              # WAV 读取/校验/写出（wave + array）
│  ├─ engine.py             # 编辑引擎：时间线、三指令、检查点续跑
│  ├─ report.py             # 编辑报告组装与写出
│  └─ cli.py                # 命令行入口 python -m podcast_editor.cli
├─ scripts/
│  ├─ generate_samples.py   # 确定性生成演示 WAV（含立体声非法样本）
│  └─ run_demo.py           # 端到端验收脚本（覆盖全部 11 项验收点）
├─ tests/
│  └─ test_editor.py        # unittest 单元/集成测试
├─ examples/
│  └─ edits.example.json    # 含三种指令的示例指令文件
└─ demo/                    # 运行后生成：input 样本 + output 各场景产物
```

## 3. 可复现的本地启动方式

所有命令在项目根目录 `project51/` 下执行。

### 3.1 一键端到端验收（推荐先跑）

```powershell
python -I scripts/run_demo.py
```

脚本会自动：生成确定性输入样本 → 构造 6 组场景（A~F）→ 子进程调用 CLI →
用 `wave` 读回输出逐项断言 → 打印 `[PASS]/[FAIL]` 清单。退出码 0 即全部验收通过。

### 3.2 单元测试

```powershell
python -I tests/test_editor.py
```

### 3.3 手动业务操作样例

```powershell
# 1) 生成输入样本到 demo/input
python -I scripts/generate_samples.py

# 2) 准备指令文件（参考 examples/edits.example.json，源路径相对指令文件解析）
# 3) 执行编辑（run_cli.py 是 CLI 入口垫片，隔离模式下也可直接运行；
#    非隔离环境同样可使用 python -m podcast_editor.cli）
python -I scripts/run_cli.py `
  --instructions examples/edits.example.json `
  --output demo/output/manual/episode.wav `
  --report demo/output/manual/report.json
```

CLI 退出码：`0` 全部成功（或指令数组为空）；`2` 部分失败但已产出可用 WAV；
`3` 全部失败无音频产出；`1` 指令文件本身非法。

## 4. 指令 JSON 规范

顶层为 JSON 数组，按数组顺序依次执行，后一条指令的输入是前一条的输出。

| 指令 | source | params | 语义 |
|---|---|---|---|
| `crop` | 必填 | `start`,`end`（秒，浮点） | 截取来源 `[start,end)` 片段，**替换**当前时间线 |
| `splice` | 必填 | `at`（必填）；`start`,`end` 可选 | 在时间线 `at` 秒处插入来源（或其片段），后续内容自动后移；`at` 等于当前时长即尾部追加 |
| `volume` | 可空 | `gain_db`（必填，-60~+60）；`start`,`end` 可选 | 对整条时间线（缺省）或指定区间施加分贝增益 |

说明：
- 时间在内部按采样率四舍五入换算为整数帧；浮点误差容忍 1 个采样间隔。
- 音量按 `factor = 10^(gain_db/20)` 缩放样本幅度，结果钳位到 `[-32768, 32767]`
  防止削波溢出，报告 `detail` 中给出被钳位的样本数。
- 失败指令只记录错误并跳过，**时间线状态保持不变**，继续执行下一条。

示例片段：

```json
[
  {"type": "crop",   "source": "voice_mono.wav", "params": {"start": 2.0, "end": 7.0}},
  {"type": "splice", "source": "ad_mono.wav",    "params": {"at": 1.0}},
  {"type": "volume", "source": null,             "params": {"gain_db": -6.0}}
]
```

## 5. 编辑报告结构

`report.json` 关键字段：

- `overall_status`：`complete`（全成功）/ `incomplete`（部分失败但 WAV 可用）/
  `failed`（无任何音频产出）/ `empty`（指令数组为空）
- `summary`：指令总数、成功/失败数、续跑跳过数、总帧数、总时长、采样率
- `output`：输出 WAV 信息（声道/位深/采样率/帧数/时长）；无产出时
  `exists=false` 并显式给出 `reason`，不把缺失数据伪装成空结果
- `unavailable_sources`：始终不可用的来源清单及其错误
- `instructions[]`：每条指令的 `status`、`applied`、`resumed_skipped`、
  成功 `detail` 或失败 `error {code, message, retryable}`
- 总时长与输出 WAV 的实际帧数严格一致（验收脚本用 `wave` 读回核对）

## 6. 错误码（稳定、可程序化处理）

| 错误码 | 触发场景 | 可重试 |
|---|---|---|
| `INSTRUCTION_FILE_INVALID` | 指令 JSON 不存在/无法解析/顶层非数组 | 否 |
| `INVALID_INSTRUCTION` | 单条指令字段缺失、类型错误、时间线为空等 | 否 |
| `UNKNOWN_INSTRUCTION_TYPE` | 非 crop/splice/volume 的类型 | 否 |
| `SOURCE_NOT_FOUND` | 引用的来源文件不存在 | 是 |
| `SOURCE_NOT_READABLE` | 文件暂时不可读/占用/容器损坏 | 是 |
| `INVALID_WAV_FORMAT` | 非单声道、非 16-bit PCM、非 PCM 压缩 | 否 |
| `SAMPLE_RATE_MISMATCH` | 来源采样率与时间线采样率不一致 | 否 |
| `TIME_OUT_OF_RANGE` | 裁剪时间越界、起止非法、区间为空 | 否 |
| `INSERT_OUT_OF_RANGE` | 拼接插入点超出时间线 `[0, 时长]` | 否 |
| `GAIN_OUT_OF_RANGE` | 增益超出 `[-60, +60]` dB | 否 |
| `INTERNAL_ERROR` | 未预期异常的兜底结构化记录 | 视情况 |

## 7. 断点续跑（临时性失败重试，验收点 7）

每条指令处理完立即把时间线与已成功指令索引原子写入检查点
（`--work-dir` 下 `checkpoint.json`，默认与报告同目录）。

- 重新运行（不带 `--fresh`）：历史已成功指令标记 `resumed_skipped` 直接跳过，
  **不会重复应用、不会污染输出**；仅重试此前失败的指令。
- 检查点与当前指令集签名不一致（指令被修改）时自动失效，按全新运行处理。
- `--fresh`：忽略检查点，清空时间线从头执行。
- 场景 E 演示了「移走 ad_mono.wav → 首跑该指令失败 → 恢复文件后续跑 →
  仅补做该指令，前 3 秒逐样本不变」。

## 8. 验收点与演示场景对照

| 验收点 | 覆盖位置 |
|---|---|
| 1 三指令解析执行 | 场景 A、examples/edits.example.json |
| 2 立体声跳过、单声道正常 | 场景 B、单元测试 `test_stereo_source_rejected` |
| 3 裁剪越界跳过 | 场景 A 指令 3、单元测试 `test_crop_time_out_of_range` |
| 4 拼接插入点校验 | 场景 C、单元测试 `test_insert_point_out_of_range` |
| 5 增益范围与防削波 | 场景 D、单元测试 `test_gain_boundaries` / `test_clipping_prevention` |
| 6 报告结构与完整/不完整区分 | 全部场景、场景 F（无产出显式声明） |
| 7 临时失败续跑不重复应用 | 场景 E、单元测试 `test_retry_after_transient_failure` |
| 8 持续失败来源清单与成功数据完整 | 场景 A 指令 6、单元测试 `test_partial_failure_keeps_successful_output` |
| 9 输出 WAV 格式与时长 | 每个场景均用 `wave` 读回核对参数/帧数/时长 |
| 10 顺序执行、失败不改变时间线 | 场景 A 内容核对、场景 C 时长核对 |
| 11 结构化错误码 + 可读消息 | 全部失败断言均检查 `code` 与 `message` |

## 9. 安全说明

- 仅处理本地确定性样本，不发起任何网络访问。
- 演示脚本以 `python -I`（隔离模式）运行子进程，避免从样本目录加载被植入的
  同名模块；写文件采用「临时文件 + 原子替换」。
