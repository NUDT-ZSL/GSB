# 本地配置中心热更新引擎

这是一个只依赖 Python 标准库与 Flask 的本地配置中心。配置以完整 JSON
快照存储，补丁经逻辑时钟窗口校验后在 SQLite 事务中原子生效；多个客户端在
固定版本上执行确定性数据处理管线，结果永久绑定配置版本与内容摘要。

## 环境与安装

已在以下环境验证：

- Python `3.10.12`
- Flask `3.1.3`
- SQLite `3.37.2`
- Linux 回环地址 HTTP，无 Docker

```bash
python3 -m pip install --user -r requirements.txt
python3 -m unittest discover -s tests -v
python3 scripts/acceptance.py
```

运行期不访问外部网络。`serve` 只允许监听 `127.0.0.1`、`::1` 或
`localhost`。

## 快速开始

```bash
# 初始化示例数据库
python3 configctl.py init --db data/config_hub.db

# 在注入的逻辑时钟 1200ms 上执行 v1 计算
python3 configctl.py compute --db data/config_hub.db \
  --client client-a --round 1 --now-ms 1200 \
  --input samples/input_records.json --output data/result-v1.json

# 在逻辑时钟 2000ms 上应用补丁 2
python3 configctl.py apply --db data/config_hub.db \
  samples/patch_002.json --now-ms 2000

# 在下一轮计算开始时读取当前完整版本 v2
python3 configctl.py compute --db data/config_hub.db \
  --client client-a --round 2 --now-ms 2600 \
  --input samples/input_records.json --output data/result-v2.json

# 应用 v3 并查询 / 验证
python3 configctl.py apply --db data/config_hub.db \
  samples/patch_003.json --now-ms 3000
python3 configctl.py status --db data/config_hub.db
python3 configctl.py verify --db data/config_hub.db \
  --replay-version 1 --input samples/input_records.json

# 恢复未被中断的工作；空闲时也不会改变已提交状态
python3 configctl.py resume --db data/config_hub.db
```

所有命令输出均为键排序的稳定 JSON，错误也使用同一结构：

```json
{
  "error_code": "E_VERSION_CONFLICT",
  "message": "patch is not based on the current configuration version",
  "ok": false
}
```

## 样本与目录

```text
config_hub/                 核心包
  validation.py             快照、补丁、输入、容差校验
  storage.py                SQLite schema 与事务连接
  engine.py                 版本链、热更新、计算生命周期、恢复、验证
  pipeline.py               固定顺序确定性计算
  server.py                 Flask 回环 HTTP API
  cli.py                    子命令实现
configctl.py                CLI 入口
samples/
  config_snapshot.json      v1 初始快照
  patch_002.json            target_version=2 的合法补丁
  patch_003.json            target_version=3 的合法补丁
  input_records.json        固定输入记录
  tolerance.json            时钟回拨与浮点核对容差
tests/test_config_hub.py    17 个自动化测试
scripts/acceptance.py       两次逐字节一致性与强杀恢复验收
VALIDATION.md               实际运行结果记录
```

## 配置快照

快照只接受 `schema_version`、`version`、`parent_version`、
`logical_clock_ms`、`clock_rollback_tolerance_ms`、`namespaces` 和
`content_digest` 这些字段；其中 `content_digest` 可省略。命名空间与命名空间
内配置键都必须唯一。

```json
{
  "schema_version": 1,
  "version": 1,
  "parent_version": null,
  "logical_clock_ms": 1000,
  "clock_rollback_tolerance_ms": 250,
  "namespaces": [
    {
      "namespace": "pipeline",
      "configs": [
        {"key": "aggregate_weight", "type": "number", "value": 1.5},
        {"key": "filter_threshold", "type": "number", "value": 2.0},
        {"key": "output_format", "type": "string", "value": "fixed3"}
      ]
    }
  ]
}
```

快照中的 `content_digest` 可省略；加载器会按规范内容计算并写入数据库。若显式
提供，则必须与计算结果完全一致。

