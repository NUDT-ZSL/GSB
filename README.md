# 配置漂移取证与裁决（离线版）

本工作区实现一个只依赖 Python 标准库的离线配置漂移取证应用。它从本地确定性 JSON 证据与基线声明出发，重建每台主机、每个规范配置键的观测链，识别断链、环、重复/冲突、篡改、算法位宽错误、首个漂移点和跨主机事件簇，并原子写出裁决报告与证据链图。

实现不使用系统当前时间作为业务真值。漂移与生效性只依赖证据中显式给出的采集序号和基线生效区间；`time.monotonic()` 只用于单文件资源预算。

## 环境与目录

本机实际验证环境：

- `python --version`：`Python 3.12.13`
- `python -m pytest --version`：`pytest 9.2.0`
- 运行时不访问网络，不依赖外部服务、Docker 或数据库

主要目录：

```text
src/config_forensics/   应用源码
samples/                已生成的确定性样本、基线与真值 manifest
artifacts/              实际验收运行产生的报告和图
tests/                  本地 pytest 测试
run_forensics.py        无需安装的源码树启动器
```

## 快速运行

Linux / WSL：

```bash
PYTHONPATH=src python -m config_forensics sample --out samples
PYTHONPATH=src python -m config_forensics run \
  --evidence-dir samples/evidence \
  --baseline samples/baseline/baseline.json \
  --report artifacts/report.json \
  --graph artifacts/graph.json \
  --checkpoint artifacts/checkpoint.json \
  --workers 4 \
  --queue-limit 64 \
  --max-file-seconds 5
python -m pytest
```

也可以使用源码树启动器：

```bash
PYTHONPATH=src python run_forensics.py run --evidence-dir samples/evidence \
  --baseline samples/baseline/baseline.json \
  --report artifacts/report.json \
  --graph artifacts/graph.json
```

Windows PowerShell 中把 `PYTHONPATH=src` 改为：

```powershell
$env:PYTHONPATH = "src"
python -m config_forensics run --evidence-dir samples\evidence `
  --baseline samples\baseline\baseline.json `
  --report artifacts\report.json --graph artifacts\graph.json
```

可选离线 HMAC 密钥文件是 JSON 对象，例如 `{"key-id":"local-secret"}`；也可把 `hmac_keys` 放在证据文件顶层。应用不会联网取密钥。

## 证据格式

一个证据文件可包含 `records` 数组，也接受 `evidence`、`items` 或单条记录对象。规范格式如下：

```json
{
  "schema_version": 1,
  "file_id": "evidence-alpha-1",
  "hmac_keys": {"demo-key": "local-secret"},
  "records": [
    {
      "evidence_id": "alpha-1",
      "host_id": "host-alpha",
      "sequence": 1,
      "path": "features[0].enabled",
      "value": true,
      "algorithm": "sha256",
      "digest_bits": 256,
      "digest": "64-hex-characters",
      "previous_evidence_id": null,
      "hmac_key_id": "demo-key",
      "sync_marker": "sync:host-alpha:1",
      "sender_observed_value": true
    }
  ]
}
```

字段说明：

- `host_id`：非空主机标识。
- `sequence`：非负整数采集序号；也接受别名 `seq`。
- `path`：点路径或数组下标路径。`features.0.enabled` 与 `features[0].enabled` 均规范化为 `features[0].enabled`。带点或空格的键使用 `["a.b"]` 形式。
- `value`：任意有限 JSON 值。
- `algorithm`：`sha256` 或 `hmac-sha256`，必须在白名单内。
- `digest_bits`：默认 256；与算法或摘要十六进制长度不一致时单独报 `E_DIGEST_WIDTH_MISMATCH`。
- `digest`：摘要十六进制；也接受 `sha256:<hex>` 前缀，前缀与 `algorithm` 不一致时报 `E_ALGORITHM_DECLARATION_MISMATCH`。
- `evidence_id`：证据标识；缺省时使用 `<file_id>:<record_index>`。
- `previous_evidence_id`：指向同主机、同规范配置键的前一条证据；根证据为 `null`。也接受 `prev_id`、`previous_id`、`link_id`。
- `sync_marker`、`sender_observed_value`：由样本生成器显式记录，便于复核与真值审计；不参与时间判定。

摘要内容固定为规范化 JSON：

```python
payload = json.dumps(
    {"path": normalized_path, "value": value},
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
digest = sha256(payload).hexdigest()          # 或 HMAC-SHA256
```

同一 `(host_id, sequence)` 重复投递时：

- 摘要算法与摘要完全一致：去重，所有证据 ID 保留为别名，链接可指向任意别名。
- 摘要不一致：判定 `E_EVIDENCE_CONFLICT`，该序号的所有投递都不进入观测链。

## 基线格式

