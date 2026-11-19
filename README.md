# 本地列式聚合与窗口计算引擎

这是一个只依赖 Python 标准库运行时的本地确定性计算工具。它在列式 JSON
批次上流式推进分组聚合与滑动窗口，使用 SQLite 原子提交算子状态、批次水位、
批次摘要、输出摘要和误差谱，并支持中断恢复、并发幂等提交和计划整体替换。

NumPy 只用于重新生成确定性样本；引擎执行和验证不依赖 NumPy。

## 快速开始

环境要求：

- Windows 10 或更高版本，或等价的本地 Python 环境
- Python 3.11+
- `python` 与 `pip` 在 `PATH` 中
- 可选依赖：`numpy`，仅 `tools/generate_samples.py` 使用

PowerShell 中执行：

```powershell
python -m pip install -r requirements.txt
python colagg.py clean
.\run.ps1 load
.\run.ps1 execute
.\run.ps1 status
.\run.ps1 verify
```

如果 PowerShell 执行策略禁止脚本，可直接使用统一入口：

```powershell
python colagg.py clean
python colagg.py load
python colagg.py execute
python colagg.py status
python colagg.py verify
```

首次运行会生成 `state.db`、`output/results.json` 和
`output/status.json`。`verify` 还会生成 `output/verify.json`。

## 固定相对路径

| 路径 | 用途 |
| --- | --- |
| `samples/batches.json` | 确定性列式批次 |
| `samples/query.json` | 当前计划版本 v1 |
| `samples/query_v2.json` | 增量计划版本 v2 |
| `samples/precision.json` | 定点累加配置 |
| `samples/precision_float.json` | float64 主通路配置 |
| `state.db` | SQLite 状态库 |
| `output/results.json` | 稳定排序的聚合、窗口、误差谱结果 |
| `output/status.json` | 稳定排序的算子状态 |
| `output/verify.json` | 持久化状态与全量重算的一致性报告 |

重新生成样本：

```powershell
python tools/generate_samples.py
```

样本由 3 个批次、3 个分组（A/B/C）、24 行组成。A/B 分组包含
`4.5` 与 `4.5000000001` 附近的近并列差值，用于触发精度敏感标记。

## 命令

所有命令都支持 `--db` 与 `--output-dir`。涉及样本的命令支持
`--batches`、`--query`、`--precision`。

```text
load     校验输入并加载批次目录与计划快照
execute  执行待处理批次，直到完成；可注入逻辑时钟或模拟硬退出
resume   先执行状态恢复，再补齐缺失批次
status   输出每算子水位、行数、累加通路和失败态
verify   全量重放并逐聚合、逐窗口比对持久化结果与 results.json
plan show      查看当前计划、快照和替换审计
plan replace   以 load 的相同语义替换计划
clean          清理状态库、锁、WAL 和输出目录
```

常用参数：

```text
--clock 5000          注入首个尚未分配时钟的批次
--max-batches 1       只推进一个批次
--crash-after 2       提交第 2 个批次后立即 os._exit(137)
--no-finalize         不输出尾部部分窗口
--plan-version 3      load 时覆盖查询文件内的版本号
```

## 输入模型

批次文件可以是数组，也可以是 `{"batches": [...]}`。单个批次示例：

```json
{
  "seq": 0,
  "row_count": 3,
  "columns": {
    "grp": ["A", "B", "A"],
    "value": [0.1, 0.2, 0.3]
  },
  "content_digest": "由规范化内容计算出的 SHA-256"
}
```

兼容字段别名：

- 批次序号：`seq`、`batch_seq`、`batch_id`、`batch_index`、`batch_no`、
  `ordinal`、`index`
- 行数：`row_count`、`rows`、`num_rows`、`n_rows`、`count`
- 列：对象映射或 `[{"name", "values"/"data", "type"/"dtype"}]`
- 摘要：`content_digest`、`digest`、`sha256`、`batch_digest`、`checksum`

查询示例：

