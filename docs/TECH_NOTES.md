# 离线授权裁决组件技术说明

## 1. 交付范围

本项目是一个仅依赖 Python 标准库的离线 MFA 授权裁决组件（测试依赖为 `pytest`）。它不访问网络、真实 IdP、真实账号或系统时钟；所有时刻判断均使用样本 `bundle_manifest.json` 中的 `observation_clock_ms` 以及各声明/尝试自带的已声明毫秒时间。

主要目录：

- `offline_authz/parsing.py`：JSONL/JSON/SQLite 严格解析、字段校验、唯一性去重、拒绝记录。
- `offline_authz/policy.py`：v1/v2 策略规范化、v1 相对秒换算、权威代数/生效时刻选择、墓碑和冲突证据。
- `offline_authz/fingerprint.py`：定点数对齐、规范指纹键、误差区间和阈值安全比较。
- `offline_authz/factors.py`：`TOTP`、`WEBAUTHN`、`PUSH` 三类确定性占位证明分派。
- `offline_authz/graph.py`：用户→设备/绑定→因子→会话依赖图、传递闭包、SCC 环检测。
- `offline_authz/engine.py`：设备、因子、凭据档位、策略时刻和失效闭包的逐条裁决。
- `offline_authz/storage.py`：SQLite 账本、主键、摘要、检查点和恢复原语。
- `offline_authz/service.py`：五类 CLI 用例编排、JSON 报告和同步标记。
- `offline_authz/samplegen.py`：固定种子样本与真值生成器。
- `tests/`：21 个 pytest 用例，覆盖真值、指纹、闭包、坏行续处理、并发一致性、检查点和 CLI 退出码。
- `samples/generated/`：运行生成器后产出的样本、v2 SQLite 元数据库和真值文件。

## 2. Windows 安装与生成

要求 Python 3.11+。在 Windows PowerShell 中：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install pytest
python -m pip install -e .
python scripts\generate_samples.py
```

当前 Linux 验证环境中，系统 `python` 为 3.10.12，另外存在 `python3.11`；验证使用：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install pytest
```

代码保持 Python 3.10 兼容，因此系统 `python -m pytest -q` 也通过。

样本参数位于 `sample_params.json`，固定种子为 `20261125`，观测时钟为 `1767225600000`。生成命令：

```bash
python scripts/generate_samples.py --params sample_params.json
```

## 3. CLI

规范命令名为 `offline-authz`，也可使用 `python -m offline_authz`、`python main.py` 或安装后的 `authz`。

五类子命令及别名：

| 类别 | 规范子命令 | 别名示例 |
| --- | --- | --- |
| 导入 | `import` | `import-claims`、`导入` |
| 登录裁决 | `adjudicate` | `decide`、`login`、`登录裁决` |
| 吊销传播 | `revoke` | `propagate`、`closure`、`吊销传播` |
| 导出诊断报告 | `report` | `export-report`、`diagnose`、`导出诊断报告` |
| 校验检查点 | `verify-checkpoint` | `verify`、`checkpoint`、`校验检查点` |

典型增量流程：

```bash
python -m offline_authz import \
  --input-dir samples/generated --state-dir state-demo

python -m offline_authz adjudicate \
  --input-dir samples/generated --state-dir state-demo \
  --watermark 1 --workers 1

python -m offline_authz adjudicate \
  --input-dir samples/generated --state-dir state-demo \
  --watermark 2 --workers 4

python -m offline_authz revoke \
  --input-dir samples/generated --state-dir state-demo --watermark 2

python -m offline_authz report \
  --state-dir state-demo --output state-demo/diagnostic-report.json

python -m offline_authz verify-checkpoint --state-dir state-demo
```

一键脚本：

- Linux/macOS：`PYTHON=.venv/bin/python bash scripts/verify.sh`
- Windows：`.\scripts\verify.ps1 -InstallDeps`
- 并发确定性：`python scripts/verify_determinism.py`

退出码：

- `0`：命令成功且没有部分失败项；干净样本、报告导出和检查点校验使用此码。
- `1`：部分失败，例如声明拒绝、策略冲突、绑定环、检查点残留，或种子样本中的预期 `DENY`/拒绝记录。
- `2`：参数错误或 argparse 用法错误。
- `3`：未预期内部错误，输出可解析的 `E_INTERNAL` JSON。

