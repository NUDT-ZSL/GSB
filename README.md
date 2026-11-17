# ML 推理网关指纹诊断工作台

面向本地 Windows / Python 3.11 的确定性推理网关实验。项目重点不是规则依赖图，而是：

> 后端版本指纹如何决定路由命中归属，以及指纹演进后账本、在途批次、统计和快照如何原子演进与重放。

网关在回环 TCP 端口接收 JSONL 请求，按规则优先级匹配后端与批大小，调用每个回环后端端口上的 NumPy 模拟推理服务，并把逐请求命中、批次、指纹、失效原因、统计版本和快照写入本地 SQLite。

## 1. Windows 快速开始

前置条件：

- Python 3.11 官方 Windows 发行版（自带 `tkinter` 和 `sqlite3`）。
- 无需 Docker、WSL、外部服务或云端 API。
- 使用高位回环端口：网关 `24310`，后端 `24311`、`24312`。

```bat
setup.bat
run_tests.bat
run_demo.bat
run_gui.bat
```

也可手工安装：

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ml_gateway.demo
.venv\Scripts\python.exe -m ml_gateway.gui --db artifacts\demo\session.db --backends data\backends_v2.json
```

运行前确认回环端口空闲：

```bat
netstat -ano | findstr ":24310 :24311 :24312"
```

若发现占用进程，结束对应 PID 后再运行；程序不会强杀其他进程，冲突时报告 `E_PORT`。

## 2. 交付内容

- `ml_gateway/declarations.py`：后端、规则、请求的强校验与稳定规则版本。
- `ml_gateway/fingerprint.py`：逐位量化、配置摘要、加载序号和 SHA-256 指纹组合。
- `ml_gateway/engine.py`：路由、确定性分批、账本、失效判定、作废重派、统计版本、快照恢复与重放。
- `ml_gateway/inference.py`、`ml_gateway/services.py`：NumPy 前向计算、后端 JSONL TCP 服务、网关 JSONL TCP 服务。
- `ml_gateway/storage.py`：SQLite 当前视图、只读统计历史、快照和结构化日志。
- `ml_gateway/diagnostics.py`、`ml_gateway/gui.py`：稳定排序诊断报告与 Tkinter 工作台。
- `ml_gateway/demo.py`：三场景演示及 `E_RULE`、`E_VECTOR`、`E_SNAP` 故障注入。
- `data/`：确定性后端声明、规则、参数向量与 8 条请求序列。
- `tests/`：32 项测试，覆盖校验、指纹、路由、演进、重派、恢复、重放、TCP 和 Tk。
- `artifacts/demo/`：最近一次演示产生的 SQLite、结构化日志和三份诊断报告。

## 3. 确定性样本

- `data/backends.json`：`be-alpha`（4 维参数，端口 24311）、`be-beta`（3 维参数，端口 24312）。
- `data/backends_v2.json`：仅把 `be-alpha` 参数末位从 `0.125` 改为下一个 binary64 值 `0.12500000000000003`。
- `data/rules.json`：4 条规则，优先级 10/20/30/100，批大小 2/3/2/3。
- `data/requests.ndjson`：8 条 OCR/文本/兜底请求。
- `data/params/*.json`：原始有限浮点参数数组。

规则按 `(priority, rule_id)` 排序，优先级数值越小越先匹配。同一规则内按请求原始顺序以 `batch_size` 切批；批次再按规则优先级稳定调度。`batch_size` 必须不大于目标后端 `max_concurrency`，执行器以串行批次保证账本确定性，任何时刻批大小都不会突破并发上限。

## 4. 指纹量化口径

报告中的 `fingerprint_quantization` 字段完整复述该口径。实现定义如下：

1. 参数必须是定长有限 IEEE-754 binary64 数组，长度等于声明的 `parameter_length`。
2. 每个参数先只把负零规范化为正零；不进行四舍五入、十进制重格式化或十进制量化。
3. 每个槽位使用 `struct.pack(">d", value).hex()`，即 16 个十六进制字符的大端 binary64 位串。参数末位即使只有一个 ULP 变化，位串也会变化。
4. 配置摘要为以下字段的 canonical JSON 的 SHA-256：`backend_id`、`model_id`、`host`、`port`、`max_concurrency`、`parameter_length`。参数文件路径不进入摘要。
5. 内容签名由参数位串、参数长度、配置摘要推导。
6. 后端本地加载序号从 `0` 开始；内容或配置不变时重开保持序号，显式重载或内容变化时递增。全局加载顺序、线程调度和并发度不进入指纹。
7. 最终指纹：

```text
SHA-256(
  "ml-gateway-fingerprint-v1\0" ||
  canonical_json({
    config_digest,
    content_signature,
    load_sequence,
    parameter_length,
    vector
  })
)
```

格式为 `fp1:<64 位小写十六进制>`。因此：

- 初始声明以任意顺序、任意并发加载，得到相同指纹集合。
- 参数向量末位变化一定改变内容签名和指纹。
- 同一参数向量显式重载后因加载序号变化，指纹也变化。
- 比较范围是“同一后端本地加载代数”；这是同时满足“加载序号参与指纹”和“初始加载顺序无关”的显式口径。

## 5. 账本与指纹绑定

每个有效请求命中记录：请求序号、规则、后端、批次、绑定指纹、绑定加载序号、输出摘要、作废/生效标志、失效原因与重派来源。

某条命中是否仍有效只比较：

1. 后端当前是否存在；
2. `bound_fingerprint == current_fingerprint`；
3. `bound_load_sequence == current_load_sequence`。

不使用时间、推测或缺失字段补齐。SQLite 对 `effective = 1` 建立按 `request_id` 的唯一部分索引，数据库层保证同一请求任一时刻至多一条有效归属。

### 在途批次规则

- 批次开始时先持久化为 `IN_FLIGHT`，绑定当时的后端指纹。
- 若响应返回后、提交前发现指纹变化，旧批次记为 `VOID`，其输出摘要丢弃并保持为空。
- 默认策略 `redispatch_policy = always` 且最多重派一次：同一请求组使用新指纹建立 `redispatch_of=<旧批次>` 的新批次，重新调用后端。
- 新批次输出不复用旧批次摘要；旧、新批次及所有尝试均可在账本与批次页核验。
- 后端不可达时批次状态为 `ERROR`、错误码为 `E_UPSTREAM`，随后执行指纹归属复核。

## 6. 统计版本与整体替换

统计包含每条规则命中数、每个后端命中数、每个后端指纹命中数、规则/后端/指纹缓存项、有效请求数和全局输出索引。

指纹集合变化时：

- 找到变化、删除、增加的后端及受影响规则；
- 将旧指纹的有效归属标为失效；
- 把在途批次标为作废；
- 旧统计版本设为只读；
- 从现行有效归属计算新统计；
- 在 `stats_versions` 元数据中稳定列出受影响后端、受影响规则、失效条数、作废/重派批次、重算键、未变键与变化键；
- 未受影响规则/后端/缓存值保持不变；若实现意外改变未受影响键，状态机会抛出 `E_STATE`；
- 当前视图在单个 SQLite 事务中删除并重建，不做逐条增量修补。

## 7. 快照、恢复与重放

快照包含后端指纹集合、各后端加载序号、规则版本、完整账本、批次、作废重派记录、统计版本、当前状态摘要、请求序列和重放期望。

恢复规则：

- 快照指纹集合与当前声明推导集合一致且规则版本一致：在一个事务中整体用快照当前视图替换当前账本与统计，绝不与漂移状态合并。
- 不一致：报告 `E_SNAP`，并整体初始化/替换为当前声明的新空视图；旧快照仍保留。错误详情列出 added/removed/changed 后端、当前/快照指纹和替换后的统计版本。
- 状态 JSON 摘要不一致：报告 `E_SNAP`，不恢复被篡改快照。

恢复后在当前后端上按快照请求序列重放，逐请求比较绑定指纹、规则、后端、作废标记、输出摘要和错误码。差异报告为 `E_REPLAY`，定位到首个不一致的 0 基 `request_index`、1 基 `request_ordinal`、请求 ID 与字段。

## 8. TCP JSONL 协议

网关监听 `127.0.0.1:24310`。客户端发送一条或多条请求，关闭写端后读取响应；每个请求一行：

```json
{"request_id":"req-001","features":{"task":"ocr","tier":"gold"},"input":[1.0,0.5,-0.25,2.0]}
```

响应示例：

```json
{"backend_id":"be-alpha","batch_id":"b-000001","bound_fingerprint":"fp1:...","duration_ms":23,"output_summary":"out1:...","request_id":"req-001","rule_id":"rule-gold-ocr","status":"OK","void":false}
```

后端服务也使用 JSONL；网关发送含 `batch_id`、`fingerprint` 和 `inputs` 的批次，后端校验指纹后调用 NumPy：

```text
weighted = inputs * parameters
scores = sum(weighted, axis=1) + arange(rows) * parameters[0]
output_summary = SHA-256(canonical_json(scores 的 binary64 位串 + 指纹 + shape))
duration_ms = 11 + (rows * columns * 13 + int(fingerprint[-8:], 16)) % 47
```

耗时是确定性的整数模拟耗时，不把 wall-clock 写入账本，从而保证相同输入与指纹得到逐字节一致的响应。

## 9. SQLite 表

- `current_state`：完整当前视图 JSON、状态摘要、视图/统计/规则版本、指纹集合。
- `backend_current`：后端当前指纹、加载序号、配置摘要与规范参数位串。
- `ledger_current`：当前视图内的全部有效/失效请求归属。
- `batch_current`：`IN_FLIGHT`、`COMPLETED`、`VOID`、`ERROR` 及 `redispatch_of` / `superseded_by`。
- `stats_versions`：只追加统计历史；当前版本 `readonly=0`，其余 `readonly=1`。
- `snapshots`：快照元数据、指纹/加载序号、请求、重放期望、完整状态和摘要。
- `log_events`：与 JSONL 文件镜像的逻辑序号结构化日志。

## 10. 业务码

| 业务码 | 含义 |
|---|---|
| `E_RULE` | 声明/规则结构、唯一性、端口、区间、条件字段或目标后端非法 |
| `E_VECTOR` | 参数文件缺失/非法、非有限值、长度不匹配 |
| `E_INPUT` | 请求 JSON、特征、输入向量或输入长度非法 |
| `E_ROUTE` | 无规则匹配 |
| `E_UPSTREAM` | 后端不可达、协议失败或后端拒绝批次 |
| `E_SNAP` | 快照缺失、摘要错误、指纹集合或规则版本冲突 |
| `E_REPLAY` | 重放与快照记录不一致 |
| `E_PORT` | 回环端口占用或绑定失败 |
| `E_STATE` | 内部状态或统计不变量失败 |

## 11. 演示与故障注入

```bat
run_demo.bat
run_demo.bat --fault corrupt_declaration
run_demo.bat --fault vector_length
run_demo.bat --fault snapshot_conflict
```

三条异常命令均预期退出码为 `2`，分别输出：

- `E_RULE`，路径 `$.backends[0].port`；
- `E_VECTOR`，路径 `$.backends[0].parameter_file`；
- `E_SNAP`，列出 `be-alpha` 指纹变化及整体替换后的统计版本。

每次故障都写入 `reports/fault.json` 和 `logs/gateway.jsonl`。

默认演示按顺序执行：

1. 首次加载并通过网关 TCP 路由 8 条请求，保存 `initial-baseline` 快照。
2. 加载末位参数变化的 v2 声明，识别 `be-alpha` 5 条旧归属失效；在首个 Alpha 批次响应后、提交前显式重载后端，作废旧批次并按新指纹重派；随后重跑请求并保存演进快照。
3. 重新打开 SQLite，先执行两条漂移请求，再恢复演进快照，证明账本不是增量合并；随后从快照请求序列重放并逐项比对。

输出三行稳定排序的场景摘要和一行最终检查摘要。诊断 JSON 位于：

- `artifacts/demo/reports/initial.json`
- `artifacts/demo/reports/evolution.json`
- `artifacts/demo/reports/restore.json`
- `artifacts/demo/reports/summary.json`

## 12. Tkinter 工作台

运行 `run_gui.bat`。界面含五个稳定排序页：

1. 后端 / 指纹：模型、回环端口、并发上限、加载序号、当前指纹、配置摘要。
2. 规则 / 命中分布：优先级、条件、目标后端、批大小、有效与失效命中数。
3. 逐请求账本：绑定指纹、有效标志、绑定复核结果、失效原因、作废标志、输出摘要。
4. 批次 / 作废重派：状态、绑定指纹、请求、重派来源、替代关系、错误码。
5. 统计版本 / 快照：只读标志、受影响后端、重算/未变项、作废/重派批次；快照指纹集合与状态摘要。

界面只读取当前 SQLite 状态并支持手工落快照，不会自行启动推理端口。

## 13. 验证

```bat
.venv\Scripts\python.exe -m pytest
```

本次实际结果：`32 passed`。详细结果和演示指纹、批次、快照摘要见 `docs/validation-report.md`，状态机说明见 `docs/architecture.md`。
