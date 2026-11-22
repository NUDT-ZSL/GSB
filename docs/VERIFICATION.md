# 实际验证记录

生成自本地工作区，未访问线上配置中心或外部目标。完整机器可读记录见 [`artifacts/verification.json`](../artifacts/verification.json)。

## 环境

| 项 | 实际值 |
| --- | --- |
| Python | 3.11.18 |
| Flask | 3.1.3 |
| NumPy | 2.4.6 |
| SQLite | 3.53.1 |
| 状态目录 | `state/`（已加入 `.gitignore`） |

以下命令均在已激活工作区 `.venv` 的环境中执行。

## 自动化测试

```bash
python -m pytest -q tests/test_engine.py
```

结果：

```text
............. [100%]
14 passed
```

覆盖项：

- 非法时钟、错误类型、错误摘要均返回独立错误码，且版本表与指针保持为空。
- 重复 JSON 对象键被严格解析器拒绝。
- 同一快照重复 `load` 为幂等 `noop`。
- 同版本不同补丁为 `E_VERSION_CONFLICT`。
- 同一客户端轮次提交不同事件输入为 `E_ROUND_CONFLICT`，原结果不增加。
- 版本回退为 `E_VERSION_ROLLBACK`。
- 增量追加与完整替换分类正确。
- 完整替换会把上一版本同键余量带入，产生可追溯的定点漂移。
- 同一版本重复读取不会新增换算登记。
- 定点与 decimal 双通路逐项输出，所有 `bound_ok=true`。
- v1/v3 精度敏感与 100 倍标度探测翻转。
- `verify` 全项通过。
- `before-commit` 强制终止后只有一个 pending 作业，`resume` 按旧绑定版本重放一次。
- 计算中途应用新版本，当前计算仍绑定旧版本完成。
- 6 个进程并发应用同一补丁，只产生一个 v2。
- 6 个进程并发提交同一客户端轮次，只产生一条结果与一套误差账本。
- `status` 两次 CLI 输出逐字节一致。
- Flask 首页、bootstrap、compute、compare、verify API 冒烟通过。

## 可重复演示

```bash
python scripts/demo.py
```

实际控制台输出：

```text
[ok] compute-v1
  v1 threshold fixed/exact: 0.135 / 0.135117525
[expected-exit] crash-before-commit
[ok] apply-v2
[ok] hot-update-during-compute
[ok] resume-pending
[ok] compute-v3
[ok] compare-v1-v3
[ok] compare-clients
[ok] verify
[ok] business-error-codes

Report written to .../artifacts/verification.json
Final: v3, clock watermark 3500, precision-sensitive rounds 3, pending jobs 0
```

## 双通路结果

样本输入事件：

```text
e-001 value=0.13510 weight=2.50000
e-002 value=0.15000 weight=1.25000
e-003 value=0.12000 weight=9.00000
e-004 value=0.14000 weight=3.00000
e-005 value=0.16000 weight=0.75000
```

decimal 过滤阈值真值为 `0.135117525`，因此 `e-001` 本应被过滤。

### v1

| 派生量 | 定点 | decimal 真值 | 偏差 | 误差上界 | 上界覆盖 |
| --- | ---: | ---: | ---: | ---: | --- |
| threshold | 0.135 | 0.135117525 | -0.000117525 | 0.00021755 | 是 |
| effective_weight | 0.65 | 0.6501 | -0.0001 | 0.0001 | 是 |
| expected_batch | 24 | 24.99 | -0.99 | 2.01 | 是 |
| eligible_count | 4 | 3 | 1 | 1 | 是 |
| processed_count | 4 | 3 | 1 | 2 | 是 |
| weighted_sum | 0.692412 | 0.47294775 | 0.21946425 | 0.219649225 | 是 |
| output_count | 2 | 1.50015 | 0.49985 | 2.00025 | 是 |

v1 固定路径错误地包含 `e-001`：

```text
fixed filtered = ["e-001","e-002","e-004","e-005"]
exact filtered = ["e-002","e-004","e-005"]
```

### v3

| 派生量 | 定点 | decimal 真值 | 偏差 | 误差上界 | 上界覆盖 |
| --- | ---: | ---: | ---: | ---: | --- |
| threshold | 0.1352 | 0.135117525 | 0.000082475 | 0.00021755 | 是 |
| effective_weight | 0.7503 | 0.75015 | 0.00015 | 0.00015 | 是 |
| expected_batch | 25 | 24.99 | 0.01 | 2.01 | 是 |
| eligible_count | 3 | 3 | 0 | 1 | 是 |
| processed_count | 3 | 3 | 0 | 2 | 是 |
| weighted_sum | 0.545843 | 0.545734125 | 0.000108875 | 0.2534767893 | 是 |
| output_count | 1 | 1.50015 | -0.50015 | 2.00025 | 是 |