## 4. 规范化与拒绝记录

每条拒绝记录至少包含：

```json
{
  "source_category": "user_credential",
  "record_seq": 13,
  "code": "E_DUPLICATE_USER",
  "message": "..."
}
```

覆盖的稳定码包括 `E_DUPLICATE_USER`、`E_DUPLICATE_FACTOR`、`E_INVALID_PLATFORM`、`E_INVALID_FINGERPRINT_COMPONENT`、`E_INVALID_DIGEST_TIER`、`E_INVALID_ITERATIONS`、`E_INVALID_EXPIRY_SECONDS`、`E_INVALID_TIMESTAMP_MS`、`E_INVALID_POLICY_GENERATION`、`E_INVALID_FACTOR_TYPE`、`E_INVALID_JSON` 等。

布尔值不会被当作整数接受。JSONL 单行损坏、单条字段非法或 SQLite 单行非法均只拒绝该记录，其余记录继续处理。

## 5. 策略合并

- v1：`expires_at_ms = observation_clock_ms + expires_in_seconds * 1000`，绝不读取系统时间。
- v2：读取分片 JSON 的绝对 `effective_at_ms`、`expires_at_ms`、`generation`、`tombstoned`，并用 SQLite `policy_meta` 补充/识别高代数墓碑。
- 规范策略键为 `policy:<policy_key>`。
- 权威顺序为 `(generation, effective_at_ms, source_priority, source)`。
- 高代数墓碑在时刻选择中直接覆盖较早的非墓碑策略。
- v1/v2 对 `required_factors`、`binding_requirement` 或 `expires_at_ms` 结论不同时输出 `E_POLICY_CONFLICT`，并在 `left`/`right` 中保留来源、记录序号、代数、生效/过期时刻和原始结论。
- 同代数、同生效时刻且结论冲突属于未裁决权威冲突，相关尝试返回 `E_POLICY_CONFLICT`；代数升级产生的已裁决冲突保留在合并报告和尝试 `warnings` 中。

## 6. 指纹定点化

规范空间为小数点后 6 位。源值是整数定点值 `raw_integer / 10**platform_decimals`：

- 源精度不高于 6：精确放大，误差界为 0。
- 源精度高于 6：向下对齐到规范整数，保留 `[lower, upper]` 区间，保守误差界为 `0.000001` 个规范单位。
- 规范键格式为 `fpv1:sha256:<digest>`。
- 比较输出观测距离、下界、上界、阈值、两侧键和组合误差界。
- 当不确定区间跨越阈值时为 `AMBIGUOUS`，裁决码为 `E_FINGERPRINT_AMBIGUOUS`，且不判定为同一设备。
- 下界明确大于阈值时为 `MISMATCH`，裁决码为 `E_DEVICE_MISMATCH`；结果含 `left_key`、`right_key` 和 `basis_generation`。

种子样本中 Bob 为歧义案例，Carol 为明确不符案例，Alice 为精确一致案例。

## 7. 登录裁决

一次裁决同时评估：

1. 设备存在性、用户归属、绑定墓碑、规范指纹比较；
2. 必需因子类型、因子归属/绑定状态、类型一致性和可选证明；
3. 凭据档位与迭代次数是否满足当前生效策略。

主要独立码：

- `E_DEVICE_NOT_FOUND`、`E_DEVICE_NOT_BOUND`、`E_DEVICE_BINDING_REVOKED`、`E_DEVICE_MISMATCH`、`E_FINGERPRINT_AMBIGUOUS`
- `E_MISSING_FACTOR`、`E_FACTOR_NOT_BOUND`、`E_FACTOR_REVOKED`、`E_FACTOR_TYPE_MISMATCH`、`E_FACTOR_PROOF_INVALID`
- `E_DIGEST_TIER_TOO_LOW`、`E_ITERATIONS_TOO_LOW`
- `E_POLICY_NOT_FOUND`、`E_POLICY_NOT_EFFECTIVE`、`E_POLICY_EXPIRED`、`E_POLICY_TOMBSTONED`、`E_POLICY_CONFLICT`
- `E_INVALIDATED_BY_CLOSURE`、`E_BIND_CYCLE`

若提交中没有 `challenge`/`proof_digest` 等证明材料，组件按“绑定因子清单命中”处理；若提供证明材料，则执行对应类型的确定性校验。

