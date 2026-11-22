# AlertLab：本地告警降噪与根因关联组件

AlertLab 是一个无外部依赖的 SRE 值班台演示。它合并 v1/v2 两代告警规则，在可注入逻辑时钟下聚合并抑制告警，沿服务上游依赖计算根因候选，并以 SQLite 事务执行规则补丁、失效闭包和中断恢复。

Web 服务只绑定 `127.0.0.1`，所有样本、数据库和演示结果都保存在本工作区。

## 快速开始

```bash
# Debian/Ubuntu 若缺少 venv 支持，可安装 python3.10-venv，或使用 virtualenv。
python3 -m venv .venv || virtualenv -p python3 .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动可交互值班台，启动前会检查 127.0.0.1:5000 是否空闲
python -m alertlab.cli serve --port 5000
```

浏览器访问 <http://127.0.0.1:5000>。页面上可以载入两代快照与事件流、应用两个样本补丁、清空工作库并执行一致性校验。

常用 CLI：

```bash
python -m alertlab.cli load
python -m alertlab.cli status
python -m alertlab.cli patch samples/patches/dependency_patch.json
python -m alertlab.cli verify
python -m alertlab.cli recover
python -m alertlab.cli reset
python scripts/demo.py
```

工作数据库默认位于 `data/alertlab.sqlite3`，文件锁为 `data/alertlab.sqlite3.lock`。样本 SQLite 位于 `samples/v2/metadata.sqlite3`，两者目录隔离。

## 演示内容

默认样本包含：

- v1：5 条单文件规则、相对生效秒数和扁平 `service_rules` 映射。
- v2：2 个哈希校验分片、10 条元数据记录、绝对毫秒时间、代数 1/2、依赖、静默标签和 2 个墓碑。
- 拓扑：5 个服务、8 个实例、两条上游链路和故障域。
- 事件流：11 条告警，含乱序时间戳、1 条迟到事件、重复告警、静默标签和跨规则关联信号。

载入后得到 6 个确定性事件簇、17 个根因候选，迟到事件 `seq=9` 被保留在审计区且不修改已关闭的 Gateway 簇。

依赖补丁把 `GatewayLatency` 的规则级上游从 `order-api + auth-api` 改为仅 `auth-api`：

- Gateway 簇的聚合摘要逐字节复用。
- Gateway 根因候选闭包从 7 个收缩为 3 个，并产生带路径的失效清单。
- 其余 5 个簇及其 10 个候选逐字段不变。
- 相同补丁重复提交返回 `idempotent: true`，不产生新版本。

## HTTP 接口

所有接口返回稳定排序的 JSON，错误使用统一结构：

```json
{
  "ok": false,
  "error": {
    "code": "E_V1_SEVERITY",
    "message": "severity is not a legal enum value",
    "details": {}
  }
}
```

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 交互式值班台 |
| `POST` | `/load` | 载入拓扑、v1/v2 快照和事件流 |
| `GET/POST` | `/patch` | 应用规则补丁或 `{"patch_file": "..."}` |
| `GET/POST` | `/status` | 当前规则版本、代表事件、根因排序、迟到审计 |
| `GET/POST` | `/verify` | 哈希、排序、水位和纯函数重放校验 |
| `POST` | `/reset` | 清空工作数据库，仅用于本地演示 |
| `GET` | `/health` | 本地绑定与工作区信息 |

`/load` 可显式设置 `logical_clock_ms`，或传入 `include_v1` / `include_v2` 选择快照来源：

```bash
curl -s http://127.0.0.1:5000/load \
  -H 'Content-Type: application/json' \
  -d '{"logical_clock_ms":1700001800000,"include_v1":true,"include_v2":true}'
```

## 关键语义

- 裁决键固定为 `(快照逻辑时钟, 规则代数, 来源格式优先级, 来源类型, 来源引用)`，选择最大者。
- v1 相对秒数按 `注入时钟 + 秒 × 1000` 换算，保留原始值、标度、舍入方向和注入时钟。
- v2 绝对毫秒原值保留；墓碑进入审计表但不进入生效视图。
- 聚合窗口只读取事件中的注入逻辑时钟毫秒值和流内水位，不读取系统墙钟。
- 代表事件排序：非静默、事件严重度、规则严重度、实例权重、时间、序号、事件 UID。
- 根因排序：有告警信号、时间差、实例权重、依赖距离、服务名、实例名。
- v2 规则级上游列表覆盖该规则直接上游，未提供时使用拓扑；更深层继续沿拓扑闭包。
- 补丁只允许 `upstream_dependencies`、`silence_labels`、`effective_at_ms`，非法或未变化内容不推进版本指针。
- 失效闭包会继续传播到“其他簇的候选证据”发生变化的情况；簇摘要可复用时候选单独重算。
- 所有结论使用规范 JSON 与 SHA-256 摘要；未受影响簇和候选直接复用原文本字段。

## 测试与恢复

```bash
source .venv/bin/activate
pytest -q
```

测试覆盖规则错误码、无副作用、哈希/代数/类型校验、合并裁决、迁移幂等、版本回退、确定性聚合、迟到隔离、根因排序、补丁复用与失效闭包、Web 接口、簇提交强杀恢复、补丁提交强杀恢复和多进程并发幂等。

强杀注入示例：

```bash
python -m alertlab.cli load --crash-at cluster_after_journal:2
python -m alertlab.cli patch samples/patches/dependency_patch.json \
  --crash-at patch_before_commit
```

`os._exit(73)` 后重新构造 `AlertLabService` 或再次执行 CLI，会自动识别未完成 ingest/patch，只补提交缺失簇或重放未激活补丁；已提交簇不会被覆盖，抑制计数不会累加。

详细设计与数据结构见 [docs/DESIGN.md](docs/DESIGN.md)，实际验证记录见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

## 项目结构

```text
alertlab/
  rules.py      两代规则解析、校验、合并、迁移
  topology.py   拓扑、故障域、实例权重、上游闭包
  events.py     事件流校验与规范化
  engine.py     聚合、代表选择、根因排序
  store.py      SQLite、文件锁、版本指针、恢复日志
  service.py    编排、补丁失效闭包、状态与校验
  web.py        Flask 端点
  cli.py        load/patch/status/verify/recover/serve
samples/        两代快照、拓扑、事件流、补丁
scripts/        样本构建与可重复演示
tests/          pytest 验收测试
```
