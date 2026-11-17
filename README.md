# 多源配置生效顺序冲突诊断与确定性收敛台

本项目从零实现一个纯本地、确定性的多源配置诊断工具。它将顺序规则本身作为可追踪对象，逐键输出来源链、绑定判定、最终生效值、覆盖原因、规则版本与业务码，并将会话快照持久化到 SQLite。相同输入与相同诊断参数会得到逐项一致的报告。

## 快速开始

```bash
# 1. 使用本机缓存 wheel 离线创建 venv、安装 pip 与 NumPy
./scripts/setup.sh

# 2. 激活后用 python / pip 直接调用
source .venv/bin/activate
python --version
pip --version

# 3. 运行完整确定性演示
./scripts/demo.sh

# 4. 运行自动化测试
python -m unittest discover -s tests -v
```

`setup.sh` 不访问网络，使用本机的 pip/setuptools wheel 引导 venv，再安装缓存中的 `numpy-2.4.6-cp311` wheel。若缓存路径不同，可修改脚本中的搜索目录。

本机实际准备结果：

```text
Python 3.11.0rc1
pip 22.0.2
NumPy 2.4.6
SQLite 3.37.2
```

## 项目结构

```text
config_order/
  loader.py        JSON 读取、注入参数、强校验、字段路径
  engine.py        偏序图、环检测、确定性拓扑链、裁决与回溯
  repository.py    规则版本、会话快照、当前视图整表替换、陈旧标记
  replay.py        快照结果逐键重算比较与 E_REPLAY 证据
  verify.py        仅依赖 sqlite3/json/hashlib 的独立数据库验证器
  logging_utils.py 结构化 JSONL 日志
  service.py       加载、诊断、持久化、恢复编排
  cli.py           命令行入口
samples/
  scenario.base.json      初始偏置样本
  scenario.rebiased.json  仅改变偏置后的样本
scripts/
  setup.sh         离线环境准备
  demo.sh          三段式确定性演示
  run_demo.py      演示与断言
tests/
  test_system.py   19 项端到端与单元测试
```

## 输入格式

输入是一个本地 JSON 文件，根对象包含来源注册表、环境集合版本和环境描述。环境包含层级、可用来源、优先级偏置、全局/逐键顺序规则、绑定约束和逐源取值。

```json
{
  "schema_version": 1,
  "environment_set_version": "sample-set-2026-11-17",
  "sources": [
    {"id": "local_baseline", "kind": "baseline", "description": "baseline"},
    {"id": "file_override", "kind": "file", "description": "file"},
    {"id": "runtime_injection", "kind": "runtime", "description": "runtime"}
  ],
  "environments": [
    {
      "id": "prod",
      "level": 2,
      "parent": "staging",
      "available_sources": ["local_baseline", "file_override", "runtime_injection"],
      "priority_bias": {"local_baseline": 100, "file_override": 40, "runtime_injection": 20},
      "order_rules": [
        {"id": "r1", "higher": "runtime_injection", "lower": "local_baseline", "basis": "policy"}
      ],
      "key_order_rules": {
        "service.timeout_ms": [
          {"id": "r2", "higher": "file_override", "lower": "runtime_injection", "basis": "key-policy"}
        ]
      },
      "bindings": {
        "service.endpoint": {"require": ["file_override"], "forbid": ["runtime_injection"]}
      },
      "values": {
        "service.endpoint": {
          "local_baseline": "https://local",
          "file_override": "https://file",
          "runtime_injection": "https://runtime"
        }
      }
    }
  ]
}
```

### 强校验

- 环境 ID 全局唯一；父环境必须存在，层级不能自引用或成环。
- 来源 ID 唯一且属于 `sources` 注册表；类型限于 `baseline`、`file`、`runtime`。
- 环境可用来源必须属于注册表；偏置覆盖每个可用来源且值必须为整数（`bool` 不被当作整数）。
- 规则 ID 在环境内唯一；规则两端必须是该环境的可用来源；`higher` 与 `lower` 不能相同。
- 绑定字段、来源列表类型严格校验；同一键不能同时要求并禁止同一来源，且单键最多一个必需来源。
- 取值键必须引用该环境可用来源。
- 绑定引用不存在或不可用的来源时以 `E_BIND` 拒绝；其余结构性错误以 `E_ENV` 整体拒绝。所有错误均携带 `field_path`。

