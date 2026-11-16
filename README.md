# 内部审计档案增量采集工作台

一个不使用 Docker、只访问本机确定性样本的增量采集工作台。FastAPI 同时提供：

- 工作台页面与 JSON API；
- 同进程内的本地授权档案站点；
- 异步采集调度、会话重放、退避重试与 SQLite 检查点；
- 列表渲染就绪判定、schemaVersion 解析、字段级增量与版本关联图；
- 跨并发/延迟配置对账脚本，以及真实 SIGKILL 后恢复演示脚本。

默认数据库为 `data/workbench.sqlite3`，默认站点/工作台地址为 `http://127.0.0.1:8000`。

## 1. 环境准备

要求 Python 3.10+。常规环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

若当前系统缺少 `ensurepip`，可先安装用户级 `virtualenv`，再创建环境：

```bash
python3 -m pip install --user virtualenv
python3 -m virtualenv .venv
.venv/bin/pip install -r requirements.txt
```

本项目实际使用 Python 3.11、FastAPI 0.163.1、httpx 0.28.1、BeautifulSoup 4.15、lxml 6.1.3 验证。

## 2. 手动启动

```bash
.venv/bin/python -m app.main
# 或
make run
```

可配置环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `PUBLIC_BASE_URL` | `http://HOST:PORT` | 采集器访问同进程站点的地址 |
| `AUDIT_DB_PATH` | `data/workbench.sqlite3` | SQLite 文件 |
| `AUDIT_MAX_CONCURRENCY` | `8` | 并发度硬上限 |
| `LOG_LEVEL` | `info` | uvicorn 日志级别 |

示例：

```bash
PORT=8787 AUDIT_DB_PATH=/tmp/audit-demo.sqlite3 LOG_LEVEL=warning \
  .venv/bin/python -m app.main
```

打开 `http://127.0.0.1:8787/`。建议按顺序创建：

1. `baseline`：6 条基线记录，包含 schema `1.0`、`2.0`、未知 `9.9`、未声明版本；
2. `incremental`：4 条增量记录，为已有业务生成修订 2，并增加一个新业务；
3. 在任务详情页查看“差异与冲突”和“版本关联图”。

站点不是外网资源，数据在 `app/archive_site.py` 中确定性地生成。

## 3. 页面与 API

页面：

- `/`：创建任务、查看任务列表和实时状态；
- `/tasks/{task_id}`：进度、检查点、会话、渲染观测、差异、冲突、版本关联图、恢复按钮。

主要 API：

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/tasks` | 创建并启动任务 |
| `GET` | `/api/tasks` | 任务列表 |
| `GET` | `/api/tasks/{id}` | 任务、进度、检查点、会话、事件 |
| `GET` | `/api/tasks/{id}/differences` | 新记录、增量边、冲突、条目失败 |
| `GET` | `/api/tasks/{id}/graph` | 版本关联图节点与边 |
| `GET` | `/api/tasks/{id}/snapshots` | 渲染就绪观测快照 |
| `POST` | `/api/tasks/{id}/resume` | 从库中检查点恢复 |
| `GET` | `/api/business/{business_id}` | 业务标识的完整版本链及每次采集原始响应 |
| `GET` | `/api/raw/{raw_id}` | 原始响应正文与哈希、请求序号 |
| `GET` | `/api/consistency` | 当前库的全局规范化摘要 |

任务配置示例：

```json
{
  "name": "incremental",
  "wave": "incremental",
  "concurrency": 6,
  "delay_profile": "bursty",
  "fault_profile": "mixed",
  "max_retries": 3,
  "backoff_base_ms": 80,
  "backoff_max_ms": 800,
  "high_latency_ms": 500,
  "max_session_replays": 3,
  "render_timeout_s": 4.0,
  "max_render_observations": 40,
  "request_timeout_s": 8.0
}
```

`concurrency` 超过硬上限会返回 `E_CONCURRENCY_LIMIT`；其他配置错误返回 `E_CONFIG`。

## 4. 确定性样本与异常注入

站点支持三个波次：

- `baseline`：3 页、6 条记录；
- `incremental`：2 页、4 条增量记录；
- `conflict`：同一业务标识、schemaVersion、修订号的两份不同字段内容。

延迟配置：

- `none`：无交互延迟；
- `slow`：响应间隔随请求序号在 12–66 ms 波动；
- `bursty`：16/72/5/44/25 ms 循环；
- `spike`：每第 5 个请求 380 ms，其余 5 ms。

异常注入：

| `fault_profile` | 行为 |
|---|---|
| `none` | 无异常 |
| `mixed` | 首次访问各详情/列表片段注入 503；会话第 6 次请求过期 |
| `session_expiry` | 每个会话第 6 次请求返回 `E_SESSION_EXPIRED` |
| `always_503` | 所有详情持续 503，用于验证重试耗尽 |
| `render_timeout` | 二次渲染始终不产生条目容器 |
| `bad_cursor` | 第 1 页声明错误的下一页标识 |

## 5. 关键实现

### 列表页二次渲染与就绪判定

列表 HTML 本身只有渲染契约和挂载点；内联脚本从 `list-fragment` 获取条目容器并注入。采集器执行同一份契约：

1. 读取 `#render-contract` 的 fragment API；
2. 反复获取 fragment；
3. 只有 `#entry-container` 存在，且条目计数连续两次观测相同时才接收；
4. 每次观测写入 `render_snapshots`，包含容器是否存在、条目数、连续稳定次数、正文哈希和片段；
5. 超时返回 `E_RENDER_TIMEOUT`，任务状态为 `recoverable`，快照可复核。