```json
{
  "schema_version": 1,
  "baselines": [
    {
      "host_id": "host-alpha",
      "path": "service.port",
      "source": "policy-service-v1",
      "effective_from_seq": 1,
      "effective_to_seq": 100,
      "effective_from_inclusive": true,
      "effective_to_inclusive": true,
      "ranges": [
        {
          "interval": "[8000,8100)",
          "source": "range-service-main"
        },
        {
          "min": 9000,
          "max": 9100,
          "min_inclusive": true,
          "max_inclusive": true,
          "source": "range-service-canary"
        },
        {
          "allowed_values": [8080, 8081],
          "source": "range-service-exact"
        }
      ]
    }
  ]
}
```

端点语义完全按声明执行：

- `[a,b]`：两端均含。
- `(a,b)`：两端均不含。
- `[a,b)`、`(a,b]`：分别使用方括号和圆括号的含入语义。
- `min_inclusive` / `max_inclusive` 或兼容别名同样生效。
- 生效区间按采集序号判断，`effective_from_inclusive`、`effective_to_inclusive` 可显式控制端点是否生效。

单条基线非法时记录 `file_index=1`、声明序号和业务错误码，继续处理其余声明。

## 裁决语义

1. 证据按主机、规范配置键建立链；链接只允许指向同主机、同配置键且序号更早的现存证据。
2. `E_SELF_LINK`、`E_LINK_CYCLE`、`E_LINK_TARGET_MISSING`、`E_LINK_CROSS_CHAIN`、`E_LINK_SEQUENCE_ORDER`、`E_CHAIN_FORK`、`E_CHAIN_MULTIPLE_ROOTS` 都会使链进入异常状态，并给出相关采集序号。
3. 摘要不匹配的证据标记 `E_TAMPERED`，从链中排除，同时在 `tampered` 清单中给出主机与采集序号。
4. 只有链接结构完整的链参与漂移裁决；结构异常或缺少基线的链不会被错误地报告为完整链漂移。
5. 对每个观测，先找出该采集序号下所有生效的基线规则，再按端点含入语义判断值是否被任一规则允许。首个不被允许的观测成为漂移点，并记录全部授权来源。
6. 同一规范配置键的漂移点按 `(sequence, host_id, evidence_id)` 排序归并为一个事件簇。单主机簇也会输出，并以 `cross_host=false` 标识；跨主机簇的 `cross_host=true`，且 `host_ids`、成员都按稳定键排序。
7. 所有证据、链、漂移点、事件簇、错误和图节点/边均使用稳定键排序，不依赖字典遍历顺序或线程完成顺序。

## 并发、预算与超时

- `--workers` 控制并发度；`--queue-limit` 控制等待窗口。准入容量为 `workers + queue_limit`，容量耗尽时文件立即以 `E_QUEUE_FULL` 拒绝入队，并写入文件级结论。
- 每个工作线程用真实 `time.monotonic()` 测量自己的处理耗时；主线程也按提交截止时间监控未完成线程。
- 超过 `--max-file-seconds` 的文件标记 `E_FILE_TIMEOUT`，其已解析/未解析记录全部丢弃，不参与链、漂移或事件簇；其他文件继续裁决。
- 不终止、不声称终止失控线程。超时问题详情明确写有 `"thread_termination_claimed": false`。
- 队列拒绝、超时文件均计入报告和预算统计；它们不是“已完成文件”，不进入检查点。

## 检查点与中断边界

- 每个完成后文件都会更新检查点；检查点保存完成文件标识、内容 SHA-256、文件级记录/错误、累计状态摘要、代际号和配置指纹。
- 检查点与报告都先写入同目录临时文件、`fsync`，再用 `os.replace` 整体替换。
- 检查点/输出使用非阻塞文件锁，冲突分别返回 `E_CHECKPOINT_LOCK` / `E_OUTPUT_LOCK`。
- 检查点写入带代际校验；另一个写者已推进代际时返回 `E_CHECKPOINT_CONFLICT`，不覆盖较新的状态。
- 检查点结构版本、裁决版本、摘要算法白名单、基线摘要或 HMAC 密钥指纹变化时整体重建，不增量续接。
- 文件内容变化或检查点累计状态哈希不一致时也整体重建。
- 进程被强制终止时只读取最后一份原子完成的检查点，不读取临时文件；不承诺在终止前生成退出报告。

## 报告与图

`report.json` 包含：

- `files`：文件级状态（`accepted`、`partial`、`rejected`、`timed_out`、`queue_rejected`）、记录数、摘要错误数、错误码。
- `evidence`：去重后的规范化证据。
- `chains`：每主机每配置键的链状态、采集序号、观测、错误、漂移点。
- `drift_points`：全局稳定排序的首个漂移点及授权来源。
- `event_clusters`：同配置键跨主机漂移簇。
- `chain_breaks`、`tampered`、`conflicts`、`errors`。
- `summary`、`budget`、`checkpoint`。