## 生效链算法

算法标识为 `explicit-topological-bias-tiebreak-v1`。

1. 对每个键只纳入实际提供取值的来源，避免无值来源参与伪排序。
2. 合并全局规则与该键的逐键规则，构建有向偏序图；同一有向对只保留一条稳定规则引用。
3. 对参与者按来源 ID 排序执行 DFS；若发现回边，以 `E_CYCLE` 作为该键唯一主因，并在轨迹中给出环路径、环边和规则依据。
4. 无环时执行 Kahn 拓扑排序。当前入度为 0 的候选来源中，先按优先级偏置降序，再用来源 ID 字典序稳定决胜。显式规则永远约束正序，偏置只裁决规则未约束的候选。
5. 每步记录来源、取值、偏置、前后规则引用、当时的全部就绪候选和选择依据。
6. 链 JSON 去除自身 `digest` 字段后，以键排序、无空白的规范 JSON 做 SHA-256，得到链结构摘要。字典顺序、来源数组顺序和规则数组顺序不会影响摘要。
7. 若链顺序中较早来源的偏置低于较晚来源，记录 `bias_inversions` 与 `non_monotonic=true`，依据为 `EXPLICIT_ORDER_OVERRIDES_BIAS`，用于暴露“显式规则压过偏置”的非单调结果。

## 裁决与覆盖

- 绑定约束过滤链上来源：`require` 指定唯一必需来源，`forbid` 指定禁止来源。
- 所选项是链上首个同时满足绑定的来源。
- 无任何取值、必需来源无值、或所有来源均被禁止时，以 `E_CONFLICT` 作为该键唯一主因。
- 对每个被覆盖来源输出链位置、偏置、与选中来源的偏置差、相关规则依据，原因严格区分：
  - `LOWER_PRIORITY`：来源有效顺序在选中来源之后。
  - `BINDING_FORBIDDEN`：来源被绑定约束禁止。
- 轨迹依次包含 `ORDER_CHAIN`、`BINDING_CHECK`、`SELECTION`、`COVERAGE`；错误键包含 `PRIMARY_CAUSE`。链摘要、所选来源与覆盖项可逐项交叉核对。

## 版本、替换与快照

规则指纹只包含会影响顺序或绑定裁决的输入：来源注册表、环境身份/层级、可用来源、偏置、规则和绑定。取值变化不单独升级规则版本。

SQLite 中 `rule_versions.version` 单调递增。只有当前最新规则指纹再次出现时才复用版本；若 A→B→A，回到 A 也会分配新版本，避免版本回退。每次诊断：

1. 写入 `sessions`、`session_chains`、`session_decisions` 快照。
2. 将同环境集合中旧规则版本的历史裁决标记 `stale=1`。
3. 对 `current_chains` 与 `current_decisions` 按环境集合先清空再整体写入，禁止增量合并。
4. 对比替换前后的键，稳定列出新增、删除、修改以及旧/新所选来源集合。
5. 当前统计只聚合 `current_decisions`，陈旧历史不参与。

恢复时先比较快照规则版本与数据库最新规则版本。若不一致，重算通过后以快照结果整体替换当前视图；重算时逐环境、逐键比较状态、业务码、链摘要、所选来源、生效值、覆盖项与轨迹。任一不一致返回 `E_REPLAY`，并给出环境、键与来源。

## CLI

```bash
# 诊断
.venv/bin/python -m config_order diagnose \
  --input samples/scenario.base.json \
  --db var/diagnosis.sqlite \
  --log var/diagnosis.jsonl

# 恢复并重算
.venv/bin/python -m config_order restore \
  --session-id <session-id> \
  --db var/diagnosis.sqlite \
  --log var/diagnosis.jsonl

# 独立验证会话数据库
.venv/bin/python -m config_order verify \
  --session-id <session-id> \
  --db var/diagnosis.sqlite

# 列出会话
.venv/bin/python -m config_order sessions --db var/diagnosis.sqlite
```

### 异常注入

注入只改变内存中的输入参数或输入对象，不修改实现：

