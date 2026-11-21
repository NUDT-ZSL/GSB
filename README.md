# 本地基础设施派生量舍入账本引擎

本项目实现一个不依赖网络或外部服务的确定性引擎：读取本地拓扑、分层参数、账本配置与参数补丁，逐项登记定点换算和乘加的舍入余量，沿依赖 DAG 合成派生量，并将定点路径与高精度 `decimal` 真值路径逐项对照。所有轮次、登记、派生量、误差链、敏感标记与逻辑时钟均写入 SQLite；进程在登记或对照中途被终止后，可以只补算缺失部分。

## 环境与运行

实际验证环境：

- Python `3.10.12`
- `numpy 1.26.4`（环境检查项；引擎本身不需要第三方包）
- SQLite `3.37.2`（Python 标准库）
- `decimal`、`fractions`、`sqlite3`、`fcntl` 均为标准库
- 无 Docker、无网络访问

入口支持以下等价形式：

```bash
python3 -m ledger_engine --db ledger.sqlite3 --sample samples/default <command>
python3 ledger.py --db ledger.sqlite3 --sample samples/default <command>
./ledger --db ledger.sqlite3 --sample samples/default <command>
```

安装为命令（可选）：

```bash
python3 -m pip install -e .
infra-ledger --db ledger.sqlite3 --sample samples/default status
```

默认路径为 `./ledger.sqlite3` 与 `./samples/default`，也可用 `LEDGER_DB`、`LEDGER_SAMPLE` 环境变量覆盖。

## 子命令

```bash
# 校验并载入基础样本；相同样本重复执行幂等
python3 -m ledger_engine load

# 首次诊断，或对已提交的同一版本做幂等复用
python3 -m ledger_engine diagnose --workers 4

# 应用补丁；补丁按出现顺序应用，相同 patch_id 重复提交幂等
python3 -m ledger_engine diagnose \
  --patch samples/default/patches/01-api-a-retry.json

# 恢复被强制终止的待提交轮次
python3 -m ledger_engine resume

# 稳定排序 JSON：派生量、定点值、真值、绝对/相对偏差、误差上界、来源链、敏感比较
python3 -m ledger_engine status

# 重新校验真值、区间、账本算术、来源链、敏感标记、结果哈希与当前指针
python3 -m ledger_engine verify
```

所有命令都接受 `--out <file>` 原子写文件，并向 stdout 输出同一份 JSON。默认 JSON 使用 `sort_keys=True`、固定缩进与 `\n` 结尾；`--compact` 输出单行规范 JSON。命令不使用时间戳、PID、随机 UUID 或数据库绝对路径，因此相同本地样本、相同命令序列在不同数据库上的输出逐位一致。

## 固定样本

`samples/default` 内容与随机种子均已固化：

- `topology.json`：8 个服务、依赖上游、来源层；DAG 含扇入与扇出。
- `layers.json`：8 层的超时、重试倍率、采样率、容量、权重。
- `ledger_config.json`：每参数标度与误差预算、派生量标度、比对容差、逻辑时钟步进、并发上限、真值精度与舍入模式。
- `manifest.json`：固定随机种子 `20261122`、文件清单与补丁顺序。
- `patches/01-api-a-retry.json`：修改会产生新余量结转的分数参数。
- `patches/02-db-primary-capacity.json`：修改一个叶子容量，验证依赖闭包失效。
- `patches/03-delete-api-a-retry.json`：删除补丁覆盖并恢复不可变种子值，验证删除型失效。

样本故意保留多位小数，使 `0.5` 个定点单位等余量真实出现。

## 数据模型与公式

参数顺序固定为：

```text
timeout_ms, retry_multiplier, sample_rate, capacity, weight
```

派生量顺序固定为：

```text
worst_chain_timeout, effective_capacity, weighted_sample_rate
```

其中 `upstream(s)` 是 `s.depends_on` 中服务的集合：

```text
worst_chain_timeout(s)
  = fixed_mul(timeout_ms(s), retry_multiplier(s))
    + max(worst_chain_timeout(u) for u in upstream(s))

effective_capacity(s)
  = capacity(s)                              if upstream(s) is empty
  = min(capacity(s), sum(effective_capacity(u))) otherwise

numerator(s)   = sample_rate(s) * weight(s)
                 + sum(weighted_sample_rate(u) * weight(u))
denominator(s) = weight(s) + sum(weight(u))
weighted_sample_rate(s) = numerator(s) / denominator(s)
```

