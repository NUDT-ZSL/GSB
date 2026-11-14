# 多来源 JSONL 业务记录交接规范化工具

上游在系统切换前把批量业务记录导出为一批 JSONL 文件，不同来源字段名、日期格式
不一致，且可能存在文件被占用、只写入半截、重复主键等问题。本工具把这些记录统一
映射为规范记录，并生成交接报告，标明**完整可用**与**未完成（需要跟进）**两类结论。

- 纯 Python 标准库实现，无第三方依赖，不使用容器；
- 支持 Windows 直接执行（`.bat` / `python -I` 隔离模式）；
- 所有样例由代码确定性构造，可重复运行。

## 1. 环境要求

- Windows 10/11，Python 3.10 及以上（开发实测：Python 3.10.20）；
- 时区使用标准库 `zoneinfo`。Windows 上若未安装 `tzdata` 包，工具内置
  Asia/Shanghai、UTC 等常用时区与 `UTC+8` 固定偏移兜底；
- 验证命令：

```powershell
python --version
```

## 2. 目录结构

```
project65/
├─ handover/                 # 核心实现包
│  ├─ config.py              # 映射配置加载与严格校验（错误定位到键名/行列）
│  ├─ tzutil.py              # 时区解析、日期规范化
│  ├─ reader.py              # 单文件读取：锁占用 / 截断 / 缺失识别
│  ├─ pipeline.py            # 映射、必填校验、去重合并、冲突裁决、续跑状态
│  ├─ report.py              # 交接报告渲染（JSON + Markdown）
│  ├─ cli.py                 # 命令行参数解析
│  └─ errors.py
├─ tools/
│  ├─ make_samples.py        # 确定性构造样例数据与配置（含错误配置样例）
│  ├─ hold_lock.py           # Windows 文件写锁模拟（Win32 零共享打开）
│  └─ run_demo.py            # 端到端演示 + 需求 1~10 自动核查
├─ run_handover.py           # Python 启动器（兼容 3.10 的 -I 隔离模式）
├─ run_handover.bat          # Windows 一键入口
└─ docs/CHECK_RESULTS.md     # 实际检查结果记录
```

## 3. 映射配置说明

配置为 JSON，结构见 `tools/make_samples.py` 中的 `MAPPING_CONFIG`，关键字段：

| 键 | 含义 |
|---|---|
| `batch_id` | 批次标识，写入报告 |
| `primary_key` | 规范主键字段名，必须在 `canonical_fields` 中 |
| `canonical_fields` | 规范字段全集，同时决定输出记录的键顺序 |
| `required_fields` | 必填字段；缺失的记录不入库并逐条写入报告 |
| `date_fields` | 日期字段；`date_fields[0]` 同时作为合并用业务时间 |
| `default_timezone` | 全局默认时区，所有规范日期统一换算到此时区 |
| `sources[]` | 来源数组，含优先级、文件名前缀、字段映射、来源默认时区、日期格式 |

来源级要点：

- `priority`：正整数且全局唯一，**数字越小优先级越高**（1 为最高）；
- `file_match`：文件名前缀，多来源命中时取最长前缀；
- `mappings`：`来源字段名 -> 规范字段名`。**未声明映射的来源字段一律丢弃**，
  声明即保留；映射目标必须存在于 `canonical_fields`，否则整体拒绝执行；
- `default_timezone`：该来源裸日期（无时区标记）的解释时区；
- `input_date_formats`：除 ISO 8601 外额外支持的 `strptime` 格式。

规范日期格式统一为偏移型 ISO 8601、秒精度，例如
`2026-11-14T09:30:00+08:00`。带时区的值按自身时区解析后换算到全局默认时区；
不带时区的值按来源默认时区解释后再换算，保证同一时刻输出完全一致。

## 4. Windows 执行命令（需求 10）

方式一：批处理入口

```powershell
run_handover.bat <输入目录> <映射配置.json> <输出目录>
```

方式二：Python 隔离模式（推荐，屏蔽外部 PYTHONPATH 与当前目录注入）

```powershell
python -I run_handover.py --input-dir <输入目录> `
    --config <映射配置.json> --output-dir <输出目录>
```

完整示例（先构造样例再运行）：

```powershell
python -I tools/make_samples.py --dir samples
python -I run_handover.py --input-dir samples/input `
    --config samples/config/mapping.json --output-dir out
```

输出目录产物：

| 文件 | 说明 |
|---|---|
| `canonical_records.jsonl` | 规范记录，每行一条，主键唯一 |
| `handover_report.json` | 机器可读交接报告（主报告） |
| `handover_report.md` | 人工交接视图 |
| `.handover_state.json` | 续跑状态（文件指纹、历史候选），用于补齐与幂等 |

退出码：`0` 处理完成（报告中可能仍有未完成文件，属业务结果）；
`2` 配置错误（不产生任何半成品）；`1` 其他致命错误。

## 5. 端到端演示与自动核查

一条命令完成：构造样例 → 真实 Windows 写锁占用 → 首跑 → 解锁并修复截断 →
同目录重跑 → 错误配置拒绝 → CLI 入口验证，并对需求 1~10 逐条断言：

```powershell
python -I tools/run_demo.py
```

实测输出为 `通过 18/18`（10 项需求拆成 18 个检查点），细节见
[docs/CHECK_RESULTS.md](docs/CHECK_RESULTS.md)。

手工模拟锁占用（默认持锁 60 秒）：

```powershell
python -I tools/hold_lock.py samples/input/source_a_locked.jsonl --seconds 60
```

## 6. 处理规则与需求对照

| 需求 | 实现位置与规则 |
|---|---|
| 1 字段统一/丢弃保留 | `pipeline._map_one`：仅输出 mappings 声明且在 canonical_fields 中的字段 |
| 2 日期统一/默认时区 | `tzutil.parse_datetime` + 全局默认时区换算 |
| 3 重复主键取最新 | `pipeline.Pipeline._merge`：业务时间降序，取首条 |
| 4 缺必填拒绝并报告 | `_map_one` 返回 Rejection，报告逐主键列缺失项 |
| 5 锁/截断隔离 | `reader.read_jsonl_file`：单文件失败不影响其他文件，报告列未完成文件 |
| 6 解锁补齐不重复 | 状态文件 + 文件指纹；同目录重跑只补缺失部分，全量重写保证幂等 |
| 7 优先级冲突裁决 | `_merge`：同主键跨来源同字段值不同即记录冲突，取高优先级来源值 |
| 8 报告分组排序 | 文件按文件名排序、组内主键排序，结论分完整可用/未完成两类 |
| 9 错误配置整体拒绝 | `config.load_config` 全量前置校验，错误信息含 JSON 路径键名或行列 |
| 10 Windows 命令入口 | `run_handover.bat` 与 `run_handover.py`，三参数指定目录与配置 |

合并裁决的完整次序（确定性，无随机）：
**业务时间最新 → 来源优先级数字最小 → 文件名 → 行号**。

## 7. 安全与边界说明

- 所有 Python 运行命令使用 `-I` 隔离模式，防止输入目录中的同名模块
  （如 planted `json.py`）被导入；
- 仅处理本地目录与明确授权的样例数据，不发起任何网络请求；
- 输出采用“临时文件 + 原子替换”，错误配置下输出目录不会被创建，杜绝半成品；
- 状态文件损坏时明确报错并要求人工确认后删除，不静默重置。
