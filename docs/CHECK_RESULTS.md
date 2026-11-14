# 实际检查结果记录

- 检查日期：2026-11-14
- 机器环境：Windows 11（10.0.26200），Python 3.10.20（`D:\Anaconda\envs\project\python.exe`）
- 运行方式：无容器、纯本地、标准库实现；所有 Python 命令加 `-I` 隔离模式
- 自动化入口：`python -I tools/run_demo.py`（10 项需求拆为 18 个断言，实测 **18/18 通过**）
- 样例数据：全部由 `tools/make_samples.py` 以固定字面量确定性构造，无随机数

## 一、自动核查结果（run_demo.py 实测输出）

```
[PASS] 需求1  字段名统一且与 canonical_fields 一致，未映射字段被丢弃
[PASS] 需求1b 来源特有字段按配置保留（A 侧 channel 保留，B 侧无该键）
[PASS] 需求2  带时区(UTC)与无时区日期均转为统一规范格式并换算到默认时区
[PASS] 需求3  重复主键合并为一条且取最新业务时间记录
[PASS] 需求4  缺必填记录不进规范集且报告逐条列出主键与缺失项
[PASS] 需求5  锁定与截断文件被标记未完成，其他文件记录不受影响
[PASS] 需求5b 截断文件报告包含异常行号与半截内容预览
[PASS] 需求8  报告按文件名固定分组、组内主键排序、两类结论齐全
[PASS] 需求7  同主键跨来源字段冲突按优先级裁决且报告记录主键与字段
[PASS] 需求7b 重复合并事件记录胜出方与淘汰方
[PASS] 需求6  解锁重跑后无重复主键，记录数只增加原缺失部分
[PASS] 需求6b 第二批报告中所有文件均为完整可用
[PASS] 需求6c 同目录重复运行幂等，已成功记录不重复写入
[PASS] 需求9  映射目标不存在时拒绝并指出所在键名
[PASS] 需求9b JSON 语法错误时拒绝并指出行列位置
[PASS] 需求9c CLI 运行错误配置退出码为 2 且不产生规范记录文件
[PASS] 需求10 命令行入口生成规范记录与交接报告到指定路径
[PASS] 需求10b Markdown 交接报告含两类结论与文件明细
通过 18/18
```

## 二、逐项实测证据

### 需求 1：字段名统一、来源特有字段按配置丢弃/保留

规范记录实际出现的字段集合（与配置 `canonical_fields` 完全一致）：

```
['amount', 'biz_time', 'channel', 'customer_name', 'record_id', 'remark']
```

- 来源 A 的 `biz_id/cust_name/trade_amount/event_time/channel_code/note/internal_trace`
  与来源 B 的 `recordId/customer/amt/ts/remark_text` 均映射到同一组规范字段；
- 未在 mappings 中声明的来源字段不会出现在输出中；
- `channel` 仅来源 A 有映射，A 侧记录保留该键，B 侧记录无该键。

### 需求 2：日期统一格式、缺时区标记按默认时区解释

| 记录 | 来源原始值 | 来源默认时区 | 规范输出 |
|---|---|---|---|
| B002 | `2026-11-14T01:30:00Z`（带时区） | UTC | `2026-11-14T09:30:00+08:00` |
| B003 | `2026-11-14T03:00:00`（无时区） | UTC（裸值按 UTC 解释） | `2026-11-14T11:00:00+08:00` |
| A002 | `2026/11/14 08:15:00`（斜杠格式、无时区） | Asia/Shanghai | `2026-11-14T08:15:00+08:00` |

两种写法最终都是偏移型 ISO 8601、秒精度、统一 +08:00 偏移。

### 需求 3：重复主键合并，取业务时间最新

- `source_a_20261114.jsonl` 内 A001 两条（09:30 金额 1200.0 / 10:05 金额 1280.0），
  加上来源 B 的 A001 一条，共 3 条候选；
- 输出仅 1 条 A001，取值来自 10:05 的最新记录：`amount=1280.0`，
  `biz_time=2026-11-14T10:05:00+08:00`；
- 全量输出 9 条记录、9 个唯一主键，无重复。

### 需求 4：缺必填字段拒绝并逐条报告

报告 `rejections` 实测内容：

| 主键（报告原样列出） | 所在文件:行 | 缺失字段 |
|---|---|---|
| `A003` | source_a_20261114.jsonl:4 | `amount` |
| `<缺少主键:source_b_extra.jsonl:L2>` | source_b_extra.jsonl:2 | `record_id` |

两条均未进入 `canonical_records.jsonl`。缺主键的记录用“文件名:行号”合成标识，
保证报告可逐条指认。

### 需求 5：锁占用 / 截断隔离，其他文件不受影响

首跑（锁持有中 + 截断文件存在）控制台实测：

```
规范记录 5 条，拒绝 2 条，合并 1 起，冲突裁决 4 起
完整可用文件 3 个，未完成文件 2 个
```