配置键格式为 `^[A-Za-z][A-Za-z0-9_.-]{0,127}$`，命名空间格式为
`^[A-Za-z][A-Za-z0-9_-]{0,63}$`。声明的类型必须是 `number`、`integer`、
`string`、`boolean`、`object`、`array` 之一，且值必须严格匹配声明类型。
JSON 中的重复对象键、`NaN`、`Infinity` 和负逻辑时钟都会被拒绝。

## 补丁与摘要

```json
{
  "schema_version": 1,
  "patch_id": "patch-002-threshold-weight",
  "target_version": 2,
  "base_version": 1,
  "namespace": "pipeline",
  "content_digest": "sha256:5242c1d4d2fea397de663592d4c0eaeef96463b451c83b8dbea84ceffe0c3320",
  "effective_from_ms": 1500,
  "effective_until_ms": 6000,
  "changes": [
    {
      "key": "aggregate_weight",
      "op": "set",
      "type": "number",
      "value": 2.0
    }
  ],
  "description": "optional"
}
```

- `target_version` 必须等于 `base_version + 1`，且 `base_version` 必须等于
  当前已提交版本；否则返回 `E_VERSION_CONFLICT`。
- 生效窗口为闭区间，逻辑时钟必须满足
  `effective_from_ms <= now_ms <= effective_until_ms`。
- `set` 可以新增键或更新已有键，但不能改变已有键的声明类型。
- `remove` 只能删除已存在的键。
- 同一 `patch_id` 的同一规范化内容重复提交返回原版本，`idempotent` 为
  `true`；合法调用即使幂等也会推进逻辑时钟水位。
- 逻辑时钟低于水位且回拨量超过快照中的
  `clock_rollback_tolerance_ms` 时返回 `E_CLOCK_ROLLBACK`。
- 容差文件中的 `clock_rollback_tolerance_ms` 必须与当前快照一致。

摘要规则是：移除 `content_digest` 后，按键排序、紧凑分隔符、
UTF-8、拒绝非有限数生成规范 JSON，再计算
`"sha256:" + SHA-256(canonical_json)`。补丁的 `changes` 在校验时按键排序。

## 确定性计算管线

`pipeline` 命名空间必须包含：

- `filter_threshold`：`number`，过滤阈值；
- `aggregate_weight`：`number`，聚合权重；
- `output_format`：`string`，取值 `fixed3`、`scientific6`、`decimal2`。

计算规则固定如下：

1. 按输入记录原始顺序遍历，保留 `value >= filter_threshold` 的记录；
2. 对保留记录执行左折叠
   `total = total + (value * aggregate_weight)`；
3. 分组聚合同样按原始输入顺序左折叠，最终分组键按字典序输出；
4. 结果 JSON 使用键排序、紧凑分隔符输出；
5. 输出格式只影响确定性格式化的 `summary` 字段。

每条结果都包含 `client_id`、`round`、`config_version`、`config_digest`、
`input_digest` 和 `result_digest`。计算开始时一次性取得完整版本快照；计算
过程中提交的新版本不会改变本轮参数，下一轮才开始使用新版本。

## SQLite 一致性与恢复

主要表：

- `config_versions`：完整旧版本、父版本、配置摘要、逻辑时钟、补丁号；
- `patches`：`prepared`/`applied`/`aborted` 审计记录；
- `state`：当前版本指针与逻辑时钟水位；
- `compute_runs`：计算轮次开始前绑定的版本、摘要、输入；
- `results`：已提交结果及摘要；
- `result_dependencies`：版本到客户端计算结果的依赖记录；
- `client_state`：每个客户端最新轮次与绑定版本。

补丁采用“先准备、后原子提交”：

1. 校验、时钟窗口与目标版本均通过后，在事务中写入 `prepared` 审计；
2. 最终事务同时插入新完整快照、更新补丁为 `applied`、切换当前指针并推进水位；
3. 任何一步崩溃，SQLite 回滚最终事务，`resume` 只重放仍为 `prepared` 的补丁。

计算采用“先绑定、后计算、再原子提交”：

