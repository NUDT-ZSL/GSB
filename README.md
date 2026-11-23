# Local Quota Ledger

本地多租户资源配额账本与跨服务预留仲裁引擎。应用从零实现，只依赖 Python 3.11、Flask、pytest 与标准库 `sqlite3`；不需要 Docker、外部服务或 GPU。

核心保证：

- 所有业务时间都是外部注入的整数毫秒逻辑时钟，核心代码不读取系统墙钟。
- 准入扣减、预留写入、释放归还、等待队列追平都在 SQLite 事务中完成。
- 租户基础配额和借调量分账；释放时先归还借调，再恢复基础余量。
- SQLite WAL + 跨进程文件锁 + `BEGIN IMMEDIATE` 提供多进程串行化边界。
- PREPARED 操作日志支持强制终止后的幂等恢复，不重复扣减或重复归还。
- 租户配额声明按版本整体替换，在途预留保留准入时的版本绑定。
- 定点余量、耗用率、舍入方向、误差上界与精度敏感来源链可追溯。

## 快速开始

### Linux / macOS

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

无需外部数据库。SQLite 文件、WAL 与锁文件会自动创建。

## 一键演示

### 确定性账本演示

```bash
python scripts/demo.py
```

该脚本会重建 `data/demo/ledger.db`，依次执行：

1. 装载正常租户、服务与 16 条申请轨迹。
2. 以 `100ms` 轮次预算提交全部暂存申请。
3. 在逻辑时刻 `200ms` 与 `500ms` 恢复并处理到期预留。
4. 执行声明版本 `v2` 整体替换。
5. 尝试回退到 `v1`，验证返回 `VERSION_ROLLBACK`。
6. 运行 `status` 与 `verify`，输出 `artifacts/demo_report.json`。

### 多进程幂等演示

```bash
python scripts/multiprocess_demo.py
```

该脚本并发启动 9 个提交进程（其中 6 个重复提交同一申请），再并发启动 5 个相同释放进程，最后打印 `verify` 结果并写入 `artifacts/multiprocess_report.json`。

### Web 控制台

```bash
python -m quota_ledger --db data/demo/ledger.db serve --port 5000
```

浏览器打开 `http://127.0.0.1:5000`。服务固定监听回环地址 `127.0.0.1`。控制台支持：

- 查看租户基础配额、借调余额、资源池可借量、等待队列、精度链。
- 单笔提交、单笔释放、推进逻辑时钟并追平。
- 一键运行完整样本、重置样本、执行守恒校验。

若数据库为空，控制台启动时会自动装载 `samples/` 正常样本。需要从确定状态开始时点击“重置样本”。

## 命令行

全局参数 `--db` 位于子命令之前。

### 装载与重建

```bash
python -m quota_ledger --db data/demo/ledger.db load \
  --tenants samples/tenants.json \
  --services samples/services.json \
  --applications samples/applications.json \
  --reset
```

`--reset` 在事务内删除并重建全部表，确保演示从确定状态开始。非法输入在触库前即被拒绝；版本冲突在事务内回滚，不留下部分状态。

### 提交

提交全部暂存申请，使用确定性单线程演示：

```bash
python -m quota_ledger --db data/demo/ledger.db submit --all \
  --round-id demo-round-1 \
  --round-start-ms 0 \
  --round-budget-ms 100 \
  --workers 1
```

提交指定申请，可用多个 `--application` 或 `--workers` 并行：

```bash
python -m quota_ledger --db data/demo/ledger.db submit \
  --application app-0001 --application app-0002 --workers 2
```

提交参数全部是逻辑时间或确定性标识。`--round-budget-ms` 定义为：申请 `requested_at_ms` 落在 `[round_start_ms, round_start_ms + round_budget_ms]` 内才可启动；超出者标记 `ROUND_BUDGET_EXHAUSTED`。单服务每轮启动数还受 `requests_per_round` 限制，超出者标记 `SERVICE_ROUND_BATCH_LIMIT`。

### 释放

```bash
python -m quota_ledger --db data/demo/ledger.db release \
  --application app-0002 --now-ms 500 --trigger MANUAL
```

`--trigger` 支持 `MANUAL`、`CANCEL`、`EXPIRY`。同一预留的重复释放返回既有事件，不重复记账。

### 恢复、状态与校验

```bash
python -m quota_ledger --db data/demo/ledger.db resume --now-ms 500
python -m quota_ledger --db data/demo/ledger.db status
python -m quota_ledger --db data/demo/ledger.db verify
```

`resume` 先重放所有 PREPARED 准入/释放操作，再释放 `expires_at_ms <= now_ms` 的预留。`status` 以按键排序、列表稳定排序的 JSON 输出。`verify` 在失败时返回非零退出码，并列出独立问题码。

## 样本格式

### 租户文件