若分母为零，采样率定义为 `0`。真值路径在配置声明的 `truth_precision`（样本为 80）与 `truth_rounding`（样本为 `ROUND_HALF_EVEN`）下，从原始参数全量重算，不复用定点中间值。

## 定点换算与余量结转

每个服务版本中，每个参数只换算一次。对精确分数 `v`、标度 `s`、结转 `c`（`0 <= c < 1` 个定点单位）：

```text
raw_units = v * 10^s + c
fixed_units = floor(raw_units)
discarded = raw_units - fixed_units
next_carry = discarded
```

登记条目记录：

- `registration_no`：服务内稳定序号；换算固定占用 `1..5`。
- `direction`：`FLOOR` 或 `EXACT`。
- `discarded_units`：精确分数 `"n/d"`；`discarded_value`：真实单位下的十进制表示。
- `carry_in` / `carry_out`：精确分数。
- `weight_position`：例如 `service.api-a.retry_multiplier` 或 `upstream.api-b.worst_chain_timeout`。
- `bound_after`：该操作输出点的误差界。

参数结转保存在 `param_carries`，仅在新版本实际重算该参数时并入；未变参数的登记直接复用。样本中 `api_a.retry_multiplier` 的种子换算产生 `1/2` 单位余量；补丁改为 `1.3333345` 后，`carry_in = 1/2`，输出为 `1333335`，余量归零。这个行为由测试直接断言。

算术顺序固定：

- 超时：先算本服务 `timeout * retry`，再与固定选择出的最大上游链路相加；最大值的平局按依赖服务名的稳定顺序选择。
- 容量：先算本服务容量，再按依赖名顺序累加上游容量，最后与上游总和取最小值。
- 采样率：本服务项、上游项、分子与分母均按依赖名顺序累加，最后做一次除法。

## 误差界与缺陷定位

每个参数真值区间为：

```text
[value - configured_error_budget, value + configured_error_budget]
```

乘加区间按无符号区间传播；每次定点输出可能截断。最终派生量的误差上界是：

```text
max(abs(fixed - interval_low), abs(fixed - interval_high))
```

该界同时覆盖输入预算传播、截断误差与链路累计。每个派生量输出 `error_interval`、`error_bound`、`ledger_ok` 与 `first_defect`。若精确绝对偏差超过区间上界，则：

- `ledger_ok = false`；
- `first_defect` 给出来源链中第一个无法用其 `bound_after` 解释偏差的登记，含服务、登记序号、操作、参数、该登记误差界与未解释超额。

`verify` 额外检查真值是否位于持久化区间内、区间界是否可由定点值重算，以及每条登记算术是否可重现。

## 账本敏感比较

对每个派生量，枚举排序后的服务对。若真值差严格小于 `compare_tolerance`，输出敏感记录：

- 两侧服务名、`fixed`、`truth`、绝对/相对偏差、误差上界；
- `fixed_delta`、`truth_delta`、`fixed_order`、`truth_order`；
- 提高 `higher_scale_increment` 个标度后的两侧值和大小关系；
- `would_flip_at_higher_scale`；
- 两侧参与账本链的 `(service, registration_no)`；
- `within_tolerance` 与 `ordering_depends_on_delta`。

敏感记录单独存入 `sensitive_pairs`。`verify` 仅依据持久化派生真值与固定值重新生成整个敏感列表并逐条比较，因此结论可从账本重新推导。

## 补丁、闭包与复用

补丁操作：

```json
{"op": "set_param", "layer": "api_a", "parameter": "retry_multiplier", "value": "1.3333345"}
{"op": "delete_param", "layer": "api_a", "parameter": "retry_multiplier"}
```

种子样本是不可变基线；`delete_param` 删除补丁覆盖并恢复种子值。这样删除后配置仍可合法计算，不会把“删除”实现为静默缺参。

失效计算以派生量为粒度：

- `worst_chain_timeout` 依赖自身及全部上游的 `timeout_ms`、`retry_multiplier`。
- `effective_capacity` 依赖自身及全部上游的 `capacity`。
- `weighted_sample_rate` 依赖自身及全部上游的 `sample_rate`、`weight`。

因此只修改容量不会重算超时或采样率；下游只在相关指标上失效。每个服务的决策记录：

- 触发来源，如 `layers.api_a.retry_multiplier`；
- 重算/复用的换算参数；
- 重算/复用的派生量；
- 复用的登记序号列表。

未受影响服务的 `derived`（含定点值、真值、偏差、误差界与 `sources`）逐字段保持不变；测试对 `db-primary`、`db-replica`、`api-b`、`worker-a`、`worker-b` 做完整字典比较。