1. 计算前在同一事务中写入 `compute_runs` 绑定与输入摘要；
2. 计算在事务外执行，因此不会持有数据库写锁；
3. 结果、依赖、轮次完成和客户端状态在同一事务中提交；
4. 强制终止只会留下 `in_progress`，`resume` 使用原始绑定版本和输入重算并提交。

所有状态写入使用 `BEGIN IMMEDIATE`、WAL、`synchronous=FULL` 和数据库级文件
锁；读取使用单个读事务，只会看到完整已提交版本。

## HTTP API

```bash
python3 configctl.py serve --db data/config_hub.db --host 127.0.0.1 --port 8765
```

- `GET /health`
- `GET /v1/status`
- `GET /v1/config?version=1`
- `POST /v1/patches` 或 `POST /v1/apply`

提交补丁时，逻辑时钟必须在包装对象的 `logical_clock_ms` 中提供：

```json
{
  "logical_clock_ms": 2000,
  "patch": {
    "schema_version": 1,
    "patch_id": "patch-002-threshold-weight",
    "target_version": 2,
    "base_version": 1,
    "namespace": "pipeline",
    "content_digest": "sha256:5242c1d4d2fea397de663592d4c0eaeef96463b451c83b8dbea84ceffe0c3320",
    "effective_from_ms": 1500,
    "effective_until_ms": 6000,
    "changes": [
      {
        "key": "aggregate_weight",
        "op": "set",
        "type": "number",
        "value": 2.0
      }
    ]
  }
}
```

也可以把补丁字段直接放在请求体顶层，并附带 `logical_clock_ms`，或通过
`?now_ms=2000` 查询参数传入逻辑时钟。

## 校验与回放

`status` 输出当前版本、逻辑时钟水位、版本链、补丁计数和每个客户端的绑定
版本/结果摘要，客户端与版本均按稳定键排序。

`verify` 检查：

- SQLite 完整性与外键；
- 版本号连续、父指针正确、快照摘要/逻辑时钟一致；
- 当前指针指向版本链头；
- 补丁、配置版本和依赖记录互相一致；
- 每个结果的内容摘要、绑定版本、输入摘要与最新客户端状态；
- 使用原始绑定版本和输入重新计算所有结果并核对差异；
- `--replay-version N` 按指定版本回放并报告逐字段差异。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_CONFIG` | 快照、命名空间、类型、摘要或不变量非法 |
| `E_BAD_PATCH` | 补丁结构、摘要、变更或语义非法 |
| `E_BAD_INPUT` | 输入 JSON 或记录非法 |
| `E_BAD_CLOCK` | 逻辑时钟缺失或不是非负整数 |
| `E_CLOCK_ROLLBACK` | 回拨超过配置容差 |
| `E_PATCH_NOT_EFFECTIVE` | 逻辑时钟不在补丁窗口内 |
| `E_VERSION_CONFLICT` | 目标版本、父版本或补丁 ID 冲突 |
| `E_RESULT_CONFLICT` | 同一客户端轮次被绑定到不同输入或结果 |
| `E_NOT_FOUND` | 版本、补丁或端点不存在 |
| `E_INCOMPLETE_WORK` | 验证时仍有未恢复工作（保留错误码） |
| `E_VERIFY_FAILED` | 持久化状态或回放结果不一致（保留错误码） |

## 崩溃测试钩子

`CONFIG_HUB_FAILPOINT` 仅用于测试，可直接硬退出进程而不回滚已提交事务：

- `after_patch_prepare`：补丁审计已提交、最终配置事务尚未开始；
- `before_patch_commit`：最终配置事务已写入但尚未提交；
- `after_compute_start`：计算绑定已提交、计算尚未开始；
- `after_compute_calculated`：结果已算出、结果事务尚未开始；
- `before_result_commit`：结果事务已写入但尚未提交。

示例：

```bash
CONFIG_HUB_FAILPOINT=before_result_commit \
python3 configctl.py compute --db /tmp/hub.db \
  --client client-crash --round 1 --now-ms 1200 \
  --input samples/input_records.json
python3 configctl.py resume --db /tmp/hub.db
python3 configctl.py verify --db /tmp/hub.db
```

实际测试与验收结果见 `VALIDATION.md`。
