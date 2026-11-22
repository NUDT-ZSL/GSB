# FIXPOINT Config Relay

本地配置中心热更新引擎与数值诊断工作台。项目完全离线运行，使用 JSON 快照/补丁、Python 定点整数计算、`decimal` 真值通路、SQLite WAL 事务与 Flask 交互界面。

核心目标不是“把配置写进数据库”，而是回答三个问题：

1. 某个版本中的十进制配置被换成了哪个定点整数，余量去了哪里？
2. 这次热更新是否改变了任务过滤、批大小、聚合权重或输出数量的结论？
3. 进程崩溃、并发提交或计算中途热更新后，结果是否仍只提交一次且绑定正确版本？

## 快速开始

```bash
python3.11 --version              # 目标环境 Python >= 3.11
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python -m config_center bootstrap
python -m config_center status
python -m config_center verify
python -m config_center serve --host 127.0.0.1 --port 5000
# 或者：python app.py
```

打开 <http://127.0.0.1:5000> 进入交互式工作台。

如果需要从空状态手工验证：

```bash
python3 -m config_center reset
python3 -m config_center load data/snapshot.json
python3 -m config_center apply data/patches/002_add_weight_gamma.json
python3 -m config_center compute data/compute_input.json --round-key round-v1
python3 -m config_center apply data/patches/003_full_replace_same_values.json
python3 -m config_center compute data/compute_input.json --round-key round-v3
python3 -m config_center verify
```

实际验证环境已安装并使用 Python 3.11.18；`pyproject.toml` 声明 `requires-python >= 3.11`。

## 演示与验证

```bash
python3 scripts/demo.py
python3 -m pytest -q tests/test_engine.py
```

演示脚本会真实执行以下时序：

1. 加载 v1，计算 `round-v1`，得到精度敏感结果。
2. 启动 `round-crash` 计算并在提交前 `os._exit(86)`。
3. 应用 v2 增量补丁。
4. 启动一个绑定 v2 的延迟计算，运行过程中应用 v3 完整替换。
5. 延迟计算仍以 v2 完成；随后 `resume` 只重放未提交的 v1 作业。
6. 计算 v3、比较 v1/v3、比较高标度探测、运行 `verify`。
7. 将完整结果写入 `artifacts/verification.json`。

实际验证结果记录在 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

## 架构

```text
JSON snapshot / patch
        │
        ▼
严格校验 + canonical SHA-256
        │
        ▼
受控定点换算（floor + 逐键余量）
        │
        ├── conversion_ledger（每次标度/舍入/余量）
        ▼
SQLite WAL
  ├── versions                 不可变版本元数据
  ├── version_configs          版本完整配置集合
  ├── current_pointer          单条当前版本指针
  ├── compute_jobs             pending / committed 作业
  ├── client_results           每客户端每轮次唯一结果
  ├── error_ledger             扁平化误差来源
  └── clock_watermarks         逻辑时钟水位
        │
        ├── CLI: load/apply/compute/resume/status/verify
        └── Flask API + 交互工作台
```

### 原子热更新

- 写入由 `state/center.lock` 文件锁串行化，并使用 SQLite `BEGIN IMMEDIATE`。
- 新版本先写入 `versions`，完整配置通过连接内临时表 `version_stage` 装载，再写入 `version_configs`。
- 最后只更新 `current_pointer` 一行并提交事务。读者要么看到旧指针，要么看到新指针。
- 版本配置不可变，进行中的计算继续读取其绑定版本。
- 目标版本小于当前版本返回 `E_VERSION_ROLLBACK`。
- 重复提交同一 `target_version + content_digest` 返回幂等 `noop`。

### 崩溃恢复

- 计算开始先创建 `pending` 作业并绑定 `bound_version/bound_digest`。
- 计算完成后，结果、扁平误差账本、作业状态和逻辑时钟水位在同一事务中提交。
- 进程在计算前、计算后或提交后被杀：
  - `after-start`: 只有 pending 作业。
  - `before-commit`: 计算完成但没有持久化结果。
  - `after-commit`: 结果完整提交，重复调用返回原结果。
- `resume` 只选择 `pending` 作业，并按原绑定版本重放。
- `client_results(namespace, client_id, round_key)` 唯一约束阻止重复计入派生量。

```bash
python3 -m config_center compute data/compute_input.json \
  --round-key round-crash --crash-at before-commit
echo $?     # 86

python3 -m config_center status
python3 -m config_center resume
```

## 数据契约

### 快照

```json
{
  "namespace": "edge-pipeline",
  "version": 1,
  "logical_clock_ms": 1000,
  "configs": {
    "filter_threshold": {
      "type": "decimal",
      "scale": 10000,
      "role": "filter_threshold",
      "value": "0.13505"
    }
  }
}
```