## SQLite 事务与中断恢复

核心表：

```text
base_sample, config_versions, applied_patches, rounds, current_state
service_results, ledger_entries, sensitive_pairs, param_carries, meta
```

轮次流程：

1. 版本变化时原子创建 `PENDING` 轮次、版本快照、补丁记录，并预留逻辑时钟。
2. 每个服务的登记先在独立事务中写成 `STAGED`；服务结果与全部登记在下一事务中转为 `COMMITTED`，同时更新参数结转。
3. 所有服务完成后，敏感对与最终失效决策写入，最后在同一事务中：
   - 将轮次置为 `COMMITTED`；
   - 写结果哈希；
   - 整体替换 `current_state.round_id` 指针。
4. 崩溃后可依据 `STAGED` 行、已提交服务与 `param_carries` 识别进度；`resume` 重新计算服务时按 `registration_key` 核对已有登记，只插入缺失项。

唯一键 `(round_id, service, registration_no)` 与 `(round_id, service, registration_key)` 防止序号或登记重复；相同补丁、相同服务结果、相同敏感记录均做内容核对后再幂等返回。

文件锁 `<db>.lock` 串行化补丁应用、轮次创建与最终发布；多进程同时启动时，后续实例在获取锁后看到已提交轮次并复用。SQLite 使用 WAL、`BEGIN IMMEDIATE`、`synchronous=FULL` 与 60 秒 busy timeout。

可重复复现中断：

```bash
LEDGER_CRASH_AFTER_STAGED_SERVICE=3 \
  python3 -m ledger_engine --db /tmp/x.sqlite3 diagnose --workers 2
# 进程退出码 137，前两个服务已提交，第三个服务的登记为 STAGED
python3 -m ledger_engine --db /tmp/x.sqlite3 status
python3 -m ledger_engine --db /tmp/x.sqlite3 resume
python3 -m ledger_engine --db /tmp/x.sqlite3 verify
```

`LEDGER_CRASH_AFTER_COMMITTED_SERVICE=<n>` 可模拟“服务已提交、轮次尚未发布”的中断。

## 错误码

所有业务错误输出 `{"ok": false, "error": {"code", "message", "details"}}`，退出码 `2`；`verify` 不一致退出码 `1`；内部异常退出码 `3`。

主要错误码：

```text
E_BAD_JSON        JSON 解析错误
E_BAD_TOPOLOGY    服务重复、上游缺失、依赖名非法
E_CYCLE           自环或依赖图成环
E_BAD_SCALE       标度缺失、非整数、越界（0..18）
E_BAD_BUDGET      预算缺失、负数或非有限数
E_BAD_PARAM       超时/容量非正整数、采样率越界、权重为负等
E_BAD_TOLERANCE   比对容差非正
E_BAD_PATCH       补丁结构或目标非法
E_PATCH_CONFLICT  相同 patch_id 内容不同
E_SAMPLE_CONFLICT 数据库来自不同基础样本
E_LEDGER_CONFLICT / E_REGISTRATION_CONFLICT / E_RESULT_CONFLICT
```

校验发生在任何状态写入之前；非法样本或首次诊断时非法补丁不会创建数据库或锁文件。

## 验证结果

实际执行：

```bash
python3 -m unittest discover -s tests -v
```

结果：`10` 个测试全部通过，覆盖：

- 环、非法标度、非法补丁均无持久化状态；
- 定点换算、`1/2` 余量结转与固定登记序号；
- 两次全新数据库运行的 `load/diagnose/status/verify` 输出逐位相同；
- 同一版本重复诊断幂等；
- 容量补丁的派生量级失效闭包与未受影响服务字段不变；
- 删除补丁恢复种子值；
- `LEDGER_CRASH_AFTER_STAGED_SERVICE=3` 后 `resume`，无重复登记、无遗留 `STAGED`；
- 4 个并发诊断实例只产生 1 个轮次、8 个服务结果，无重复登记序号；
- 人为篡改账本后 `verify` 返回非零并报告 `E_LEDGER_ARITHMETIC` / `E_SOURCE_MISMATCH`；
- 人为制造无法解释偏差时 `ledger_ok=false` 且给出首个缺陷登记。

`scripts/run_demo.sh` 会顺序执行基础诊断、三个补丁、状态与校验，并在两个独立目录比较所有 JSON 文件；实际输出所有文件均为 `IDENTICAL`。`scripts/run_crash_recovery.sh` 记录强制中断与恢复结果。更详细的实测记录见 `artifacts/verification.md`。