```json
{
  "fixed_scale": 1000,
  "precision_tolerance": 0.000001,
  "resource_pools": [
    {"name": "pool-core", "millicores": 2200, "mib": 4096}
  ],
  "tenants": [
    {
      "name": "team-red",
      "resource_pool": "pool-core",
      "quota": {"millicores": 800, "mib": 1024},
      "allow_overdraft": true,
      "borrow_limit": {"millicores": 400, "mib": 512},
      "declaration_version": 1
    }
  ]
}
```

- 租户与资源池名称唯一。
- 配额、借调上限为非负整数；`allow_overdraft` 为严格布尔值。
- 资源池容量固定；基础配额合计不得超过池容量。
- 任意时刻“基础配额合计 + 资源池未归还借调”不得超过池容量。

### 服务文件

```json
{
  "services": [
    {
      "name": "svc-red-a",
      "resource_pool": "pool-core",
      "requests_per_round": 3,
      "max_concurrent_reservations": 3
    }
  ]
}
```

申请所关联的租户和服务必须属于同一资源池。

### 申请轨迹

```json
{
  "applications": [
    {
      "sequence": 1,
      "application_id": "app-0001",
      "tenant": "team-red",
      "service": "svc-red-a",
      "millicores": 600,
      "mib": 400,
      "requested_at_ms": 0,
      "duration_ms": 200,
      "expires_at_ms": 200
    }
  ]
}
```

- `sequence` 在文件内严格递增，新增申请序号必须大于账本现有最大序号。
- `application_id` 唯一；省略时生成 `app-XXXXXX`。
- `requested_at_ms`、`expires_at_ms` 非负数；`expires_at_ms` 不得早于申请时刻。
- 毫核与 MiB 必须为正整数。
- 若同时给出 `duration_ms` 与 `expires_at_ms`，必须满足 `expires = requested + duration`。

异常样本位于 `samples/invalid/`：

- `tenants_duplicate.json`：重复租户。
- `tenants_negative_borrow.json`：负借调上限。
- `tenants_rollback.json`：版本回退。
- `applications_bad.json`：到期早于申请、重复序号、错误类型。

## 账本与仲裁语义

### 配额拆分

每个活动预留保存四项资源：

- `base_mc/base_mib`：从租户基础配额扣除的份额。
- `borrow_mc/borrow_mib`：从资源池可借余量借入的份额。

因此始终有：

```text
base_used <= quota
borrowed <= borrow_limit
reservation.total = reservation.base + reservation.borrow
tenant.base_used = sum(active reservation.base)
tenant.borrowed = sum(active reservation.borrow)
pool.borrowed = sum(active reservation.borrow in pool)
```

基础余量不会因借调变为负数。

### 借调

当基础余量不足且租户允许借调时：

```text
deficit = max(0, request - free_base)
借调还需满足 deficit <= 租户借调余额
            且 deficit <= 资源池可借余量
```

`borrow_obligations` 记录借调量、资源池、借出时资源池可借余量快照、借调逻辑时刻与到期归还期限。

释放事务的分录顺序固定为：

1. `BORROW_RETURN`：归还租户和资源池的借调余额。
2. `QUOTA_RELEASE`：恢复租户基础已用量。

若借调义务与预留借调量不相等，或租户/资源池借调账本不足，释放返回独立错误码 `RELEASE_BORROW_MISMATCH`，事务回滚。

### 等待与追平

配额、借调或服务并发不足时，申请进入 `wait_queue`，状态为 `WAITING`。队列按：

```text
requested_at_ms, sequence
```

稳定排序。释放事务提交前，在同一事务中依次尝试唤醒队列头部；一旦某个候选仍无法准入即停止，并记录：

- 触发来源释放申请 ID。
- 每个候选的排名、结果、原因码。
- 最终仍未准入的队列快照。

若多个候选都可准入，会连续唤醒多个；释放产生的每个可准入机会不会被跳过。

### 版本

每个租户声明以内容摘要版本化：

- 新租户：`INCREMENTAL_APPEND`。
- 既有租户字段变化且版本更高：`FULL_REPLACE`。
- 内容不变：`NO_OP`。
- 版本更低：`VERSION_ROLLBACK`。
- 同版本不同摘要：`VERSION_CONFLICT`。

当前版本指针和完整版本快照在同一事务内更新。活动预留记录准入时的 `declaration_version` 与摘要；版本替换不重算在途预留。

### 定点精度

账本金额本身始终是精确整数。外部定点视图使用声明 `fixed_scale`：

- 基础余量、借调余额、请求量使用精确标度换算，舍入方向为 `EXACT`。
- 耗用率使用 `DOWN` 舍入，记录原始分子/分母、标度、转换值与误差上界。
- 每次裁决产生的误差上界累计到租户 `cumulative_error_micro`。
- 当有效可给余量与请求量之差小于 `precision_tolerance`，且该差值决定准入/阻塞时，标记租户和申请为精度敏感。
- `precision_events.chain_json` 包含参与租户、申请序号、版本、请求、有效余量、换算步骤与步骤 ID。

