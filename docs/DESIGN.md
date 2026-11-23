# 本地 TLS 证书链校验与信任时刻精度诊断引擎 — 技术说明

## 1. 概述

引擎完全在本地运行，不访问网络、不读取系统墙钟。所有“当前时刻”均来自注入的
确定性逻辑时钟（毫秒整数），证书有效期、信任生效时刻、吊销证据有效性等一切时间
比较都基于该时钟完成。核心能力：

- 解析并严格校验 v1（单文件 JSON）与 v2（分片 JSON + SQLite 元数据库）两代信任库；
- 按逻辑时钟、信任代数与来源优先级跨版本合并，形成统一信任视图；
- 基于 `cryptography` 自下而上构建叶子到根的证书链并逐步校验；
- 区分 `revoked / good / unknown` 三种吊销状态，并与信任代数联动失效重算；
- 对每次相对秒→绝对毫秒换算同时走“受控定点通路”和“decimal 高精度真值通路”，
  记录双通路值、绝对/相对偏差、误差上界与误差来源链；
- 在有效期/回源边界上标记精度敏感比较，并给出更高标度下是否翻转结论；
- 版本化 SQLite 原子持久化、强制终止恢复、多进程并发一致性与幂等 CLI。

## 2. 目录结构

```
tlsdiag/
  errors.py       稳定业务错误码（E1001..E5001）与 DiagError
  clock.py        确定性逻辑时钟 + 精度配置
  precision.py    双通路换算账本、误差累计、精度敏感比较
  certgen.py      确定性证书生成原语（RSA/EC、CA/叶子、通配符 SAN）
  samples.py      一次性生成完整本地样本（信任库/CRL/OCSP/时钟/精度配置）
  truststore.py   v1/v2 解析与严格校验（无持久化副作用）
  merge.py        跨版本裁决与统一信任视图
  chain.py        链构建、签名/约束/用途/有效期/主机名逐步校验
  revocation.py   CRL/OCSP 证据评估与代数联动键
  storage.py      SQLite 版本化持久化、文件锁、原子提交、崩溃恢复
  engine.py       顶层编排（apply/verify/resume/status/audit）
  cli.py          load/chain/verify/apply/resume/status/verify-audit 子命令
tests/            pytest 套件（35 个用例）
docs/DESIGN.md    本文档
```

## 3. 需求到实现的映射

### 3.1 信任库解析与校验（业务要求 1）

- v1：别名唯一（`E1001`）、指纹与 PEM 实际 SHA-256 摘要一致（`E1002`）、
  相对信任秒为非负整数（`E1003`）、必填字段（`E1004`）、PEM 可解析（`E1005`）、
  启用标记为布尔（`E1006`）。
- v2：分片 SHA-256 哈希（`E2001`）、分片缺失（`E2002`）、同别名信任代数严格单调
  （`E2003`）、生效毫秒为整数（`E2004`）、墓碑结构合法（`E2005`）、吊销标记合法
  （`E2006`）、SQLite 元数据可读且与分片一致（`E2007/E2008`）。
- 加载阶段只读取样本并返回内存对象，非法输入在任何 SQLite 写入前抛出，保证
  “非法条目不写入任何持久状态”（见 `test_invalid_snapshot_does_not_touch_storage`）。

### 3.2 跨版本合并（业务要求 2）

同一别名同时存在时，裁决顺序为：

1. 逻辑时钟 tick 较大者胜；
2. tick 相同则信任代数较大者胜（v1 视为代数 1）；
3. 仍相同则来源格式优先级 `v2 > v1`。

每个信任锚保留 `source_version`、`original_time_representation`、`decision_basis`、
标度与舍入方向。v1 相对秒在 `clock.instant_ms(tick)` 基线上换算为绝对毫秒，并记录
换算标度（含定点 Qm）与舍入方向；v2 整数毫秒走零误差透传。

### 3.3 链构建与校验（业务要求 3）

`chain.build_chain` 按签发者/主体名自下而上匹配，直到自签名且受信的根锚点；
`validate_chain` 逐步记录签名、基本约束/路径长度、密钥用途/EKU、有效期、主机名、
信任锚节点（`TraceNode`）。主机名支持 RFC6125 风格单左标签通配符。有效期边界通过
整数毫秒换算（`_dt_to_ms`，无浮点），只与注入时钟比较。

### 3.4 吊销与代数联动（业务要求 4）

`RevocationEvidence` 在当前逻辑时刻窗口内读取 CRL/OCSP：

- 命中 CRL 序列号或信任锚吊销标记 → `revoked`（链结论置 `E4007`）；
- OCSP 明确 `good` → `good`；
- 无任何可用证据 → `unknown`，与已吊销严格区分。

