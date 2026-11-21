# 本地 DNS 解析缓存系统与误差诊断 — 技术说明

## 总览

本仓库从零实现了一个本地 DNS 解析缓存系统及其 TTL 误差诊断能力。全部
验证均在本地确定性样本上完成：不访问真实域名、不依赖线上解析器，回源
由 `samples/dns_server.py`（基于 dnslib 的回环可编程测试服务器）提供。

- 运行环境：Python 3.10+（任务书要求 Windows 3.11+，代码同时兼容），
  依赖 `dnslib`、`pytest`，SQLite3 使用 Python 标准库。
- 不使用 Docker 或任何容器环境。

## 目录结构

- `dnscache/` — 系统包
  - `errors.py` — 业务错误码（E1xxx v1 校验、E2xxx v2 校验、E3xxx 版本、
    E4xxx 回源、E5xxx 恢复、E9xxx 通用）
  - `normalize.py` — 域名规范化（小写、去尾点、标签校验）
  - `clock.py` — 可注入逻辑时钟（FixedClock / StepClock 步进表）
  - `ttl.py` — TTL 换算与换算记录（标度、舍入方向、亚毫秒余量、误差上界）
  - `snapshot_v1.py` / `snapshot_v2.py` — 两代快照解析与校验
  - `merge.py` — 跨版本合并、裁决、统一多级索引、定序取最小值
  - `precision.py` — 误差上界累计、精度敏感标记、结论翻转分析
  - `resolver.py` — 多级缓存查询、CNAME 逐跳、回源、否定缓存
  - `store.py` — SQLite 持久化（账本、日志、指针、水位）+ 跨进程文件锁
  - `service.py` — load/replace/resolve/warmup/resume/diagnose/status/verify 编排
  - `cli.py` — 命令行入口（`python3 -m dnscache.cli <子命令>`）
- `samples/` — 确定性样本与测试服务器
  - `make_samples.py` — 样本生成器（重跑可完全复现，含分片哈希）
  - `v1_snapshot.json`、`v2/`、`v2_incr/`、`v2_full/`、`v2_rollback/`
  - `queries.json`、`clock_steps.json`、`responses.json`
  - `dns_server.py` — 回环 UDP 测试服务器（预设应答/TTL/NXDOMAIN/
    NODATA/畸形报文/延迟）
- `tests/` — 44 个 pytest 用例

## 关键设计

### TTL 换算与精度账本（需求 3）

所有时间以整数微秒计算。v1 剩余秒数与上游 TTL 秒数按注入逻辑时钟换算：
`expiry_ms = (now_us + ttl_s * 1_000_000) // 1000`。由于源表示是整数秒，
真实剩余时长落在 `[ttl, ttl+1)` 秒区间，换算记录登记：标度 1000、舍入
方向 down、丢弃的亚毫秒余量（µs）、误差上界 1000ms；v2 绝对毫秒时间戳
以标度 1、exact、上界 0 登记（直通记录保证全链路可追溯）。多级（权威、
递归、本地）同域名条目按固定次序 `authoritative → recursive → local`
折叠取最小值，每次比较记录参与条目、差值、合并误差上界与是否可靠，
绝不使用无序归并。

### 误差上界与精度敏感（需求 4）

当判定（是否过期 / 是否回源 / 取最小值选谁）依赖的剩余时长差
`|diff| ≤ max(容差, 合并误差上界)` 时标记精度敏感，输出误差来源链
（域名、类型、各级换算步骤含余量、比较步骤、裁决依据）与翻转结论：

- `flip_possible_at_current_precision; ms_exact_representation_would_settle`
  — 当前精度下判定不可靠，但毫秒级精确表示可定论（|diff| > 2ms）；
- `flip_possible_even_at_ms_precision` — 即使毫秒级表示也无法定论；
- `decision_firm` — 判定稳固。

### 合并裁决（需求 2）

同一 `(域名, 类型, 级别)` 在两代快照同时存在时，按
`逻辑时钟 → 缓存代数 → 格式优先级(v2>v1>upstream)` 裁决；每条目保留
来源版本、原始时间表示、原始 TTL 值与裁决依据（含全部竞争者）。

### 快照替换（需求 6）

比较清单版本号、缓存代数下限、分片哈希与条目摘要：
版本回退 → `E3001`；同版本同清单 → `NO_OP`；同版本不同清单 →
`E3002`；`min_generation` 超过高水位或分片集合不兼容 → `FULL_REPLACE`
（整体切换快照指针，旧版本条目同事务删除）；否则 `INCREMENTAL`
（未受影响条目仅提升版本号，过期时刻与精度标记逐项不变，仅 upsert
变化条目）。全部在单个 `BEGIN IMMEDIATE` 事务内完成。

