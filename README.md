# 多级健康探测状态机

一个完全本地、确定性的多级健康探测诊断工作台。它从固定种子样本生成注册清单、观测流、v1/v2 阈值快照和补丁，完成跨版本阈值合并、注入时钟打分、四态迁移、SQLite 原子账本、SIGKILL 恢复、增量闭包重算和多进程幂等提交。

不依赖真实服务、网络账号或 Docker；Flask 仅提供可选本地状态接口。

## 快速开始

```bash
python3 -m pip install -r requirements.txt

# 1. 生成固定种子样本（种子号 20261126）
python3 -m health_machine seed --out samples

# 2. 校验并装载注册清单与观测流
python3 -m health_machine load \
  --samples samples \
  --db state/hsm.sqlite

# 3. 校验 v1/v2，合并统一阈值视图
python3 -m health_machine merge \
  --samples samples \
  --db state/hsm.sqlite \
  --output artifacts/unified_thresholds.json

# 4. 推进窗口状态机
python3 -m health_machine probe --db state/hsm.sqlite

# 5. 稳定排序 JSON 状态账本
python3 -m health_machine status --db state/hsm.sqlite

# 6. 任意历史窗口/全量账本重算比对
python3 -m health_machine verify --db state/hsm.sqlite
python3 -m health_machine verify --db state/hsm.sqlite --instance inst-outage --window-index 5

# 7. 中断后恢复（识别已提交窗口和 received 半提交观测，只重放缺失后缀）
python3 -m health_machine resume --db state/hsm.sqlite

# 8. 增量补丁
python3 -m health_machine patch --db state/hsm.sqlite \
  --file samples/patches/01_adjust_flap.json
```

一键验收：

```bash
bash scripts/acceptance.sh
python3 -m pytest -q
```

可选 Flask：

```bash
python3 -m health_machine web --db state/hsm.sqlite --host 127.0.0.1 --port 5050
# GET /healthz
# GET /api/status?instance_id=inst-outage
# GET /api/verify?instance_id=inst-outage
```

## 固定样本轨迹

样本包含 4 个回环实例、每实例 3 个探测级别、窗口长度 `1000ms`、超时 `2500ms`。

- `inst-steady`：全程健康，v2 新逻辑时钟阈值生效。
- `inst-flap`：`healthy -> degraded -> lost -> healthy`，演示同时钟、同代数时 v2 格式优先级。
- `inst-outage`：`healthy -> degraded -> lost -> isolated -> healthy`，第 3 级阈值被 v2 墓碑排除，隔离后需 4 个恢复窗口且逻辑时长至少 `3000ms`。
- `inst-timeout`：观测时钟从 `1000ms` 跳到 `5000ms`，仅依据注入逻辑时钟判定 `4000ms > 2500ms` 超时。

## CLI

所有业务命令均输出 JSON；失败时输出：

```json
{
  "ok": false,
  "error": {
    "code": "HSM-1003",
    "message": "...",
    "location": {"instance_id": "inst-a", "level": 2, "path": "..."}
  }
}
```

退出码：`2` 输入/业务错误，`6` 校验不一致，`130` 可处理终止。

| 子命令 | 作用 |
| --- | --- |
| `seed` | 生成确定性注册表、观测、v1 单文件、v2 分片 + SQLite 元数据库和补丁 |
| `load` | 校验并幂等写入注册表与观测收件箱 |
| `merge` | 校验两代阈值，生成带来源、时间换算、失败数数组和裁决依据的统一视图 |
| `probe` | 提交缺失窗口；`--instance` 处理单实例，`--workers N` 可多进程处理不同实例 |
| `patch` | 阈值修改、实例 ID 变更、墓碑标记；输出依赖闭包和可复用实例 |
| `resume` | 输出恢复水位、重放缺失后缀并执行校验 |
| `status` | 按实例 ID 稳定排序输出状态、各级别最新得分、连续计数、阈值代数和时钟水位 |
| `verify` | 在内存账本中重放并做字段级 diff；可用 `--window-index` 检查任意窗口 |

## 关键规则

