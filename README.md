# 日志脱敏残留核查工具

Windows 本地运行的日志审计工具：扫描指定目录下全部 `.log` 文件，用自建的
「修复前」「修复后」两套脱敏实现各处理一遍，找出仍残留的手机号与证件号，
输出可核对的 UTF-8 JSON 报告。

不依赖容器、不访问外部服务，仅使用 Python 标准库。

## 环境要求

- Windows 10 / 11
- Python 3.10 及以上（开发验证环境：Python 3.10.20）
- 运行测试可选安装 `pytest`（已在 9.2.0 验证）

## 目录结构

```
sanitizer_audit/          审计工具
  cli.py                  命令行入口
  core.py                 扫描、调用、重试、报告生成
sanitizers/               两套脱敏实现与共享识别规则
  sanitizer_legacy.py     修复前实现（故意保留缺陷，用于对照）
  sanitizer_fixed.py      修复后实现（覆盖变形写法）
  common.py               号码识别、Unicode 规范化、残留统计
samples/
  make_samples.py         样例日志生成脚本
  logs/                   生成的样例（app/ batch/ broken/）
tests/                    单元测试与端到端测试（40 项）
out/                      运行产物（报告 JSON）
```

## 快速开始

在项目根目录 `E:\Company\demo\projects\project68` 下执行：

```powershell
# 1. 生成样例日志
python samples/make_samples.py

# 2. 运行核查，输出 JSON 报告
python -m sanitizer_audit.cli --log-dir samples/logs --report out/report.json

# 3. 运行测试
python -m pytest tests -q
```

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--log-dir` | 必填，待扫描的日志目录（递归查找 `.log`） |
| `--report` | 必填，JSON 报告输出路径（父目录自动创建） |
| `--max-attempts` | 单套实现处理单个文件的最大尝试次数，默认 `2` |
| `--fail-fixed-call N` | 模拟修复后实现第 N 次调用失败一次，用于演示重试恢复 |
| `--disable-fixed` | 模拟修复后实现持续不可用 |
| `--disable-legacy` | 模拟修复前实现持续不可用 |

### 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部文件完成核查 |
| `1` | 报告已写出，但存在未能完整核查的文件（编码异常、解析异常、实现不可用） |
| `2` | 参数或目录问题（目录不存在、无 `.log` 文件），未写报告 |

## 报告格式

顶层字段：

```json
{
  "generated_at": "2026-11-14T15:15:51+08:00",
  "log_dir": "E:\\Company\\demo\\projects\\project68\\samples\\logs",
  "implementations": { "legacy": "sanitizers.sanitizer_legacy", "fixed": "sanitizers.sanitizer_fixed" },
  "summary": { "...": "各类状态计数与两版残留合计" },
  "confirmed_vulnerabilities": ["app/2026-11-11/service.log"],
  "incomplete": { "unavailable_implementations": [], "affected_files": [] },
  "records": [ "...": "每个日志文件一条" ]
}
```

每条 `records` 记录：

| 字段 | 说明 |
| --- | --- |
| `path` | 相对日志目录的 POSIX 路径，记录按字典序排列 |
| `line_count` | 原始文件行数；编码/解析异常时为 `null` |
| `status` | `clean` / `confirmed_vulnerability` / `fix_incomplete` / `fix_unverified` / `unverified` / `encoding_error` / `parse_error` |
| `residuals` | 修复前实现输出中的残留，`{type, line, count}`，`type` 为 `phone` 或 `id_card` |
| `legacy_residual_count` | 修复前实现的残留项数量 |
| `fixed_residuals` | 修复后实现输出中的残留 |
| `fixed_residual_count` | 修复后实现的残留项数量 |
| `fixed_verification` | `passed` / `failed` / `unavailable` / `skipped` |
| `leaked_numbers` | 修复后文本按号码原文检索仍命中的项 |
| `attempts` | `{legacy, fixed}` 本次文件各实现的尝试次数 |
| `error` | 异常或不可用说明，正常为 `null` |

`status` 判定规则：修复前残留数 > 0 即 `confirmed_vulnerability`（该结论不受
修复后实现是否可用影响）；修复后实现不可用时为 `fix_unverified`，并与
`incomplete.affected_files` 一并列出。

## 识别与脱敏规则

两套实现与检测器共用 `sanitizers/common.py` 的识别规则：

- 手机号：11 位、`1[3-9]` 开头；支持 `+86` 前缀、空格 / 连字符 / 点 / 中点 /
  下划线分隔、全角数字与全角空格、零宽字符、数字间夹杂空白。
- 证件号：18 位居民身份证号（地址码 + 出生日期结构校验）与 15 位旧式号码；
  支持分隔符、全角字符、`x` 校验位。

修复前实现只识别连续 11 位手机号与连续 18 位数字证件号，因此变形写法会残留。
修复后实现先做 NFKC 规范化并剔除零宽字符，再把匹配区间映射回原文替换，
其余文本与换行保持原样。

## 可复现的业务样例

`samples/make_samples.py` 每次运行都会重建 `samples/logs/`（幂等）：

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| `app/2026-11-10/service.log` | 9 | 已正确脱敏，无残留 |
| `app/2026-11-11/service.log` | 7 | 含 `138-1234-5678`、`+86 138-1234-5678` 等分隔符手机号 |
| `app/2026-11-12/service.log` | 5 | 含全角手机号、数字间夹杂空格 |
| `batch/report.log` | 6 | 含空格分隔证件号、小写 `x` 证件号、全角证件号 |
| `broken/gbk.log` | — | GBK 编码，触发 `encoding_error` |
| `broken/binary.log` | — | 含 NUL 控制字符，触发 `parse_error` |

## 实际运行结果

```
$ python -m sanitizer_audit.cli --log-dir samples/logs --report out/report.json
日志目录: E:\Company\demo\projects\project68\samples\logs
文件总数: 6  已确认漏洞: 3  未验证: 0  修复后未验证: 0  编码异常: 1  解析异常: 1
残余总数: 修复前 7 / 修复后 0；修复后未验证: 0
  [clean] app/2026-11-10/service.log  行数=9  残留(修复前)=0  残留(修复后)=0
  [confirmed_vulnerability] app/2026-11-11/service.log  行数=7  残留(修复前)=2  残留(修复后)=0
      残留 phone @ 行 1
      残留 phone @ 行 4
  [confirmed_vulnerability] app/2026-11-12/service.log  行数=5  残留(修复前)=2  残留(修复后)=0
      残留 phone @ 行 1
      残留 phone @ 行 2
  [confirmed_vulnerability] batch/report.log  行数=6  残留(修复前)=3  残留(修复后)=0
      残留 id_card @ 行 2
      残留 id_card @ 行 3
      残留 id_card @ 行 4
  [parse_error] broken/binary.log  行数=-  残留(修复前)=None  残留(修复后)=None
      说明: 内容包含 NUL 控制字符，无法按行解析
  [encoding_error] broken/gbk.log  行数=-  残留(修复前)=None  残留(修复后)=None
      说明: UTF-8 解码失败: 'utf-8' codec can't decode byte 0xb7 in position 36: invalid start byte