### 持久化与恢复（需求 7）

快照指针、迁移决策、换算/比较精度账本、替换审计、逻辑时钟水位均事务
原子写入 SQLite（WAL + synchronous=FULL）。每个操作先写 journal
（pending），步骤级检查点（`journal_steps`）逐步提交；进程被强制终止
后，`resume` 识别 pending 操作并只重放未完成步骤。账本序号在提交事务
内从持久化计数器分配，因此重放后的登记序号与误差上界与未中断运行
逐项一致（测试逐行比对验证）。

### 并发一致性（需求 8）

跨进程文件锁（POSIX `flock` / Windows `msvcrt.locking`）串行化写者，
WAL 允许并发读者；事务内重查 journal 保证同一快照版本只提交一次、
同一替换/解析请求幂等；查询方只在指针切换事务提交后看到完整新版本。

## 实际验证结果

环境：Linux 容器，Python 3.10.12，dnslib 0.17.0，pytest 8.x，
SQLite 3.37.2。

### 单元/集成测试

`python3 -m pytest tests/ -q` → **44 passed**（约 8s），覆盖：

- v1/v2 校验错误码（负 TTL、非整数 TTL、规范化重名、分片哈希不符、
  代数回退、重复条目、非法过期时间）及“非法输入不落盘”；
- 合并裁决、换算账本字段、定序取最小值、精度敏感标记与翻转结论；
- 命中 / 过期回源 / NXDOMAIN / NODATA / 否定缓存命中 / CNAME 逐跳 /
  畸形报文 / 延迟应答 / 幂等解析；
- 替换：回退拒绝、增量保持不变量、整体替换切换指针、幂等 NO_OP、
  同版本冲突、代数回退；
- 恢复：warmup 第 2 步后强杀（exit 3），`resume` 重放后账本 20 行与
  参考运行逐行一致，diagnose 输出一致，verify 通过；
- 并发：6 进程并发替换同一版本 → 恰好 1 次提交、1 条审计、1 个账本
  操作；4 进程并发解析同一名称 → 单一账本操作。

### 样本端到端运行（实际输出）

```
load:    committed version 2 entries 11 ledger 14 findings 4
warmup:  REFETCH gamma.test | HIT alpha.test | MISS_FETCH cdn.test hops=2
         NXDOMAIN nx.test | NODATA nodata.test
         NEGATIVE_HIT_NXDOMAIN delta.test | HIT cdn.test hops=1
diagnose:
  alpha.test min_select -> winner=local
    | flip_possible_at_current_precision; ms_exact_representation_would_settle
  beta.test  min_select -> winner=local
    | flip_possible_at_current_precision; ms_exact_representation_would_settle
  gamma.test expiry -> expired | flip_possible_even_at_ms_precision
  zeta.test  min_select -> winner=authoritative
    | flip_possible_even_at_ms_precision
replace: committed INCREMENTAL changed 1   (v2_incr, 仅 theta.test 新增)
replace: committed FULL_REPLACE changed 1  (v2_full, 指针整体切换到 v4)
rollback: E3001_SNAPSHOT_VERSION_ROLLBACK  (v2_rollback 被拒绝)
verify ok: True
```

精度敏感样本的构造：v1 `alpha.test` 剩余 60s（本地段，换算上界
1000ms）与 v2 `alpha.test` 绝对过期 +60500ms（递归段，上界 0）相差
500ms < 容差 1000ms，取最小值判定在当前精度下不可靠，而毫秒级精确
表示可定论；`gamma.test` 剩余 0s 恰好压在过期边界上，即使毫秒级表示
也无法定论 —— 两类翻转结论均可复现，且恢复后一致。

## 复现步骤

```bash
pip install dnslib pytest
python3 samples/make_samples.py            # 重新生成确定性样本
python3 samples/dns_server.py samples/responses.json &   # 等待 READY
python3 -m dnscache.cli load --state ./state \
    --v1 samples/v1_snapshot.json --v2 samples/v2 --clock-us 1000000000
python3 -m dnscache.cli warmup --state ./state \
    --queries samples/queries.json --clock-table samples/clock_steps.json
python3 -m dnscache.cli diagnose --state ./state
python3 -m dnscache.cli replace --state ./state --v2 samples/v2_incr \
    --clock-us 1000900000
python3 -m dnscache.cli status --state ./state
python3 -m dnscache.cli verify --state ./state
python3 -m pytest tests/ -q
```