吊销结论主键为 `(fingerprint, generation, tombstone_signature)`；验证请求身份也包含
当前信任代数，因此代数提升或新增墓碑会产生新键、强制重算而非复用旧结论。

### 3.5 双通路精度账本（业务要求 5）

`PrecisionLedger.convert_relative_seconds`：

- 定点通路：先把标度因子量化到 Qm，再量化乘积，舍入模式可配；
- 真值通路：`Fraction` 精确有理计算并以 60 位 `Decimal` 呈现；
- 记录两通路绝对毫秒、绝对偏差、相对偏差、单步误差上界与累计误差上界，
  误差来源链含别名、快照版本、换算步骤与舍入方向。

默认样本使用 1000/1 的精确标度（偏差为 0，可复算）；`tests/test_precision.py`
使用 1003/1000 的非整除标度验证非零偏差、误差上界约束与累计。

### 3.6 精度敏感标记与翻转追溯（业务要求 6）

`compare_remaining` 对参与证书的剩余时长做 expiry / reverify 两类比较：当固定通路
差值落入容差，且单证书贴近阈值或两通路结论分歧（多证书比较看相互差值）时标记
`precision_sensitive`，输出参与证书、各自偏差、误差上界，并以更高定点标度重算判定
`flips_at_higher_scale`。比较 ID 由规范化输入哈希得到，保证可重算且稳定。

### 3.7 版本化持久化与恢复（业务要求 7）

SQLite 表：`meta / snapshots / anchors / jobs / chain_conclusions /
revocation_conclusions / precision_ledger / precision_sensitive`。快照提交使用
临时表 `stage_anchors` 暂存，再在单事务内整体写入并切换 `current_version` 指针；
版本号回退抛 `E3001`。崩溃后 `jobs` 中残留 `started` 行，`resume` 只重放未完成请求，
已提交请求不受影响。

### 3.8 并发一致性与幂等 CLI（业务要求 8）

SQLite 启用 WAL 与 `busy_timeout`，写事务用 `BEGIN IMMEDIATE` 串行化；相同补丁哈希
返回 `idempotent_replay`，链结论对 `(hostname, purpose, generation)` 建唯一约束，
作业对 `(job_type, request_hash)` 幂等。CLI 全部输出稳定排序 JSON。

## 4. 命令用法

```bash
python -m tlsdiag --sample sample --db work/engine.sqlite3 gensamples   # 生成确定性样本
python -m tlsdiag load          # 仅解析校验两代信任库
python -m tlsdiag chain         # 输出统一信任视图、裁决依据、精度账本
python -m tlsdiag apply --version 1   # 合并并原子提交快照
python -m tlsdiag verify        # 构建/校验全部叶子链与吊销
python -m tlsdiag resume        # 只重放崩溃遗留的未完成作业
python -m tlsdiag status        # 稳定排序 JSON 状态
python -m tlsdiag verify-audit  # 完整精度账本+链+吊销+未完成作业审计
```

## 5. 本地验证结果

环境：Python 3.12.16 虚拟环境，`cryptography 50.0.2`、`pytest 9.2.0`，
标准库 `sqlite3`，无网络访问。

- 单元/集成测试：`python -m pytest -q` → **35 passed**。
- 静态检查：`python -m pyflakes tlsdiag tests` → 无告警。
- 端到端 CLI：`apply` 首次 `committed`、重复 `idempotent_replay`、版本回退 `E3001`；
  `verify` 产生 6 条链结论（expired→`E4005`、revoked→`E4007`，其余有效），
  吊销状态覆盖 `good/unknown/revoked`；`status` 输出精度敏感记录与逻辑时钟水位。
- 强制终止恢复：子进程 `os._exit(137)` 模拟 SIGKILL 留下 `started` 作业，
  `resume` 后 `remaining_unfinished=[]`，链结论完整（6 条）。
- 多进程幂等：4 个 spawn 进程并发 `apply` 同一补丁，稳定得到 1 次 `committed`、
  3 次 `idempotent_replay`（见 `test_concurrent_apply_commits_snapshot_once`）。

## 6. 设计取舍

- 证书 `not_valid_after` 精度为秒，毫秒级有效期会被截断，因此边界样本改用整秒
  有效期 + 毫秒级时钟偏移来构造确定性的精度敏感场景。
- 默认标度选择精确的 1000 ms/s 以保证样本可复算、偏差为零；非整除标度的误差传播
  通过专门测试覆盖，避免在主样本中引入难以解释的人为误差。
- 文件锁（`fcntl`，Windows 有回退标记）与 SQLite 事务并存，真正的写串行化由
  `BEGIN IMMEDIATE` + busy timeout 保证，文件锁主要用于跨进程互斥语义说明。