- 窗口：`window_index = floor(clock_ms / window_ms)`，完全使用注入逻辑时钟。
- 级别分（基点，满分 `10000`）：
  - 可用率分：`10000 * successes / (successes + failures)`；无计数时为 `0`。
  - 延迟分：延迟不超过阈值预算为 `10000`，否则按 `floor(10000 * budget / latency)`。
  - 级别分：`floor((70 * availability_bp + 30 * latency_bp) / 100)`。
  - 窗口分：使用统一阈值视图中的整数权重加权并向下取整。
- 联合失败裁决：级别必须同时满足 `score_bp < min_score_bp` 与 `failures >= failure_limit`；注入时钟超时也可形成失败窗口。
- 状态升级：连续失败 `1/2/4` 个窗口分别进入 `degraded/lost/isolated`，只能沿严重度升级。
- 恢复：好窗口累计恢复弧；`degraded/lost/isolated` 分别要求 `2/3/4` 个窗口和 `1000/2000/3000ms` 连续逻辑时长。隔离不会因重启、墙钟或单个好窗口解除。

## 持久化与一致性

SQLite 数据库保存：

- 实例/服务注册信息；
- `observation_inbox`：`(instance_id, level, seq)` 主键和 `received/committed` 状态；
- `threshold_candidates`、`threshold_view`、`unified_pointer`；
- 窗口汇总、级别得分；
- `instance_state`：状态、连续失败/恢复计数、恢复弧、窗口水位、各级别已提交序号、逻辑时钟水位、阈值指针和状态哈希；
- 哈希链 `transition_log` 与独立 `recovery_arcs`；
- 补丁、闭包、重算审计和幂等命令记录。

窗口汇总、级别得分、迁移记录、恢复弧、观测提交标记和实例状态在同一个 `BEGIN IMMEDIATE` 事务中提交。`fcntl.flock` 按实例加锁，SQLite 唯一约束和 busy timeout 提供多进程隔离。

测试包含真实 `SIGKILL`：在首个窗口事务提交前杀死进程，事务回滚；恢复时识别 30 条 `received` 观测和 0 个已提交窗口，重放 10 个窗口后状态哈希、得分和迁移链与一次性执行完全相同。

## 主要错误码

| 错误码 | 含义 |
| --- | --- |
| `HSM-1000` | JSON 不可读、语法损坏或基础输入结构无法解析 |
| `HSM-1001` / `HSM-1002` | 服务名/服务 ID 重复、实例 ID 重复 |
| `HSM-1003` | 非回环端点或端口/URL 非法 |
| `HSM-1004` / `HSM-1005` | 级别集合为空/非法、级别重复或非严格递增 |
| `HSM-1006` | 长期状态世代非法 |
| `HSM-1101` / `HSM-1102` | 观测引用未知实例/级别 |
| `HSM-1103` | 观测序号或流序号未严格递增/重复 |
| `HSM-1104` | 逻辑时钟为负或倒退 |
| `HSM-1105` / `HSM-1106` | 计数、延迟不合法 |
| `HSM-1201` / `HSM-1202` / `HSM-1203` / `HSM-1204` | v1 长度、阈值、时间或实例引用错误 |
| `HSM-1301` / `HSM-1302` / `HSM-1303` / `HSM-1304` | v2 哈希、代数单调性、墓碑或分片元数据错误 |
| `HSM-2001` | 合并后没有可用活动阈值 |
| `HSM-4001` | 非法状态迁移 |
| `HSM-5001` | 恢复/校验账本不一致 |
| `HSM-6001` / `HSM-6002` | 补丁非法或补丁 ID 内容冲突 |
| `HSM-7001` / `HSM-7002` | 并发提交内容冲突、快照版本内容冲突 |
| `HSM-8001` | verify 发现字段级差异 |

## 目录

- `health_machine/`：CLI、校验、合并、打分、引擎、补丁、恢复校验、Flask。
- `tests/`：状态机、错误码、补丁闭包、SIGKILL/SIGTERM、多进程并发、Flask 和篡改检测。
- `samples/`：运行 `seed` 后生成的固定样本。
- `scripts/acceptance.sh`：本地验收脚本。
- `TECHNICAL_NOTES.md`：需求到实现的映射和验证说明。
- `artifacts/`：实际验收输出与校验报告。