### 崩溃恢复

提交与释放采用两阶段工作流：

1. 独立事务写入 `operations.state = PREPARED`。
2. 另一事务执行扣减/释放/追平，并把操作标记为 `DONE`。

若进程在阶段 2 中途强制终止，SQLite 回滚未提交事务，PREPARED 操作保留。`resume` 读取 PREPARED 操作：

- 若准入已完整提交，只标记完成。
- 若未提交，重新计算并执行一次。
- 释放通过预留状态、释放事件状态和借调义务识别半提交，避免重复归还。

每次阶段 2 提交还会在同一事务内写入账本快照摘要：逻辑时钟、提交水位、租户账本、预留、队列、借调义务与全部分录。

测试提供两个强制崩溃点：

```text
QUOTA_LEDGER_CRASH_POINT=during_admit_apply
QUOTA_LEDGER_CRASH_POINT=during_release_apply
```

也可用 `after_admit_prepare` / `after_release_prepare` 验证“已持久化意图但尚未执行”的恢复路径。

## 并发边界

每个变更操作：

1. 获取同一数据库对应的 OS 文件锁。
2. 获取进程内同名 `RLock`。
3. 使用独立 SQLite 连接与 `BEGIN IMMEDIATE`。

由此得到：

- 同申请重复提交只有一个事务能完成准入。
- 租户配额与借调账本不会被并发超扣。
- 同一释放只有一个事务能完成归还。
- 等待队列不会被两个释放并发重复唤醒。
- `application_id`、`sequence`、操作 ID、释放 ID 与数据库唯一约束提供第二层幂等保护。

SQLite 使用 WAL、`synchronous=FULL`、30 秒 busy timeout 和外键检查。

## 校验项

`verify` 会检查：

- 租户基础/借调账本等于活动预留之和。
- 基础余量与借调余量非负。
- 活动预留均有配额扣减分录，同一申请不重复扣减。
- 分录净额等于租户账本。
- 资源池借调等于池内活动预留借调之和，容量不超卖。
- 开放借调义务与活动预留借调量一致，借出时池余量足够。
- 释放事务先借调归还、后配额恢复。
- 申请状态、预留状态与等待队列互相一致。
- 服务并发上限、单轮服务批次未被突破。
- 活动预留的声明版本绑定存在。
- 无残留 PREPARED 操作。
- 当前账本摘要与最新持久化快照逐位一致。

## 测试

```bash
pytest -q
```

测试覆盖：

- 非法字段、重复租户、负借调、资源池闭合、序号顺序、非法申请时间。
- 非法装载不写入任何状态；版本回退不改变现有账本。
- 正常样本并行准入、借调、等待、到期释放与追平。
- 多候选连续唤醒、阻塞停止与最终队列。
- 借调归还顺序、借调不平拒绝释放。
- 定点误差链与精度敏感标记。
- 版本整体替换、在途预留版本绑定、回退拒绝。
- 准入事务崩溃与释放事务崩溃后的幂等恢复对比。
- 多进程并发提交、重复提交、重复释放与守恒校验。
- Flask 控制台、状态 API、完整演示 API。

## 文件结构

```text
quota_ledger/
  cli.py             # load/submit/release/resume/status/verify/serve
  database.py        # SQLite 模式、WAL、跨进程文件锁、事务
  engine.py          # 校验后装载、准入、借调、释放追平、恢复、校验
  errors.py          # 业务错误码
  fixedpoint.py      # 定点换算与误差上界
  models.py          # 严格 JSON 解析、模型与跨引用校验
  web.py             # Flask 工厂与 API
  templates/ static/ # 本地交互控制台
samples/
  tenants.json services.json applications.json
  tenants_v2.json
  invalid/
scripts/
  demo.py
  multiprocess_demo.py
tests/
```

## 实际验证结果

本工作区已执行：

```text
pytest -q
14 passed

python scripts/demo.py
ok=true, logical_clock_ms=500,
submit: 11 ADMITTED / 4 WAITING / 1 ROUND_BUDGET_EXHAUSTED,
invalid inputs: TENANT_DUPLICATE / INVALID_FIELD_VALUE /
               APPLICATION_EXPIRY_BEFORE_REQUEST / VERSION_ROLLBACK,
verify: 16 checks, 0 issues

python scripts/multiprocess_demo.py
9 个提交进程 + 5 个重复释放进程,
verify: 16 checks, 0 issues
```

详细报告：

- `artifacts/VERIFICATION.md`
- `artifacts/demo_report.json`
- `artifacts/multiprocess_report.json`
- `artifacts/pytest_output.txt`

更细的事务状态机与不变量说明见 `docs/DESIGN.md`。