报告对两个未完成文件的原始描述：

- `source_a_locked.jsonl` → status `locked`：
  `文件被占用，无法读取（PermissionError: [Errno 13] Permission denied ...）`
  （由 `tools/hold_lock.py` 通过 Win32 零共享打开真实加锁，非 mock）；
- `source_b_truncated.jsonl` → status `truncated`：
  `第 3 行不是合法 JSON（Expecting property name enclosed in double quotes）...`，
  异常行预览 `{"recordId": "T003", "customer": "Half Line", "amt": 33,`；
- 其余 3 个文件记录数与无故障时完全一致。

### 需求 6：解锁补齐、已成功记录不重复写入

同一输出目录两次运行实测：

| 运行 | 条件 | 规范记录数 | 未完成文件 |
|---|---|---|---|
| 首跑 | locked + truncated | 5 | 2 |
| 重跑 | 解锁 + 截断文件补全 | 9 | 0 |

- 净增主键恰好为原缺失部分：`L001、L002、T001、T002`，首跑 5 条主键全部保留；
- 第三次同目录重跑（文件指纹未变）记录数仍为 9，合并/冲突事件数不变，幂等；
- 去重保障：最终 9 条记录对应 9 个唯一主键。

### 需求 7：来源优先级裁决并记录冲突

A001 在两来源同字段冲突的实测裁决（来源 A priority=1 高于来源 B priority=2）：

| 字段 | source_a (p1) | source_b (p2) | 裁决结果 |
|---|---|---|---|
| amount | 1280.0 | 999.99 | 1280.0（source_a） |
| customer_name | 张伟 | ZHANG Wei | 张伟（source_a） |
| biz_time | 2026-11-14T10:05:00+08:00 | 2026-11-14T10:00:00+08:00 | source_a |
| remark | trace-a001-2 | B 侧较早记录 | trace-a001-2（source_a） |

报告 `events.field_conflicts` 共 4 起，每条含主键、字段名、各来源值与优先级、
最终裁决来源；`duplicate_merges` 记录胜出方与两个淘汰方的文件/行号/业务时间。

### 需求 8：报告分组与排序、两类结论

- `file_groups` 按文件名固定字典序：
  `source_a_20261114 → source_a_locked → source_b_20261114 → source_b_extra → source_b_truncated`；
- 每组 `accepted_pks`、`rejections`、事件均按主键（及字段名）排序；
- 顶层结论固定两个键：`complete_usable` 与 `unfinished`，与文件实际状态一致；
- 同时提供 `handover_report.json`（机器读取）与 `handover_report.md`（人工交接）。

### 需求 9：错误配置整体拒绝、无半成品、错误信息带位置

两类错误配置样例实测：

1. 映射目标不存在（`bad_mapping_target.json`）：

```
[配置错误] $.sources[0].mappings.cust_name: 映射目标字段 'customer_name_typo'
不存在于 canonical_fields 中
```

2. JSON 语法错误（`bad_mapping_syntax.json`）：

```
[配置错误] 配置文件 bad_mapping_syntax.json 不是合法 JSON：第 5 行第 3 列
附近 Expecting ',' delimiter
```

CLI 实测退出码 `2`，且输出目录 `out_cli_error` **根本未被创建**，
无 `canonical_records.jsonl` 等任何半成品文件（配置校验先于输出目录创建）。

### 需求 10：Windows 可直接执行的命令入口

两种入口实测退出码均为 0：

```powershell
run_handover.bat demo_workspace\manual_samples\input `
    demo_workspace\manual_samples\config\mapping.json demo_workspace\manual_out

python -I run_handover.py --input-dir <输入目录> --config <配置> --output-dir <输出目录>
```

指定输出目录下生成三件产物：`canonical_records.jsonl`、
`handover_report.json`、`handover_report.md`（另有续跑状态文件
`.handover_state.json`）。

## 三、补充边界检查

- 未匹配任何来源的 JSONL 文件：状态标记为 `source_unmatched`，进入“未完成”结论，
  不静默丢弃；
- 已成功文件在后续运行中被占用/删除：沿用历史已入库记录，报告附 `read_issue`
  说明，已入库数据不丢失；
- 状态文件损坏：明确报错退出，不静默重置；
- 所有文件写入走“同目录临时文件 + 原子替换”，不存在半截产物；
- `python -m compileall` 全模块编译通过。

## 四、复现步骤

```powershell
# 1. 环境确认
python --version          # 期望 3.10+

# 2. 一键全自动核查（含真实写锁、两批处理、错误配置、CLI 验证）
python -I tools/run_demo.py

# 3. 手工分步运行
python -I tools/make_samples.py --dir samples
python -I run_handover.py --input-dir samples/input `
    --config samples/config/mapping.json --output-dir out
# 或：run_handover.bat samples\input samples\config\mapping.json out
```