没有用固定 sleep 代替就绪判定。分页推进同时校验 `data-total-pages`、`data-page` 与 `data-next-page`；下一页必须等于当前页 + 1，最后一页不得有 next 标识，否则返回 `E_CURSOR_INVALID`。

### schemaVersion 解析

- `1.0`：语义化 HTML，读取 `meta[name=schemaVersion]` 与 `[data-field]`；
- `2.0`：内嵌 JSON `script#audit-detail`，读取 `record.attributes`；
- 未知版本：保留 HTML 中全部可识别字段，标记 `unresolved=true`；
- 未声明版本：唯一键 schema 使用 `__missing__`，同样保留字段并标记 unresolved；
- 解析结果保存 `schema_evidence_json`，包括声明值、声明来源、选用解析器、修订号来源、HTML 哈希与稳定的源路径。

字段值统一规范为字符串/确定性 JSON，便于跨结构比较。

### 幂等键、冲突与版本链

详情唯一键为：

```text
(business_id, schema_version, revision)
```

- 相同键、相同字段哈希：仅记录本次原始响应和 observation，结果为 `duplicate`，不新增规范化记录或边；
- 相同键、不同字段哈希：写入 `conflicts`，本次条目返回 `E_CONFLICT`；规范化记录按字段哈希的确定性最小值选取，不按请求到达顺序覆盖；
- 每次采集（插入、重复、冲突）都写入 `record_observations`，保留到原始响应的追溯关系。

关联链在每次成功写入后按确定性排序重建：

```text
(revision, schema_version, field_hash)
```

对相邻记录计算：

- `added_fields = 当前字段 - 上一修订字段`；
- `missing_fields = 上一修订字段 - 当前字段`。

因此最终记录集合、增量字段和边集合不依赖并发完成顺序。

### 并发、退避与会话重放

- 请求信号量严格限制在途 HTTP 请求数；
- 503、网络错误与超过高延迟阀值的成功响应使用有界指数退避；
- 退避等待发生在释放请求信号量之后，不会扩大在途请求数；
- 重试耗尽形成条目级 `E_RETRY_EXHAUSTED`，任务仍为 `partial`，`task_failure_count=0`；
- 401 或响应体中的 `E_SESSION_EXPIRED` 触发新会话并重放当前请求；
- 重放超过上限形成 `E_SESSION`，任务保持可恢复；
- 过期响应只记录请求事件中的状态码与正文哈希，不写入原始响应表，也不会进入规范化记录。

### 检查点与恢复

`checkpoints` 保存：

- 当前分页游标及是否完成枚举；
- 实际已完成条目数；
- 已完成会话标识列表。