## 8. 失效闭包与环

图边包括：

- `user -> device`
- `device -> factor`
- `factor -> session`
- 会话中的额外因子边
- v2 绑定到设备边
- 样本声明的 `FACTOR_DEPENDENCY` 和 `DEVICE_BINDING_CHAIN` 边

吊销根来自因子解绑、设备/绑定墓碑、凭据迁移用户和策略升级后不满足新下限的用户。闭包为从根出发的确定性可达集合。SCC 检测输出 `FACTOR_DEPENDENCY_CYCLE`、`DEVICE_BINDING_CHAIN_CYCLE` 或通用 `BIND_DEPENDENCY_CYCLE`，并列出环内全部规范键和稳定循环路径。

所有节点、边、SCC 和闭包均排序；随机打乱声明顺序后结论不变。增量两批提交与 `--rebuild` 全量重算的 `result_hash` 和账本摘要逐条一致。

## 9. SQLite 账本、快照版本和检查点

账本表 `ledger` 使用 `PRIMARY KEY (user_id, attempt_seq)`，记录：

- `decision`、`codes_json`
- `policy_key`、`policy_generation`
- `canonical_fingerprint_key`
- `judgement_watermark`、`logical_clock`、`batch_watermark`
- 完整确定性 `result_json`、`input_hash`、`result_hash`

每批裁决在一个 `BEGIN IMMEDIATE` 事务中写入全部账本行和检查点行。版本元数据在任何 DDL 前读取：

- `layout_version` 不符：`E_LEDGER_LAYOUT_MISMATCH`，拒绝增量续接，需 `--rebuild`。
- `snapshot_version` 不符：`E_SNAPSHOT_VERSION_MISMATCH`，拒绝混用旧结论。

检查点包含 `watermark`、`ledger_digest`、`row_count`、`snapshot_version` 和 `sync_marker`，同时写入 SQLite `checkpoints` 表以及：

- `state/checkpoints/checkpoint-000001.json`
- `state/checkpoints/checkpoint-000002.json`
- `state/checkpoints/checkpoint-latest.json`

JSON/报告写入均使用同目录临时文件、写入后回读解析、`os.replace` 原子替换和目录 fsync。

恢复边界：

- SQLite 检查点与账本摘要一致：从最后水位继续。
- 存在 `batch_watermark > checkpoint.watermark` 的账本残留：报告 `E_CHECKPOINT_RESIDUE`，恢复时删除半写/越界行后回到最后完整检查点。
- 残留 `*.tmp`：报告 `E_HALF_WRITTEN_FILE`，但绝不作为检查点读取。
- SQLite 已提交而文件检查点尚未生成：可由 SQLite 检查点重新生成文件。
- 编号检查点损坏：忽略该不可解析文件并报告 `E_CHECKPOINT_MISMATCH`。
- 账本摘要与检查点不一致：抛出 `E_CHECKPOINT_MISMATCH`，在人工或 `--rebuild` 前不推进账本。

`kill -9` 若发生在 SQLite 事务提交前，事务回滚；若发生在 SQLite 提交后、报告/文件重命名前，不会产生承诺的退出报告，恢复逻辑按上述边界处理。

## 10. 实际验证结果

最终本地执行结果：

- Python 3.11.18 虚拟环境：`21 passed`。
- 系统 Python 3.10.12：`21 passed`（额外兼容性确认；任务要求的 3.11 由 `.venv` 提供）。
- `scripts/verify_determinism.py`：串行与 4 并发结果 JSON 完全一致，`sync_marker=7ce1c255d9644e51`。
- 种子样本：12 个有效用户、12 个有效设备、18 个有效因子、13 个有效裁决尝试；另有 12 条预期拒绝记录。
- 第一批：10 条裁决，`allow=2`、`deny=8`。
- 第二批：3 条裁决，`allow=0`、`deny=3`；账本共 13 行。
- 吊销重判：`checked=13`、`consistent=true`、`mismatches=[]`。
- 检查点：水位 2，13 行，账本摘要 `8c35809124e18e8acdd95fcce0215a92b7f22c60255a949154546378da690492`，校验 `ok=true`。
- CLI 退出码：干净/报告/校验为 `0`；种子部分失败为 `1`；参数错误为 `2`。

可复现命令：

```bash
PYTHON=.venv/bin/python bash scripts/verify.sh
```