```bash
--inject env-corrupt       # E_ENV，退出码 2
--inject binding-missing   # E_BIND，退出码 2
--inject chain-digest      # E_CHAIN，退出码 3
```

每个错误事件写入 JSONL，包含时间、阶段、业务码、会话 ID、字段路径、环境、键、来源与详情。

## 业务码

| 码 | 含义 | 处理 |
|---|---|---|
| `E_ENV` | 环境/来源/偏置/规则/绑定结构非法 | 输入整体拒绝，输出字段路径 |
| `E_BIND` | 绑定引用不存在或不可用来源 | 输入整体拒绝 |
| `E_CHAIN` | 期望链摘要与计算摘要不一致 | 报告完整性失败 |
| `E_CYCLE` | 某键的显式顺序规则成环 | 该键唯一主因 |
| `E_CONFLICT` | 某键因无值或绑定约束无可裁决来源 | 该键唯一主因 |
| `E_REPLAY` | 快照重算无法逐项复现 | 恢复失败，附环境/键/来源 |

## 实际验证结果

执行时间：2026-11-17，本地 venv。

```text
python -m unittest discover -s tests -v
Ran 19 tests in 1.873s
OK
```

`./scripts/demo.sh` 的实际摘要：

```text
SCENARIO 1: rule_version=1, overall_code=E_CYCLE
statistics={"chain_integrity":0,"conflict":2,"cycle":1,"environments":3,"keys":10,"non_monotonic_keys":7,"resolved":7}
prod.service.mode chain=file_override -> runtime_injection -> local_baseline, selected=file_override
prod.service.endpoint coverage=[runtime_injection:BINDING_FORBIDDEN, local_baseline:LOWER_PRIORITY]
prod.feature.order_probe={"code":"E_CYCLE","cycle_sources":["file_override","runtime_injection","file_override"],"rule_ids":["prod-probe-file-over-runtime","prod-probe-runtime-over-file"]}
prod.feature.conflict_probe={"code":"E_CONFLICT","reason":"NO_ELIGIBLE_SOURCE"}
prod.service.timeout_ms.non_monotonic={"bias_inversions":[...],"non_monotonic":true}

SCENARIO 2: rule_version=2, replacement_strategy=whole-view-replace, changed_key_count=5
prod.service.mode selected file_override -> runtime_injection
prod.service.mode changed_fields=chain_digest,coverage,effective_value,selected_source

SCENARIO 3: snapshot_rule_version=1, latest_rule_version=2, version_mismatch=true
replay_status=PASS, replay_checked_keys=10
replacement_strategy=whole-view-replace
independent_verification=PASS, checks=112, errors=0

SCENARIO 4:
env-corrupt -> E_ENV, exit=2
binding-missing -> E_BIND, exit=2
chain-digest -> E_CHAIN, exit=3

DEMO_RESULT=PASS
```

连续运行两次演示，stdout 逐字节一致：

```text
SHA-256 c40dc9ccd6f4df89fbacbe9b8b6c9b499d936481f6151172b598175655787c1a
```

分别以 `PYTHONHASHSEED=1` 与 `PYTHONHASHSEED=987654` 运行完整演示，输出仍为同一 SHA-256，验证实现不依赖哈希遍历顺序。

回归还验证了：

- `session_decisions` 中版本 1 的 10 条历史裁决全部 `stale=1`，版本 2 当前结论 `stale=0`。
- `current_decisions` 在版本 2 时只有版本 2 的 10 条；恢复旧快照后整体切回版本 1。
- 篡改会话快照后，恢复返回 `E_REPLAY`，并定位到 `prod/service.mode` 及来源。
- 篡改持久化链摘要后，独立验证器返回 `FAIL`，检出 `chain_digest_recompute` 错误。

## 确定性约束

- 环境、来源、键、规则和变化列表都显式排序；实现不依赖 Python 字典插入序或哈希遍历序。
- 规范 JSON 固定 `sort_keys=True`、紧凑分隔符、禁止 NaN/Infinity。
- 报告不包含运行时间或随机 ID；会话 ID 由规范输入与诊断参数摘要确定。
- 时间戳只写入审计日志和数据库元数据，不进入诊断报告判定内容。
- 所有样本与输入均为本地文件，运行过程不访问网络。