逐条目的原始响应、规范化/冲突处理、完成键集合和检查点计数在同一 SQLite 事务中提交。进程被 SIGKILL 后，任务行通常保持 `running`，但没有退出报告；重启后执行：

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/<task_id>/resume
```

恢复只依赖同一数据库中的任务配置、分页游标、已发现条目、已完成键和会话记录。旧进程的内存会话丢失后会重新获取会话并重放请求。

## 6. 自动验证

### 测试

```bash
.venv/bin/pytest
# 或 make test
```

实际结果：

```text
6 passed
```

覆盖：两次稳定渲染、分页观测、v1/v2/未知/缺失 schema、字段增量、原始响应追溯、冲突与幂等、503 重试耗尽、条目级/任务级失败区分、高延迟退避、会话重放上限、过期正文隔离、坏游标和渲染超时。

### 跨配置一致性对账

```bash
.venv/bin/python scripts/validate_consistency.py
# 或 make reconcile
```

脚本分别启动两个独立数据库/进程：

- A：并发 1，`slow` 延迟，`mixed` 异常；
- B：并发 6，`bursty` 延迟，`mixed` 异常；
- 每个运行依次执行 baseline 与 incremental。

输出写入：

- `artifacts/consistency/reconciliation.json`
- `artifacts/consistency/reconciliation.md`
- `artifacts/consistency/run-a.sqlite3`
- `artifacts/consistency/run-b.sqlite3`

实际结果：

```text
A: 7 businesses, 10 records, 3 edges
B: 7 businesses, 10 records, 3 edges
逐业务标识：全部一致
结论：PASS
```

对账比较规范化字段与字段哈希、schemaVersion、修订号、unresolved 标记、每条边的 `added_fields` 与 `missing_fields`。运行局部 ID、时间戳、URL 和 raw_response_id 不参与等价比较。

### 强制终止与恢复

```bash
.venv/bin/python scripts/recovery_demo.py
# 或 make recovery
```

脚本会：

1. 在独立数据库执行一次无中断对照运行；
2. 启动同类任务，在完成至少 1 条、但尚未完成 6 条时对 uvicorn 进程发送 SIGKILL；
3. 确认库中任务仍为 `running`，原始进程没有退出报告；
4. 用同一端口和同一 SQLite 重启服务；
5. 调用 resume，仅依据库中检查点继续；
6. 再执行 incremental，并与对照运行逐业务对账。

实际结果：

```text
终止前：1 / 6 条，任务 running，无退出报告
重启后：任务仍为 running，resume 成功
恢复后：6 / 6 条
最终：7 业务、10 记录、3 边，与对照运行逐项一致
结论：PASS
```

报告文件：

- `artifacts/recovery/report.json`
- `artifacts/recovery/report.md`
- `artifacts/recovery/crash.sqlite3`
- `artifacts/recovery/reference.sqlite3`

## 7. 业务码与任务状态

| 业务码 | 层级 | 说明 |
|---|---|---|
| `E_RENDER_TIMEOUT` | 任务可恢复 | 二次渲染未连续两次稳定 |
| `E_CURSOR_INVALID` | 任务可恢复 | 总页数/页码/next 标识冲突 |
| `E_RETRY_EXHAUSTED` | 条目级 | 503/高延迟/网络重试耗尽 |
| `E_SESSION` | 条目或任务可恢复 | 会话重放次数超限 |
| `E_CONFLICT` | 条目级 | 同唯一键字段内容不一致 |
| `E_PARSE` | 条目级 | 详情无法形成可追溯记录 |
| `E_TASK` | 任务级 | 未预期编排异常 |

任务状态：

- `completed`：所有发现条目均完成（重复修订也算完成）；
- `partial`：有条目级失败，任务本身仍完成调度；
- `recoverable`：渲染/游标/会话等任务阶段需要恢复；
- `failed`：未预期任务级异常。

## 8. 代码结构

```text
app/
  archive_site.py   确定性站点、动态渲染契约、延迟/异常注入
  crawler.py        并发调度、退避、会话重放、渲染观测
  render.py         渲染契约、条目解析、游标校验
  parsers.py        v1/v2/未知/缺失 schema 解析与版本证据
  db.py             SQLite schema、事务、幂等/冲突/边重建/检查点
  reconciliation.py 跨运行摘要规范化与逐业务差异
  service.py        任务生命周期与恢复
  api.py            工作台 JSON API
  main.py           FastAPI 应用与手动启动入口
  static/           任务列表、详情、差异和关系图页面
scripts/
  validate_consistency.py
  recovery_demo.py
tests/
  test_workbench.py
```

## 9. 设计取舍

- 为保证离线、确定性和可复核，没有引入浏览器或 Docker。列表页的实际前端脚本与采集器都遵守同一个显式渲染契约：“fragment 请求 → 容器出现 → 条目数连续两次稳定”。采集器直接执行该契约，并保存每次观测快照。
- 规范化记录只保留本次响应实际提供的字段，不把上一修订的字段补齐；这样字段消失可以明确成为 `missing_fields`。
- 关联边不是“抓到时立即连边”，而是每次写入后按确定性顺序重建，消除了延迟和并发完成顺序对链结构的影响。
