# 本地确定性 QoS 整形、队列调度与验证管线

本项目在本地逻辑时钟上实现一套可复现的 QoS 数据处理管线：DSCP/五元组分类、双令牌桶整形、逐类 FIFO 队列、Tail Drop/WRED 裁决、层级加权轮询（WRR）、派生吞吐/时延估计、误差链与精度敏感标记，以及 SQLite 原子提交和强杀恢复。运行过程不访问真实网卡、不抓包、不调用外部服务。

## 环境

正式目标环境为 Windows + Python 3.11 或更高版本。代码兼容 Python 3.10 以上；本次实际验证环境是 Linux 容器中的 Python 3.10.12、SQLite 3.37.2、NumPy 1.26.4。

Windows PowerShell 准备：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e . --no-build-isolation
```

NumPy 只用于独立交叉校验测试；运行时只依赖 Python 标准库（包括 `sqlite3`、`fractions`、`json`）。依赖安装完成后，验证脚本不需要网络。

## 快速运行

```powershell
python -m qos_pipeline load --manifest samples/manifest.json --db artifacts/qos.db
python -m qos_pipeline shape --db artifacts/qos.db --no-steps
python -m qos_pipeline status --db artifacts/qos.db
python -m qos_pipeline verify --db artifacts/qos.db --no-steps
```

省略 `--no-steps` 时，`shape`/`resume` 会输出全部步进的逐类令牌、余量、队列、通过/等待/丢弃字节、事件裁决、调度额度、轮询次数和原因。输出始终为键排序、稳定缩进的 JSON。

恢复与故障注入：

```powershell
# 在第 12 步完成 refill 后强制退出，数据库仍停在水位 11
python -m qos_pipeline shape --db artifacts/qos.db --max-steps 20 `
  --crash-at-step 12 --crash-point after_refill --no-steps

python -m qos_pipeline resume --db artifacts/qos.db --no-steps
python -m qos_pipeline verify --db artifacts/qos.db --no-steps
```

`--crash-point` 支持 `after_refill`、`after_schedule`、`before_commit`。前两者在内存令牌或调度已更新、但事务尚未写入时使用 `os._exit(137)`，用于验证 SQLite 不会留下半步状态。

## 固定样本

`samples/manifest.json` 引用固定的 `config.json` 与 `arrivals.json`，并记录两者的 SHA-256、注入编号和种子：

- 样本 ID：`qos-fixed-hierarchical-001`
- 注入 ID：`inject-20261121-deterministic`
- 种子：`20261121`
- 逻辑步长：`5 ms`，共 `120` 步
- 滑动窗口：`250 ms`
- 事件数：`236`
- 层级：根类 `access`，叶子类 `interactive`、`bulk`、`telemetry`
- 分类：五元组端口 443、DSCP 46、DSCP 10、DSCP 8
- 丢弃：`interactive`/`telemetry` 为尾丢弃，`bulk` 为确定性 WRED

`python scripts/make_sample.py` 可按固定种子重建样本。manifest 中的文件哈希会随内容固定，重复生成应得到相同文件。

## 输入契约

每个整形类包含：

```json
{
  "name": "bulk",
  "parent": "access",
  "cir_bps": 6000000,
  "pir_bps": 8000009,
  "committed_burst_bytes": 12000,
  "peak_burst_bytes": 24000,
  "queue_depth_bytes": 18000,
  "drop_policy": "wred",
  "weight": 2,
  "guarantee_bps": 5000000,
  "wred": {
    "min_bytes": 6000,
    "max_bytes": 16000,
    "max_probability_ppm": 350000
  }
}
```

事件包含严格递增的 `seq`、正整数 `bytes`、非负整数 `arrival_ms`、叶子类 `class`，并可带 `dscp` 或完整 `five_tuple`。分类规则按数组顺序首次匹配；若事件显式给出类别，则分类结果必须与之一致。

`arrival_ms` 落在半步内时，在该时刻之后的第一个逻辑步边界进入管线；等待时间仍从真实 `arrival_ms` 计算。

校验失败返回稳定的业务错误码，例如 `E_CLASS_DUPLICATE`、`E_PEAK_BURST`、`E_RATE_ORDER`、`E_PARENT_UNKNOWN`、`E_HIERARCHY_CYCLE`、`E_GUARANTEE_SUM`、`E_EVENT_SEQ`、`E_EVENT_TIME`、`E_SAMPLE_HASH`。完整错误码表在 `src/qos_pipeline/errors.py`。所有校验在打开写事务前完成；非法样本不会修改已加载数据库。

## 关键语义

固定点标度为 `1000` 单位/字节，令牌分母为 `8000`。速率换算为：

```text
increment = floor(bps * step_ms * 1000 / 8000)
remainder = (bps * step_ms * 1000) mod 8000
```

承诺桶和峰值桶分别保存 `remainder`。每次补充时先加承诺桶，承诺桶溢出仅在峰值桶有空间时转入峰值桶，随后再加峰值速率增量。发送时严格先扣承诺桶、再扣峰值桶；任何类、任何父类都不可能透支。

每步顺序固定为：

1. 所有类按配置顺序补充双桶。
2. 处理本步到期事件，按队列深度和 Tail/WRED 规则裁决入队或丢弃。
3. 从根类开始递归执行层级 WRR；父类预算不超过其整形后可用令牌，子类额度先满足结转、保证带宽，再按权重分配。
4. 叶子队列按 FIFO 整事件出队；父链逐级扣令牌。
5. 生成完整步进载荷和状态快照，原子提交 SQLite。

未用完的调度额度按子类结转，设有由突发深度/队列深度确定的上限。轮询起点每步稳定前进一位，余数分配按“最大余数、配置顺序”稳定打破平局。