`graph.json` 包含稳定排序的 `nodes` 和 `edges`。节点标记 `tampered`、`conflict`、`drift`、`chain_status`、`root`；边标记 `ok`、`missing`、`self`、`cycle`、`cross_chain`、`sequence_order`、`tampered_target` 等状态。

## 主要业务错误码

| 分类 | 错误码 |
|---|---|
| 文件/结构 | `E_JSON_INVALID`、`E_FILE_READ`、`E_SCHEMA_INVALID` |
| 字段 | `E_HOST_INVALID`、`E_SEQUENCE_INVALID`、`E_PATH_INVALID`、`E_VALUE_INVALID`、`E_EVIDENCE_ID_MISSING`、`E_EVIDENCE_ID_DUPLICATE` |
| 摘要 | `E_ALGORITHM_UNSUPPORTED`、`E_ALGORITHM_DECLARATION_MISMATCH`、`E_DIGEST_WIDTH_MISMATCH`、`E_DIGEST_INVALID`、`E_HMAC_KEY_MISSING`、`E_TAMPERED` |
| 重复/链 | `E_EVIDENCE_CONFLICT`、`E_LINK_INVALID`、`E_LINK_TARGET_MISSING`、`E_LINK_TARGET_TAMPERED`、`E_LINK_TARGET_CONFLICT`、`E_LINK_CROSS_CHAIN`、`E_LINK_SEQUENCE_ORDER`、`E_SELF_LINK`、`E_LINK_CYCLE`、`E_CHAIN_FORK`、`E_CHAIN_BROKEN`、`E_CHAIN_MULTIPLE_ROOTS` |
| 基线 | `E_BASELINE_INVALID_RANGE`、`E_BASELINE_INVALID_INTERVAL`、`E_BASELINE_DUPLICATE`、`E_BASELINE_MISSING`、`E_BASELINE_NOT_EFFECTIVE` |
| 预算 | `E_FILE_TIMEOUT`、`E_QUEUE_FULL` |
| 持久化 | `E_CHECKPOINT_LOCK`、`E_CHECKPOINT_CONFLICT`、`E_CHECKPOINT_CORRUPT`、`E_CHECKPOINT_VERSION`、`E_CHECKPOINT_CONFIG_CHANGED`、`E_OUTPUT_LOCK`、`E_OUTPUT_WRITE` |

## 样本与测试

`python -m config_forensics sample --out samples` 生成：

- 20 个乱序证据文件；
- 显式的 `sender_observed_value` 与 `sync_marker`；
- 断链、环、篡改、位宽错误、HMAC、重复冲突、跨主机漂移和端点含入边界；
- `manifest.json` 真值清单。

测试覆盖：

- 点路径/数组下标统一与非法路径；
- SHA-256、HMAC-SHA256、位宽错误；
- 基线非法条目继续解析与端点/生效区间语义；
- 乱序并发与串行裁决逐字段等价；
- 同序号摘要一致去重、不一致冲突；
- 断链、自链、环及错误序号；
- 篡改排除、跨主机事件簇、稳定输出顺序；
- 队列拒绝、真实耗时超时、并发度差异；
- 检查点强制中断恢复、版本/算法白名单变化重建、锁冲突；
- 报告/图原子写入与输出锁。

实际回归结果：

```text
$ python -m pytest
..............................                                           [100%]
30 passed in 1.69s
```

另以 Python 3.11.15 运行同一测试集，结果为 `30 passed in 1.69s`。

实际样本裁决结果：

- `artifacts/verification.json` 记录了样本运行、串并行等价复核和摘要统计。
- 漂移点：`host-alpha/service.port@3`、`host-beta/service.port@2`、`host-gamma/features[0].enabled@3`、`host-hmac/tls.enabled@1`、`host-endpoint/quota.limit@1`。
- `service.port` 归并为跨主机事件簇 `[host-alpha, host-beta]`。
- `host-broken` 返回 `E_LINK_TARGET_MISSING`；`host-cycle` 返回 `E_LINK_CYCLE`。
- `host-tamper@1` 被排除并列入篡改清单；`host-width@1` 返回 `E_DIGEST_WIDTH_MISMATCH`；`host-conflict@1` 返回 `E_EVIDENCE_CONFLICT`。
- `workers=1` 与 `workers=8` 的规范化裁决投影和图数据完全相等。

## 已知边界

- Python 线程无法被安全强杀。超时后会丢弃结果并停止其后续裁决，但已在执行的文件函数可能短暂继续运行到自然返回；报告明确不声称线程被终止。
- 结构异常链不产生漂移点，避免在证据不完整时给出不可复核结论；断链原因仍完整输出。
- `event_clusters` 输出所有配置键漂移组；单主机组通过 `cross_host=false` 区分。
