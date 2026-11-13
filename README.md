# 游戏对白 WAV 批量剪辑与波形统计工具

纯 Python 标准库实现（`wave` / `struct` / `json` / `logging`），无需第三方依赖、无需容器。
对单声道 16 位 PCM WAV 批量执行片段截取、增益调节（含钳制）、峰值/RMS/过零率统计，
产出标准 WAV 与 JSON 报告；单个文件出错只记录错误，不影响其余文件。

## 1. 环境要求

- Python 3.10+（开发与验证环境：Windows 11 + Python 3.10.20，仅标准库）
- 不使用 Docker 或任何第三方包

## 2. 目录与文件说明

```
project36/
├── clip_tool.py      # 主工具：读取配置、校验格式、截取、增益、统计、报告与日志
├── make_samples.py   # 确定性生成 6 个测试 WAV（含立体声/8位/损坏等非法样本）
├── verify.py         # 端到端验收脚本，自动核对任务书 10 条验收要求
├── config.json       # 示例配置（10 条指令，覆盖全部正常与异常场景）
├── input/            # 输入 WAV（运行 make_samples.py 生成）
└── output/           # 输出 WAV + report.json + clip_tool.log（自动创建）
```

## 3. 本地启动方式（可复现演示）

```powershell
# 步骤 1：确定性生成输入样本（固定公式，无随机源，可重复复现）
python -I make_samples.py input

# 步骤 2：执行批量剪辑
python -I clip_tool.py config.json

# 一步完成“生成样本 + 运行工具 + 10 项自动验收”
python -I verify.py
```

> `-I` 为隔离模式，忽略当前目录可能存在的同名模块（如 planted `json.py`）与
> 用户站点包，保证只加载标准库。

控制台输出示例：

```
处理完成: 成功 4 个，错误 6 个，共 10 个
Summary: success=4 error=6 total=10
报告: ...\output\report.json
日志: ...\output\clip_tool.log
```

退出码：`0` = 批处理正常结束（允许部分或全部条目为错误）；`2` = 配置缺失/非法等致命错误。

## 4. config.json 配置说明

```json
{
  "input_dir": "input",          // 输入目录（相对路径以配置文件所在目录为基准）
  "output_dir": "output",        // 输出目录，不存在时自动创建
  "report_name": "report.json",  // 可选，默认 report.json
  "log_name": "clip_tool.log",   // 可选，默认 clip_tool.log
  "tasks": [
    {
      "filename": "tone_440.wav", // 输入文件名
      "start_sec": 0.1,           // 起始秒（含），非负
      "end_sec": 0.6,             // 结束秒（不含），必须为正
      "gain": 2.0                 // 增益倍数，非负有限数
    }
  ]
}
```

## 5. 处理规则与错误隔离

| 主题 | 规则 |
|---|---|
| 格式准入 | 仅接受 `PCM(NONE)`、单声道、16 位 WAV；立体声/8 位/非 PCM/损坏文件记 `UnsupportedFormat` 或 `InvalidWav`，其余文件继续 |
| 时间截取 | 秒按 `round(t × 采样率)` 转帧，帧对齐截取；越界（含起止相等的空片段）记 `TimeRangeOutOfBounds` |
| 增益 | `round(样本 × gain)` 后钳制到 `[-32768, 32767]`，**绝不回绕溢出** |
| 输出格式 | 标准 RIFF/WAVE，`fmt `+`data` 块，单声道 16 位 PCM，采样率与输入一致，可被 `wave.open` 重开 |
| 命名 | `<原名去扩展名>_clip<同文件序号>.wav`；目标已存在（含同批次）则追加 `_1`、`_2`…，旧文件不变 |
| 报告 | 按输出文件名排序；每条含 `status`(success/error)、`error`(错误码或 null)、`message`、统计值等 |
| 日志 | 每条指令一对“开始处理/结果”行，末尾汇总成功/错误数，与报告统计一致 |
| 隔离 | 单条指令的任何异常都被捕获并转为错误条目，批处理不中断 |

### 统计量定义（写入每条成功记录的 `stats`）

- `peak`：带符号峰值 `max(最大正样本, |最小负样本|)`，恒在样本域 `[-32768, 32767]` 内；
  其绝对值为振幅峰值
- `rms`：`sqrt(Σ样本² / N)`，保留 4 位小数
- `zero_crossing_rate`：相邻样本符号变化次数 / 样本总数，保留 8 位小数
- `duration_sec`：帧数 / 采样率

### 错误码

| error | 触发条件 |
|---|---|
| `FileNotFound` | 输入文件不存在 |
| `UnsupportedFormat` | 非单声道、非 16 位或非 PCM |
| `InvalidWav` | WAV 无法解析或 data 块字节数不足（损坏） |
| `TimeRangeOutOfBounds` | 起止帧越界或片段为空 |
| `IOError` | 读写文件失败 |
| `UnexpectedError` | 兜底未预期异常（仍不影响其他条目） |

## 6. 实测验证结果（2026-11-14，Python 3.10.20 / Windows 11）

`python -I verify.py` 输出 **通过 10/10 项**，关键实测数据：

| 验收项 | 实测结果 |
|---|---|
| 配置+输出 | 删除 output 后自动重建，4 条成功指令产出 4 个 WAV |
| 错误隔离 | 立体声/8 位/损坏各记一条 error，合法文件全部正常输出 |
| 截取精度 | 0.5s 指令 → 24000 帧 / 48000Hz = 0.500000s，偏差 0.000 ms（限 ≤5ms） |
| 增益钳制 | 2.0× 增益（20000→40000）出现 32767 钳制点，全部样本 ∈ [-32768,32767]，与独立钳制计算逐样本一致 |
| 标准 PCM | 4 个输出均被 `wave.open` 重开，ch=1、16bit、48000Hz、帧数与报告一致，RIFF/WAVE 魔数正确 |
| 正弦统计 | 440Hz：peak 20000（理论 20000，误差 0.000%）；RMS 14142.1405（理论 14142.14，0.000%）；ZCR 0.01829167（理论 0.01833333，0.227%），均 <5% |
| 排序/缺失/越界 | 报告按文件名排序；缺失文件、结束越界、空片段均 status=error 且 message 非空 |
| 重名保护 | 二次运行旧 4 文件 MD5 全不变，新增 `*_clip1_1.wav` 等 4 个带序号文件 |
| 日志 | 10 条开始行 + 10 条结果行 + 末尾“成功 4 / 错误 6”，与报告一致 |
| CLI 汇总 | 控制台 `success=4 error=6 total=10`，与报告、日志三方一致 |

## 7. 引擎导入对接建议

- 报告 `entries[].output_file` 即输出 WAV 文件名，可直接与 `output_dir` 拼接供引擎批量导入
- 仅 `status == "success"` 的条目对应可用资产；错误条目可凭 `error`/`message` 回流给音频制作人员修复源文件
- 全部输出为固定 48kHz/16bit/单声道 PCM，无需转码即可作为引擎原生 PCM 资产