v3 固定路径与真值均排除 `e-001`。v1/v3 的配置声明值在阈值键上相同，但 v3 完整替换重新换算时带入 v1 的 `0.5` units 余量：

```text
filter_threshold v1: raw=1350.5, carry_in=0.0, fixed=1350, discarded=0.5
filter_threshold v3: raw=1350.5, carry_in=0.5, fixed=1351, discarded=0.0
retry_factor v1/v3  : carry_in 从 0.0 变为 0.5, fixed 从 1000 变为 1001
```

来源链中可直接定位：

```text
threshold source #1
  config_key=filter_threshold
  version=1
  conversion_id=2
  carried_in_units=0
  discarded_units=0.5
  residual_units=0.5
  contribution=0.000050025

threshold source #2
  config_key=retry_factor
  version=1
  conversion_id=5
  carried_in_units=0
  discarded_units=0.5
  residual_units=0.5
  contribution=0.000067525
```

## 版本敏感性

v1 → v3：

| 项 | 值 |
| --- | --- |
| 固定阈值差 | `-0.0002` |
| 真值阈值差 | `0` |
| 容差 | `0.001` |
| 固定差小于容差 | 是 |
| 过滤集合变化 | 是 |
| 精度敏感 | 是 |
| 100× 标度探测翻转 | 是 |

高标度探测输出：

```text
left fixed conclusion  = filtered [e-002,e-004,e-005], processed [e-002,e-004,e-005], output 1
right fixed conclusion = filtered [e-002,e-004,e-005], processed [e-002,e-004,e-005], output 1
```

因此低成本标度下由余量累计造成的过滤差异，在提高标度后回到 decimal 真值。

## 热更新与恢复

### 计算中途热更新

```text
compute process bound version = 2
applied while running         = 3
result bound version          = 2
```

说明当前计算完成时仍使用开始时绑定的 v2；v3 只对后续计算生效。

### 强制终止

```text
crash point                  = before-commit
process exit                 = 86
pending job bound version    = 1
committed result count       = 0
```

随后应用 v2、v3，再执行 `resume`：

```text
resumed_count = 1
resumed bound version = 1
pending_after = 0
```

再次以相同客户端/轮次调用 `compute` 返回 `idempotent=true`，结果仍只存在一条。

### 并发

- 6 个进程同时应用补丁 002：最终版本链为 `[1, 2]`，没有重复 v2。
- 6 个进程同时提交同一 `client/round`：最终只有一条 `client_results`，误差账本只挂在该结果上。

## verify

实际结果：

```text
checks=7, passed=7, failed=0, warnings=0
```

| 检查项 | 结果 |
| --- | --- |
| `version_chain` | pass |
| `conversion_replay` | pass |
| `result_and_job_atomicity` | pass |
| `pointer_isolation` | pass |
| `business_error_contract` | pass |
| `numeric_truth_bounds` | pass |
| `version_sensitivity` | pass |

## 业务错误码

实际 CLI 探针：

| 场景 | 输入 | 退出码 | 错误码 |
| --- | --- | ---: | --- |
| 负逻辑时钟 | `data/error_cases/bad_clock.json` | 1 | `E_BAD_CLOCK` |
| 声明 integer 但值为字符串 | `data/error_cases/bad_type.json` | 1 | `E_BAD_CONFIG` |
| 补丁摘要错误 | `data/error_cases/bad_digest_patch.json` | 1 | `E_DIGEST_MISMATCH` |
| 当前 v3 后重新应用 v2 补丁 | `data/patches/002_add_weight_gamma.json` | 1 | `E_VERSION_ROLLBACK` |

错误输出示例：

```json
{
  "error": {
    "code": "E_DIGEST_MISMATCH",
    "details": {
      "computed": "sha256:...",
      "declared": "sha256:0000..."
    },
    "message": "补丁 content_digest 与内容不一致"
  },
  "ok": false
}
```

## 复现顺序

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q tests/test_engine.py
python scripts/demo.py
python -m config_center verify
python -m config_center status
python -m config_center serve --host 127.0.0.1 --port 5000
```