WRED 的随机量由 `splitmix64(seed XOR class_hash XOR seq XOR step)` 生成，不依赖进程随机数。输出同时记录丢弃候选、概率 ppm、随机值 ppm、入队前后队列字节和选择依据。

## 派生指标与误差

`verify` 和 `shape` 的 `derived` 部分包含：

- 每个类别的窗口通过字节、观测吞吐、P50/P95/最大/平均等待；
- 每个逻辑步的滑动窗口序列（窗口边界、通过字节、吞吐、P50/P95）；
- 按路径最小 PIR 得到的可达吞吐上限；
- 流式滑动窗口估算与逐事件全量真值的逐字段比较；
- 定点换算台账，包括原始浮点文本、标度、公式、分子、分母、商、余数、舍入方向和误差上界；
- 每类误差传播上界与参与事件序号；
- 吞吐差或兄弟类调度额度差小于容差时的精度敏感标记。

固定点余量不会丢弃。对某个桶而言，长期补充值与精确有理数的差始终小于一个固定点单位；因此令牌传播上界按每类两个桶记录为 `< 0.002 byte`。吞吐、队列和时延聚合使用整数或 `Fraction` 精算，正常路径没有额外浮点累计。

窗口分位时延使用最近秩法：排序后索引为 `ceil(q*n)-1`。流式与真值两路若分歧，会比较中间窗口、通过字节、事件序号和聚合字段，并分类为 `fixed_point_conversion`、`token_accumulation_error` 或 `scheduling_order_difference`。

## SQLite 一致性

数据库使用 WAL、`synchronous=FULL`、`BEGIN IMMEDIATE` 和事务内提交。每行 `steps` 同时保存：

- `(step_index, version)` 主键；
- 逻辑时间、前序摘要、步进摘要；
- 完整步进 JSON；
- 完整提交后状态 JSON。

只有载荷和状态都在同一事务中写入后，读者才能看到该步。提交前会再次检查数据库水位；若另一个实例先提交同一步，当前实例收到 `E_STALE_COMMIT` 并重新加载已提交状态，不会重复消耗令牌或发送字节。恢复直接读取最后完整状态，事件游标、队列、令牌余量、WRED 输入序列和 WRR 结转均在其中。

## 验证命令

```powershell
python -m unittest discover -s tests -t . -v
python scripts/compare_runs.py --manifest samples/manifest.json
python scripts/recovery_check.py --manifest samples/manifest.json
python scripts/run_validation.py
```

`run_validation.py` 会重新执行单元测试、双运行比较、三种强杀点恢复和完整验收，并写入 `artifacts/validation_summary.json`。

## 实际验证结果

当前环境：Python 3.10.12、SQLite 3.37.2、NumPy 1.26.4。目标 Windows/Python 3.11 未在本容器中可用，但实现未使用 3.11 专有语法。

- 单元测试：`28/28 OK`。
- 样本摘要：`603d386cf28d0ebd3a4d324d26946f9f5e22568b89914e5a24b0d7693c422f8f`
- 120 步最终状态摘要：`395ba5406aa3c5a1e8a64b7550953c95ef203b468f2afd012c3cc282af3bc15f`
- 最后一步摘要：`407b3ec6a38e4836106edd68b325ca4e5de24b333b0d7f2914f6172055181d8a`
- 事件处置：通过 172 个、丢弃 64 个、结束时等待 0 个。
- 两次独立运行：样本摘要、状态摘要、120 个步进摘要链、派生结果全部一致。
- 流式/真值比较：`3360` 项，全部一致，首个分歧为 `null`。
- 精度敏感标记：4 个，包含参与类别、事件序号和换算来源链。
- 强杀恢复：`after_refill`、`after_schedule`、`before_commit` 三种点均退出码 137、水位 11、从 12 恢复，最终摘要和逐步摘要链与干净运行一致。
- 四进程并行提交：每个 `step_index` 仅一行，最终 `verify` 通过。

固定窗口（250 ms）结果：

| 类别 | 通过字节 | 丢弃字节 | 观测吞吐 bps | P50 等待 ms | P95 等待 ms |
|---|---:|---:|---:|---:|---:|
| interactive | 60,427 | 20,376 | 162,144 | 3 | 4 |
| bulk | 82,338 | 5,040 | 215,296 | 2 | 4 |
| telemetry | 44,513 | 50,077 | 300,288 | 4 | 14 |

## 项目结构

```text
src/qos_pipeline/
  sample.py       输入解析、业务校验、分类、摘要
  fixed.py        定点换算、余量、稳定整数分配
  engine.py       双桶、队列、WRED、层级 WRR
  metrics.py      流式/真值指标、误差链、精度敏感
  store.py        SQLite 原子提交和状态恢复
  runner.py       步进循环、强杀注入、并发冲突重试
  cli.py          load/shape/resume/status/verify
samples/          固定配置、事件流、注入清单
scripts/          样本生成、双运行比较、恢复检查、验收汇总
tests/            28 个确定性单元与集成测试
docs/DESIGN.md    算法、事务和误差传播说明
```

## 边界与假设

- 事件是不可拆分的整包，一个事件不会在同一队列内乱序或部分发送。
- 分类目标必须是叶子整形类；内部类只承担整形与层级调度。
- 初始时承诺桶和峰值桶均填满。
- `peak_burst_bytes` 是峰值桶独立容量；峰值速率也独立补充该桶，发送按承诺优先消费。
- 逻辑时钟只到 manifest 的 `total_steps`；样本事件必须落在该范围内。
- 本地 SQLite 文件需要可写目录和 WAL 支持；本项目不使用 Docker、真实网络或外部服务。