校验规则：

- JSON 对象重复键、非法 namespace/config key 使用 `E_BAD_CONFIG`。
- 命名空间内配置键唯一；声明 role 在命名空间内唯一。
- `integer` 值必须是 JSON 整数；`decimal` 接受有限十进制/数字字符串。
- 版本号必须严格递增；逻辑时钟必须非负且新版本不得回退。
- 快照可带 `content_digest`；若声明则必须与 canonical SHA-256 一致。
- 非法输入在任何版本行、指针或作业写入前被拒绝。

### 补丁

```json
{
  "namespace": "edge-pipeline",
  "base_version": 1,
  "target_version": 2,
  "logical_clock_ms": 2000,
  "mode": "merge",
  "operations": [
    {
      "op": "set",
      "key": "weight_gamma",
      "type": "decimal",
      "scale": 10000,
      "role": "weight_gamma",
      "value": "0.10005"
    }
  ],
  "content_digest": "sha256:..."
}
```

`content_digest` 的计算对象是补丁自身去除 `content_digest` 后的 canonical JSON；版本内容摘要另行由 namespace、目标版本、逻辑时钟和完整配置集合计算。

可用摘要工具：

```bash
python3 -m config_center digest data/patches/002_add_weight_gamma.json
python3 -m config_center digest data/patches/002_add_weight_gamma.json --write
```

### 版本变化判定

提交时同时比较版本号、摘要和键集合：

- 当前版本、相同补丁摘要：`noop`。
- 新键集合严格包含旧键集合且无删除：`incremental_append`。
- 同键集合值变化、包含删除或显式 `replace`：`full_replace`。
- 目标版本已存在但摘要不同：`E_VERSION_CONFLICT`。

## 定点换算

每个数值配置声明自己的 `scale`。换算采用向负无穷取整：

```text
raw_scaled     = value * scale
adjusted_scaled = raw_scaled + carried_in
fixed_int      = floor(adjusted_scaled)
discarded      = adjusted_scaled - fixed_int       # [0, 1)
carried_out    = discarded
```

每次实际换算都写入 `conversion_ledger`：

- 标度与舍入方向；
- 输入余量 `carried_in`；
- 调整后标量、定点整数；
- 被舍弃余量 `discarded`；
- 结转余量 `carried_out` 与小数形式 `residual`；
- 换算误差 `fixed_int / scale - value`。

增量追加时，未变化的配置继承原换算登记；完整替换会重新换算全部数值键，并把上一版本同键余量作为 `carried_in`。因此同一条逻辑配置在多次完整替换后可能产生不同的定点整数，这正是样本要诊断的业务风险。

## 确定性派生量

事件按 `id` 升序处理，运算顺序固定：

```text
threshold       = floor(filter_threshold_int * retry_factor_int / retry_scale)
effective_weight = weight_alpha_int + weight_beta_int + weight_gamma_int
base_batch      = min(capacity_int, floor(rate_limit_int * timeout_ms_int / 1000))
expected_batch  = floor(base_batch * sample_rate_int / sample_scale)
eligible        = events with value >= threshold
processed       = first expected_batch eligible events by id
weighted_sum    = sum(value * weight * effective_weight) for processed
output_count    = floor(processed_count * output_ratio_int / output_ratio_scale)
```

输入事件的十进制值使用固定 `1_000_000` 标度换算；高标度探测时输入标度同步放大。

同时保留完整 `decimal` 通路作为真值对照。每个派生量输出：

- `fixed`：定点通路值；
- `exact`：decimal 真值；
- `deviation`：定点值减真值；
- `error_bound`：换算误差项 + 运算量化项 + 离散事件集合不确定项；
- `bound_ok`：偏差是否落在上界内；
- `sources`：配置键、原换算版本、换算登记序号、标度、`carried_in`、`discarded`、结转余量、误差贡献。

## 精度敏感与高标度探测

当两个版本或两个客户端的固定路径结论不同，而对应派生量固定值差小于容差且高精度差值更小（通常为 0）时，标记 `precision_sensitive`。

系统会将配置标度和输入标度放大一个探测因子（默认 100），重新换算、过滤、截断批大小并计算输出。若高标度结论回到 decimal 真值，则：

```json
{
  "flips_conclusion": true,
  "explanation": "将各配置标度与输入标度放大 100 倍后，两侧离散结论变为一致..."
}
```

当前样本中：

- v1 `filter_threshold` 定点为 `0.1350`，decimal 真值为 `0.135117525`；
- v3 完整替换重算后，余量结转使定点阈值变为 `0.1352`；
- 事件 `e-001 = 0.13510` 在 v1 固定路径被错误过滤，在 v3 被正确过滤；
- v1/v3 固定阈值差 `0.0002` 小于容差 `0.001`，真值差为 `0`；
- 100 倍标度探测后两版本结论均回到 `["e-002","e-004","e-005"]`，因此高标度会翻转结论。