已确认漏洞文件: app/2026-11-11/service.log, app/2026-11-12/service.log, batch/report.log
报告已写入: E:\Company\demo\projects\project68\out\report.json
```

退出码为 `1`（存在编码/解析异常文件，结论已如实写入报告）。

### 场景演示

```powershell
# 瞬时故障：第 1 次调用修复后实现失败，重试后成功
python -m sanitizer_audit.cli --log-dir samples/logs --report out/report_fail_once.json --fail-fixed-call 1 --max-attempts 2

# 持续不可用：受影响文件清单与已确认漏洞分开呈现
python -m sanitizer_audit.cli --log-dir samples/logs --report out/report_no_fixed.json --disable-fixed
```

## 测试覆盖

`tests/test_sanitizers.py` 覆盖各类变形写法与两套实现差异；
`tests/test_audit.py` 为端到端测试，对应需求 (1)~(9)：

- 空目录 / 缺失目录退出码非零且不写报告；
- 三份日志记录条数、行数与原文件一致；
- 修复前残留按类型与行号列出，干净文件为空；
- 修复前残留 > 0、修复后为 0 且标记 `confirmed_vulnerability`；
- JSON 字段、UTF-8 编码、生成时间可解析；
- 非 UTF-8 与无法解析文件单独归类，其余文件结论完整；
- 修复后输出按号码原文检索无命中；
- 瞬时故障重试后结论完整，先前已完成记录不变；
- 持续不可用列出受影响文件，已确认漏洞不被改写；
- 记录按路径字典序，两次运行除生成时间外深度相等。