```json
{
  "plan_version": 1,
  "group_by": ["grp"],
  "aggregates": [
    {"name": "s", "op": "sum", "column": "value"},
    {"name": "m", "op": "mean", "column": "value"},
    {"name": "v", "op": "variance", "column": "value"},
    {"name": "n", "op": "count"}
  ],
  "window": {
    "column": "value",
    "length": 4,
    "step": 2,
    "boundary": "emit",
    "functions": [
      {"name": "ws", "op": "sum"},
      {"name": "wm", "op": "mean"}
    ]
  }
}
```

聚合支持 `sum`、`mean`/`avg`、`variance`/`var_pop`、`count`。
`variance` 是总体方差。窗口使用同一组算子，窗口长度 `L` 与步长 `S`
满足 `1 <= S <= L`。

窗口按每个分组自己的到达顺序切分。第 `k` 个完整窗口覆盖行
`[k*S, k*S+L)`，允许跨批次。末尾不足 `L` 行时：

- `emit`：输出结果并设置 `complete=false, status="partial"`
- `mark`：输出占位记录并设置 `value=null, status="insufficient"`
- `skip`：不输出该尾部窗口

精度配置示例：

```json
{
  "accumulator": "fixed",
  "scale": 100,
  "relative_tolerance": 1e-6,
  "clock_step": 10,
  "clock_start": 1000,
  "int_bits": 64
}
```

`accumulator` 可为 `float64` 或 `fixed`。`scale` 必须是
`[1, 10^12]` 内的整数；`int_bits` 支持 32 或 64。即使主通路选择其中一种，
引擎仍同时维护两条通路，以便输出误差谱和路径差。

## 数值与误差语义

- 定点换算使用 `Decimal(repr(value)) * scale`，按 half-even 规则取整。
- 每次定点换算记录原值、标度、定点值、舍入方向（`up`/`down`/`none`）。
- 原始浮点值在转换前保留；结果误差链和 `conversion_traces` 都保存原值。
- float64 通路按批次和行输入顺序固定累加求和、平方和，不使用并行归约。
- 定点通路对整数和、平方和做精确累加，并在每一阶段检查配置的整数位宽。
- 固定路径误差上界：求和为 `n * 0.5 / scale`，均值为
  `0.5 / scale`，方差使用 `2*max|x|*delta + delta^2`。
- float64 路径使用机器 epsilon 的 gamma 上界，按实际累加次数计算。
- 每个结果同时记录 `float_value`、`fixed_value`、`path_delta`、
  `bounds.fixed`、`bounds.float64` 和主通路 `error_bound`。
- 同一算子的结果按值排序后比较相邻项。若差值不超过
  `relative_tolerance * max(|a|, |b|)`，两条结果都标记
  `precision_sensitive=true`，并附带对方分组/窗口、差值和阈值。
- 误差链包含输入列、批次序号、逐行或逐批中间累计值。

## 校验和业务错误码

`load` 在打开或写入状态库之前完成全部输入解析与交叉校验。非法输入不会
创建 `state.db`，已有状态库也不会被写入。

