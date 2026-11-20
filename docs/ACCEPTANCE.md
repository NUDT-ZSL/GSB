# 验收记录

记录日期：2026-11-21。以下结果均来自本工作区，不访问外部网络，不使用容器。

## 环境

```text
Python 3.11.15
cryptography 50.0.2
pytest 9.2.0
SQLite 3.53.1
```

对 `sample/` 全目录生成前后做 SHA-256 清单比较，重新运行
`scripts/generate_sample.py` 后差异文件数为 `0`，包括 PEM、JSON、分片与
`metadata.sqlite`。

## 自动化测试

```bash
.venv/bin/pytest -q
```

结果：

```text
........................                                                 [100%]
24 passed in 23.17s
```

覆盖范围：

- v1 重复别名、指纹不匹配、无状态写入
- v2 分片哈希篡改、元数据墓碑语义、代数回退
- v1 相对秒数与毫秒精度记录
- SAN 精确/单层通配符、错误主机名
- 签名、CA 约束、路径长度、KeyUsage/EKU、有效期
- CRL/OCSP 吊销、证据缺失 `UNKNOWN`、吊销策略
- 锚签发中间证书标记
- 补丁闭包精确复用、逐字段不变、吊销按代重算
- 重复补丁与重复结论幂等
- 非法补丁错误路径零状态写入
- 逻辑时钟回拨零副作用
- 校验/补丁中途强杀与 `resume`
- 四进程并行校验
- 校验进程与补丁写入进程并行
- 两个独立数据库双次全管线字节一致

## 双次确定性

命令：

```bash
.venv/bin/python scripts/run_acceptance.py
```

每次运行使用独立数据库，依次执行：

```text
load -> chain -> verify -> apply -> verify -> status -> verify-audit -> resume
```

八个输出文件全部 `byte_identical: true`：

| 输出 | SHA-256 |
|---|---|
| `load.json` | `41e38c8a077c772cef9e4238366198714dc850c37aee910aff652536cb7be69b` |
| `chain.json` | `cd02c06e4cca3850c98c702b97ec0bd385c69919f16ffcf259518072cab10b5c` |
| `verify-before-patch.json` | `2eb82050e1b9ae1338de2dcf90f02acb06e92a95c9be64b58fa03edd7783774a` |
| `apply.json` | `5f586f19f933e46c1dfeee39a104a047cb3dc014a66097d713d232cd6a0d2d84` |
| `verify-after-patch.json` | `f0a9ebff985e746d515ba3c57945e5436c0a5ebddf21fa4a73011fa2879957f1` |
| `status.json` | `500b35123d4784ba2135064fdfd422ec7f2d12ab2b710456554c8c3794b8614f` |
| `audit.json` | `08f98ce14d4115992b6abdba86d4a107eb5487dd4b056af75bbc54a979da4c4c` |
| `resume.json` | `6e1404ba1b185061392c1d076709a59f0cf6902fd5bee5c6469f985ea239f774` |

完整报告：`artifacts/acceptance/report.json`。两次运行的原始输出分别在 `artifacts/acceptance/run-a/` 与 `run-b/`。

## 基线判定

| 请求 | 结构结果 | 原因 | 吊销 |
|---|---|---|---|
| `r01` api.example.test | VALID | OK | GOOD |
| `r02` foo.example.test | VALID | 单层通配符 `*.example.test` | GOOD（OCSP 命中） |
| `r03` wrong.example.test | INVALID | `E_HOSTNAME_MISMATCH` | GOOD |
| `r04` client.example.test | VALID | clientAuth | GOOD |
| `r05` revoked.example.test | VALID | 链结构有效 | REVOKED；完整 `verify` 拒绝为 `E_REVOKED` |
| `r06` unknown.example.test | VALID | 链结构有效 | UNKNOWN；完整 `verify` 拒绝为 `E_REVOCATION_UNKNOWN` |
| `r07` retired.example.test | VALID | root-retired 活动 | GOOD |
| `r08` path.example.test | INVALID | `E_PATH_LENGTH` | GOOD |
| `r09` expired.example.test | INVALID | `E_EXPIRED` | GOOD |
| `r10` rootb.example.test | VALID | root-b 锚 | GOOD |

## 增量闭包

应用 `p1-tombstone-root-retired.json`：

```text
chains_reused:                  9
chains_invalidated:             1
revocations_invalidated:       13
hostname_conclusions_reused:   10
```

结果：

- 不影响 `root-a`、`root-b` 下 9 条链；复制后的 `chain_json`、`evidence_json`、`payload_sha256` 与旧代数逐字节相同。
- `root-retired` 下 `r07` 旧链标记 `stale`，重新校验得到 `E_ANCHOR_TOMBSTONE`。
- 13 条旧代吊销结论全部标记 `stale`；新代重新读取证据，13 条均重新裁决。
- 10 条主机名结论原记录复用，负载摘要不变。
- 重复提交相同补丁返回 `idempotent`，不产生新快照或新失效事件。

补丁后审计：

```json
{
  "active_chains": 10,
  "active_revocations": 13,
  "current_generation": 3,
  "current_version": "trust-v2-p1",
  "ok": true,
  "running_jobs": 0
}
```

## 恢复

- `TLS_TRUST_CRASH_POINT=verify_before_commit` 强制退出码为 `99`：留下 1 个 `running` 作业，当前代链/吊销结论均为 0；`resume` 后链结论为 1、运行作业为 0、审计通过。
- `TLS_TRUST_CRASH_POINT=apply_before_commit` 强制退出码为 `99`：当前版本仍为 `trust-v2`，仅留下 1 个 `running` 作业；`resume` 后切换到 `trust-v2-p1`，审计通过。

## 并发回环

命令：

```bash
.venv/bin/python scripts/loopback_concurrency.py
```

本轮启动 4 个 `verify` 进程与 1 个 `apply` 进程并行：

```text
verify returncode: 0, 0, 0, 0
apply  returncode: 0
current_version:  trust-v2-p1
current_generation: 3
active chains:     10
active revocations: 13
running jobs:      0
audit:             ok
```

并发期间可能观察到快照切换重试；旧作业会被标记 `aborted`，不会留下 `running` 作业或半提交结论。完整报告：`artifacts/loopback/report.json`。

