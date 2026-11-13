# WAV 标记剪辑工具（wav_splitter）

把单声道 PCM WAV 整段录音按 CSV 标记剪成多个片段，并产出 JSON 剪辑报告。
**纯 Python 标准库实现，无需安装任何第三方依赖**，要求 Python 3.10+。

## 目录结构

```
project59/
├── wav_splitter/              # 主程序包
│   ├── __init__.py
│   ├── __main__.py            # 支持 python -m wav_splitter
│   ├── cli.py                 # 命令行入口与退出码
│   ├── errors.py              # SplitterError 致命错误类型
│   ├── markers.py             # CSV 读取与严格校验
│   ├── models.py              # 数据模型与状态常量
│   ├── processor.py           # 越界/无效/重叠判定与切片导出
│   ├── report.py              # JSON 报告组装、排序与写出
│   └── wav_io.py              # WAV 校验、毫秒对齐、片段写出
├── tools/
│   └── generate_samples.py    # 确定性样本生成脚本（任务 14）
├── tests/
│   ├── test_wav_splitter.py   # 29 个验收测试（覆盖任务 1~14）
│   └── _isolated_runner.py    # 测试用 -I 隔离模式启动器
├── REPORT_SCHEMA.md           # 报告结构与状态规则说明
├── run_demo.ps1               # Windows 一键演示
└── run_demo.sh                # macOS/Linux 一键演示
```

## 快速开始

```powershell
# 1. 生成确定性样本（samples/sample.wav + samples/marks.csv）
python tools/generate_samples.py

# 2. 执行剪辑（输出目录不存在会自动创建）
python -m wav_splitter --wav samples/sample.wav --csv samples/marks.csv --output output/demo
# 等价短参数：
python -m wav_splitter -w samples/sample.wav -c samples/marks.csv -o output/demo
```

或直接运行一键演示：

```powershell
./run_demo.ps1          # Windows PowerShell
bash run_demo.sh        # macOS/Linux
```

## 命令行参数

| 参数 | 含义 |
|---|---|
| `--wav` / `-w` | 输入单声道 PCM WAV 路径（必填） |
| `--csv` / `-c` | 输入 CSV 标记路径，UTF-8、逗号分隔、三列（必填） |
| `--output` / `-o` | 输出目录，不存在时自动创建（必填） |

## CSV 格式

无表头、UTF-8（兼容 BOM）、逗号分隔、恰好三列：

```
片段编号,起始秒,结束秒
A,0.0,1.0
b,1.5,3.0
c,1.2345,1.4
```

- 时间支持浮点秒；空行跳过；编号不可为空。
- 首行若三列齐全但时间列均非数字，会被识别为可选表头并跳过
  （即带 `segment_id,start,end` 表头的文件也能处理）；其余任何行
  缺列、多列、时间非数字均为致命错误。
- 重复编号允许：报告中按行号区分，导出文件重名时后者自动追加
  `__row<行号>` 后缀。

## 处理规则

对每行标记按以下顺序短路判定（详见 [REPORT_SCHEMA.md](REPORT_SCHEMA.md)）：

1. **越界**：任一端点超出 `[0, 总时长]` → 跳过，无 WAV，报告保留编号与原始时间。
2. **无效区间**：`结束 <= 起始`（含毫秒对齐后退化）→ 跳过，无 WAV。
3. **重叠**：与此前已成功导出的片段相交（**端点相接也算**）→ 后出现者跳过。
4. **成功**：切片导出 `编号.wav`，采样率、位深、声道数与源文件一致。

跳过片段会向 **stderr** 输出含编号与原因（越界/无效区间/重叠）的警告；
成功片段 `错误描述` 为 `null`，跳过片段不产生任何 WAV 或空片段记录。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 运行完成（允许存在被跳过的标记） |
| `2` | 输入级致命错误：CSV 缺失/格式错误、WAV 缺失/非单声道 PCM 等 |

致命错误在创建输出目录**之前**完成校验，因此不会产生任何片段或报告。

## 运行测试

```powershell
python -m unittest discover -s tests -v
```

子进程类测试以 `python -I -X utf8`（隔离模式 + UTF-8）运行，
验证真实退出码与 stderr 内容。

## 设计备注

- 时间解析与对齐全程使用 `decimal.Decimal` + `ROUND_HALF_UP`，
  `1.2345s @8000Hz` 确定性映射到采样点 `9880`。
- 导出文件名过滤 `<>:"/\|?*` 与控制字符，防止路径穿越。
- 样本 WAV 采用确定性线性斜坡 PCM，重复生成逐字节一致，
  测试可通过采样值反查切片偏移。