主要错误码：

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_BATCH` | 批次结构、行数、序号或列长度非法 |
| `E_BAD_QUERY` | 查询结构、引用、窗口参数或名称非法 |
| `E_BAD_PRECISION` | 累加通路、标度、容差或时钟非法 |
| `E_BATCH_DIGEST_MISMATCH` | 批次嵌入摘要或持久化摘要不匹配 |
| `E_TYPE_MISMATCH` | 引用列不存在类型或跨批次类型不可比 |
| `E_FIXED_OVERFLOW` | 定点换算或累计超过配置整数宽度 |
| `E_STATE_STALE` | 算子状态版本落后或与当前计划不一致 |
| `E_PLAN_CONFLICT` | 同一版本号对应不同内容摘要 |
| `E_PLAN_ROLLBACK` | 计划版本回退 |
| `E_PLAN_CHANGED` | execute/verify 输入与持久化计划不一致 |
| `E_NO_STATE` | 状态库或当前计划不存在 |
| `E_INCOMPLETE` | verify 发现未完成批次或缺失输出 |
| `E_VERIFY_FAILED` | 持久化状态、输出或全量重算不一致 |

错误输出为稳定 JSON：

```json
{
  "error": {
    "code": "E_BAD_BATCH",
    "details": {"seq": 0},
    "message": "batch seq must be strictly increasing"
  },
  "ok": false
}
```

运行时失败按算子隔离。某算子发生定点溢出时，只将该算子和批次置为失败；
其他已经成功处理的算子状态和结果不会回滚。摘要属于整批内容，因此摘要
不匹配会使该批所有尚未提交的算子进入 `E_BATCH_DIGEST_MISMATCH` 失败态。

## 持久化、恢复与并发

SQLite 使用 WAL、`synchronous=FULL`、`busy_timeout` 和
`BEGIN IMMEDIATE`。每个批次的以下内容在同一事务中提交：

- 算子状态 JSON 与状态摘要
- 批次水位、累计行数
- `applied` 幂等账本
- 每批输出 JSON 与输出摘要
- 分组结果、窗口结果、误差链
- 换算审计与失败记录

读取方只能看到完整提交。进程在事务中死掉时不会留下半批状态；在事务提交后
死掉时，`resume` 通过 `applied` 账本跳过已提交批次。

恢复会检查状态摘要、水位、累计行数、账本和已提交批次集合。发现半提交、
新计划算子或需要重建的窗口算子时，只重建对应算子：清除该算子旧派生表，再
按批次序号重放；不会重复追加持续型窗口缓冲。

多实例可以同时运行。每个批次提交使用跨平台文件锁和 SQLite 写事务双重保护；
若两个实例选中同一批次，后取得锁的实例会看到幂等账本并跳过。测试中两个
并发 `execute` 的 `applied` 表没有重复键，verify 通过。

## 计划替换

计划快照以 `version + content_digest` 标识。`load` 或 `plan replace` 的分类：

- 初始安装：`initial`
- 同版本、同摘要、同批次：`unchanged`
- 追加批次：`incremental_append`
- 旧算子集合是新集合子集：`incremental_append`
- 删除或修改算子：`full_replace`
- 版本更小：拒绝并返回 `E_PLAN_ROLLBACK`
- 同版本不同摘要：拒绝并返回 `E_PLAN_CONFLICT`

每次替换写入 `plans`、`plan_ops` 和 `plan_audit`。算子 ID 由其语义指纹
（分组键、表达式、窗口参数、累加通路和标度）生成。语义未变的算子复用已有
状态；新算子或语义变化的算子只重建自身。`plan show` 可查看快照和审计记录。

## verify 检查

`verify` 从全部有效批次以固定顺序全量重放每个算子，并检查：

1. SQLite `quick_check`
2. 批次状态与批次摘要
3. 每个算子的持久化状态是否与全量重放状态一致
4. 每个分组聚合结果是否与全量重放一致
5. 每个窗口（含跨批次和尾部窗口）是否逐窗口一致
6. 每批输出摘要是否与输出 JSON 一致
7. `results.json` 是否与持久化结果、计划版本和摘要一致
8. 是否存在失败记录、pending 或 partial 批次

## 测试与已验证结果

```powershell
python -m unittest discover -s tests -v
```

当前自动化测试覆盖：

- 固定样本重复执行字节一致
- 全量重算逐分组、逐窗口一致
- 强制终止后 resume 与完整运行字节一致
- 计划增量替换、状态复用、同版本冲突和回退拒绝
- 非法输入不创建状态库
- 定点溢出算子隔离
- 批次摘要篡改进入失败态
- 状态版本陈旧返回 `E_STATE_STALE`
- 并发执行幂等
- `mark` 边界与定点/float64 双换算审计

本工作区实测结果：

- 3 个批次全部 `committed`，6 个算子，12 条分组聚合结果，26 条窗口结果
- 强制退出码 137 后恢复：只补处理 1 个缺失批次，最终输出与完整运行一致
- 定点配置下 A/B 求和结果为 `4.5` 与 `4.5000000001`，差值小于相对容差，
  两者均标记精度敏感
- 定点溢出测试中 `big_sum` 失败，`small_sum` 与 `small_count` 仍提交成功
- 10 个 `unittest` 集成测试全部通过