## 业务错误码

| 错误码 | 含义 | HTTP |
| --- | --- | --- |
| `E_BAD_CONFIG` | 结构、类型、唯一性、role、非法操作 | 400 |
| `E_BAD_CLOCK` | 逻辑时钟为负或版本间回退 | 400 |
| `E_DIGEST_MISMATCH` | 快照/补丁摘要缺失或不匹配 | 400 |
| `E_VERSION_ROLLBACK` | 目标版本低于当前版本 | 409 |
| `E_VERSION_CONFLICT` | 同版本已提交不同摘要/补丁 | 409 |
| `E_ROUND_CONFLICT` | 同客户端轮次绑定了不同输入或结果不完整 | 409 |
| `E_NOT_INITIALIZED` | 命名空间尚无当前指针 | 409 |
| `E_ALREADY_INITIALIZED` | 已初始化后再次 load 更高版本 | 409 |
| `E_NOT_FOUND` | 版本、结果或样本不存在 | 404 |

非法输入不会写入版本、配置、换算账本、指针或结果。

## CLI

```bash
python3 -m config_center load data/snapshot.json
python3 -m config_center apply data/patches/002_add_weight_gamma.json
python3 -m config_center compute data/compute_input.json --round-key round-v1
python3 -m config_center resume
python3 -m config_center status
python3 -m config_center verify
```

通用选项可放在子命令前：

```bash
python3 -m config_center --root /path/to/workspace --data-dir /path/to/data status
```

`status` 始终以 `sort_keys=True` 的稳定 JSON 输出，包含当前版本、当前/客户端逻辑时钟水位、版本链、客户端结果摘要、精度敏感标记与 pending 作业。

`verify` 会复算：

- 版本号、父版本、摘要与配置计数；
- 每次定点换算的输入余量、定点值、舍弃/结转余量与舍入方向；
- 客户端结果摘要、作业状态、误差账本行数与绑定版本；
- 当前指针版本配置集合完整性；
- 定点偏差是否落在误差上界内；
- 版本敏感性及高标度翻转；
- `E_BAD_CLOCK`、`E_BAD_CONFIG`、`E_DIGEST_MISMATCH` 错误码契约。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/overview` | 当前配置、版本链、客户端和统计 |
| `GET` | `/api/status` | 稳定状态 |
| `GET` | `/api/version/<version>` | 不可变版本快照 |
| `POST` | `/api/load` | 加载初始快照 |
| `POST` | `/api/apply` | 提交补丁 |
| `POST` | `/api/digest` | 计算补丁摘要 |
| `POST` | `/api/compute` | 绑定当前版本计算 |
| `POST` | `/api/resume` | 重放未提交作业 |
| `POST` | `/api/compare` | 两版本数值比较 |
| `POST` | `/api/compare-clients` | 两客户端结果比较 |
| `GET` | `/api/verify` | 运行验证器 |
| `POST` | `/api/bootstrap` | 重置并载入示例 |

## 交互工作台

Flask 页面包含六个视图：

- **总览**：版本、定点键、敏感轮次、客户端水位、换算余量与当前配置。
- **版本链**：不可变版本列表、父版本、摘要、完整配置检查。
- **热更新**：补丁编辑器、摘要计算、原子提交、变更分类与错误码契约。
- **计算**：客户端/轮次输入、版本绑定、双通路指标、误差上界和来源链。
- **诊断**：版本选择、容差、标度放大、离散结论翻转说明。
- **验证**：逐项检查、错误码复现与原始 JSON。

## SQLite 状态

默认位置：

```text
state/center.lock
state/config_center.db
```

SQLite 使用 WAL、`synchronous=FULL` 与 `busy_timeout=30000`。完整替换的连接内临时表为 `temp.version_stage`，读者不会查询或观察到该表。

## 测试

```bash
python3 -m pytest -q tests/test_engine.py
```

覆盖 14 项：非法输入零版本写入、重复 JSON 键、快照幂等、版本冲突、回滚拒绝、重复补丁、同一轮次不同输入冲突、增量/完整替换分类、余量结转、重复读取换算稳定性、双通路误差上界、高标度翻转、verify、强制终止恢复、计算中途热更新、6 进程并发 apply/compute、稳定 status 与 HTTP API。

## 约束与说明

- 仅使用本地 JSON 样本与本地 SQLite，不依赖配置中心服务、容器或网络。
- 输入事件换算不跨轮次结转余量；配置键余量按版本链逐键结转。
- 高标度探测是对“提高标度后结论是否变化”的诊断，不修改已提交版本。
- 该应用面向本地诊断与演示，未实现认证、多租户隔离或远程传输加密。

