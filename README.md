# 本地 CTR/CVR 联合预估数据编辑与操作组件

这是一个完全本地、确定性可复现的搜索推荐领域工具包，覆盖：

- 展示日志 JSONL / Parquet 的读取与校验；
- v1 / v2 / v3 三版本特征规范互操作、双向迁移与迁移记录重放；
- 特征装配、CTR/CVR 联合打分、逐项贡献和在线统计量累积；
- 装配、打分、累积三类节点的内容寻址缓存；
- 门限变化、统计量更新、特征规范升级、标签修正的定向失效；
- 打分追溯重放、过期产物拒绝、快照整体替换、并发提交和资源上限；
- 跨模块一致性自检，以及输出 JSON 的完整命令行演示。

所有样本、特征规范和统计量均由固定种子在仓库内生成，不访问网络、真实账号或线上站点。

## 快速开始

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .

python scripts\generate_demo_data.py
python -m ctr_cvr_joint demo --data data --out artifacts\demo
python -m ctr_cvr_joint verify --out artifacts\demo
pytest -q
```

### Windows 命令提示符

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .

python scripts\generate_demo_data.py
python -m ctr_cvr_joint demo --data data --out artifacts\demo --summary
python -m ctr_cvr_joint verify --out artifacts\demo
pytest -q
```

演示命令默认输出完整 JSON 报告。加 `--summary` 可只输出关键检查、各次运行摘要、迁移计数和业务码；完整内容始终写入：

```text
artifacts/demo/demo_report.json
artifacts/demo/migrations.json
artifacts/demo/assembly_summary.json
artifacts/demo/contributions.json
artifacts/demo/invalidation.json
artifacts/demo/business_codes.json
artifacts/demo/run_summaries.json
artifacts/demo/migrated_*.json
artifacts/demo/migrated_*.txt
```

## 已提交的确定样本

`data/MANIFEST.json` 显式记录 `seed=20261124`、`sample_count=12`、样本字段和每个文件的 SHA-256。生成逻辑在 `scripts/generate_demo_data.py`。

样本字段：

```text
request_id, timestamp_ms, user_id, item_id,
numeric_feats, categorical_feats, label_click, label_convert
```

特征：

```text
numeric:      ctr_base, price, dwell, quality
categorical:  device, slot, city
cross:        device_slot(code, affinity)
              price_quality(product, affinity)
statistics:   ctr, cvr
```

提交文件：

| 文件 | 内容 |
|---|---|
| `data/requests.jsonl` | 12 条展示日志，固定种子生成 |
| `data/requests.parquet` | 同一批样本的本地列式表示，只用 PyArrow 读写 |
| `data/spec_v1.txt` | 无列名、竖线分列的行式规范 |
| `data/spec_v2.json` | 带字段名键值配置，逐特征缺失策略 |
| `data/spec_v3.json` | 结构化嵌套规范，显式默认值和版本化统计口径 |
| `data/spec_v3_rev2.json` | v3 的下一修订，用于演示特征规范升级 |
| `data/stats_v1.json` | 离线统计量快照 v1 |
| `data/stats_v2.json` | 只更新 `city=shenzhen` 的局部统计量快照 v2 |

## 三版本规范

### v1：竖线行式、无列名

```text
version=1
N|ctr_base|0.42|0.16|0.55
N|price|88|24|0.22
C|device|0.18|32
X|device_slot|device|slot|code,affinity|0.20
S|ctr|0.18|8
S|cvr|0.07|6
```

字段位置：

- `N|name|mean|std|weight`
- `C|name|weight|buckets`
- `X|name|left|right|slots_csv|weight`
- `S|metric|prior|smoothing`

### v2：键值 JSON，逐特征缺失策略

```json
{
  "format_version": 2,
  "features": {
    "numeric": [
      {
        "name": "ctr_base",
        "mean": 0.42,
        "std": 0.16,
        "weight": 0.55,
        "missing_strategy": "mean"
      }
    ],
    "categorical": [
      {
        "name": "device",
        "weight": 0.18,
        "buckets": 32,
        "missing_strategy": "default"
      }
    ],
    "cross": []
  },
  "statistics": {
    "ctr": {"prior": 0.18, "smoothing": 8},
    "cvr": {"prior": 0.07, "smoothing": 6}
  }
}
```

### v3：嵌套 JSON，默认值与版本化统计口径

```json
{
  "format_version": 3,
  "numeric_features": {
    "ctr_base": {
      "stats": {"mean": 0.42, "std": 0.16},
      "weight": 0.55,
      "default_value": 0.42,
      "missing": {"strategy": "default"}
    }
  },
  "categorical_features": {},
  "cross_features": {},
  "statistics": {
    "ctr": {"prior": 0.18, "smoothing": 8},
    "cvr": {"prior": 0.07, "smoothing": 6}
  },
  "stats_semantics": {
    "name": "snapshot_bayes_decay",
    "version": 3,
    "decay": 1.0
  }
}
```

读取器先检查显式版本头；没有版本头时按字段集分派。未知版本返回 `E_VERSION_UNSUPPORTED`。竖线字段数、缺失键、缺失必填字段和非数值分别返回带 `line`、`field` 的 `E_FORMAT_INVALID`。

`LoadedSpec` 同时保存：

- `version`
- 原始内容 SHA-256 `checksum`
- 每一行的 `LineSummary`（行号、行哈希、预览）
- 解析后的内部规范

## 迁移与重放

`migrate_spec` 支持任意版本到 v1/v2/v3。每条迁移记录包含：

```json
{
  "op": "fill",
  "path": "numeric.ctr_base.default",
  "value": 0.0,
  "source": "compatibility:v1_implicit",
  "target_expressible": false,
  "reason": "kept in the unified internal representation; target serialization omits it"
}
```

升级时补全：

- 缺失策略；
- 数值/类别默认值；
- 统计口径名称和版本。

降级时显式记录丢弃/展平：

- v3 数值 `missing.strategy=default` 在 v2 中展平为 `zero`；
- v1 无法表达的逐特征缺失策略被丢弃；
- v1/v2 无法表达的默认值、统计口径版本被丢弃；
- 丢弃记录同时保存 `dropped` 原值和 `fallback` 回退值。

`replay_migration` 只依赖源规范与迁移记录即可重建目标内部表示。`migrate_spec` 在返回前自动执行重放比对；不一致会返回 `E_FORMAT_INVALID`。

## 确定性装配

处理顺序固定为：

```text
request_id 升序，再 timestamp_ms 升序；重复 request_id 立即 E_DUP_REQUEST。
```

数值特征：

```text
z = clip((x - mean) / std, -8, 8)
```

类别特征固定编码：

```text
bucket = int(sha256(f"{seed}|{feature}|{value}")[:16], 16) mod buckets
encoded = bucket / (buckets - 1)
```

交叉槽位展开：

- `code`：固定 64 桶稳定哈希；
- `affinity`：`1 / (1 + abs(left - right))`；
- `product`：`left * right`。

缺失值按当前内部规范的策略处理：`zero`、`mean`、`default`、`error`。

矩阵格式为 `CTRMAT1\0` 魔数 + 固定宽度元数据 + little-endian float64。相同输入、规范与种子会生成逐字节相同的矩阵、行哈希和装配摘要。

## 联合打分口径

基础 logit：

```text
base_click = logit(stats.global.ctr.prior)
base_cvr   = logit(stats.global.cvr.prior)
```

平滑率：

```text
smoothed = (rate * count + prior * smoothing) / (count + smoothing)
delta    = logit(smoothed) - base
```

逐项贡献：

```text
numeric:     weight * z * delta
categorical: weight * (2 * encoded - 1) * delta
cross:       (weight / slot_count) * encoded * delta
```

概率：

```text
click_logit = base_click + sum(click_contributions)
convert_logit = base_cvr + sum(convert_contributions)
                + 0.25 * click_logit
p = sigmoid(clip(logit, -60, 60))
expected_conversion = p_click * p_convert
```

概率按 `stable_digits=12` 做十进制量化，并设置 `1e-12` 的次正规/零值保护。每条贡献都写入 trace，`recompute_score_row` 只加总 trace 中的 `base_logit` 与 `contribution` 就能重放出同一概率。

门限策略：

- `strict`：量化后 `p > threshold`；
- `tolerance`：`abs(p - threshold) <= tolerance` 时使用确定性的 `p >= threshold` 打破边界，两侧超出容差则按符号判定；
- 非法门限返回 `E_THRESHOLD_INVALID`；
- 非法容差返回 `E_TOLERANCE_INVALID`。

## 三类缓存节点

缓存目录为 `workspace/cache/<node>/<key[:2]>/<key>.json`。每个文件保存：

- 节点类型；
- 缓存键和完整键组成；
- `request_id`、节点路径；
- 产物内容哈希；
- 产物本体。

读取时重新计算产物哈希。任何键、类型或内容校验失败都返回 `E_CACHE_CORRUPT`。

键组成包含：

```text
assembly:
  sample_content_hash
  internal_spec_version
  spec_checksum
  statistics_snapshot_version = not_applicable
  upstream_product_hash = root
  seed, protocol

scoring:
  sample_content_hash
  internal_spec_version
  spec_checksum
  statistics_snapshot_version（该样本实际使用的统计依赖子集）
  statistics_checksum（统计投影指纹）
  upstream_product_hash（装配产物哈希）
  scoring_config_checksum

accumulation:
  sample_content_hash
  internal_spec_version
  spec_checksum
  statistics_snapshot_version
  statistics_checksum
  upstream_product_hash（打分概率产物哈希，不含门限判定）
  label_hash
```

`engine.explain_request(request_id)` 返回每个节点的键组成、缓存键、产物哈希和节点路径。样本标签不进入装配/打分键，因此标签修正可以复用上游。

键中的规范校验和与统计量版本使用“节点实际依赖投影”：只有样本真正使用的
特征/统计序列或全局统计发生变化时，相关键才变化；整份规范快照版本与校验和
仍完整保存在 trace 中，供审计和旧产物拒绝使用。

## 定向失效

| 变化 | 装配 | 打分 | 累积 |
|---|---:|---:|---:|
| 仅门限/判定策略 | 复用 | 重算 | 复用 |
| 统计量局部更新 | 复用 | 仅依赖受影响统计序列的样本重算 | 仅受影响样本重算 |
| 特征规范升级 | 受影响样本重算，派生特征变化 | 全部下游重算 | 全部下游重算 |
| 标签修正 | 复用 | 复用 | 仅修正样本重算 |

统计量快照使用逐序列版本（例如 `categorical.city.shenzhen`）。演示中 v2 只修改深圳统计，因此只有 `req-0003`、`req-0007` 的评分与累积重算，其余 10 条复用。

每次运行报告：

- `affected_samples`
- `recomputed_samples`
- `reused_samples`
- `recomputed_nodes`
- 受影响 request_id 集合
- 变化路径（特征规范或统计量）
- 受影响基础特征与交叉派生特征
- 增量与全量重算的矩阵/分数/累积哈希是否一致

## 追溯与拒绝旧产物

每条 score trace 保存：

- score 缓存键、完整键组成和节点路径；
- 特征规范格式版本、快照版本、内容校验和；
- 统计量快照版本、依赖投影版本和指纹；
- 门限配置校验和；
- 装配、打分、累积三类产物哈希；
- 完整逐项贡献、概率和保护信息；
- trace 自身内容哈希。

`engine.replay_trace(trace)` 默认要求依赖仍为当前版本：

1. 校验 trace 与 score 行内容哈希；
2. 比较特征依赖指纹、统计投影指纹、门限配置；
3. 任一变化返回 `E_LINEAGE_BROKEN`，并指出 `request_id` 与失效依赖；
4. 依赖一致时从贡献明细重放概率和门限判定。

## 快照、并发与资源

- 特征规范、统计量、打分结果分别使用单调递增快照版本；
- 更高版本以完整 payload 整体写入，不做增量修补；
- 回退返回 `E_SNAPSHOT_ROLLBACK`；
- 同版本不同校验和、文件损坏或校验和错误返回 `E_CACHE_CORRUPT`；
- 增量提交使用进程内 guard + OS 非阻塞文件锁，冲突立即返回 `E_STAGE_BUSY`；
- `max_feature_matrix_bytes` 超限返回 `E_RESOURCE_LIMIT`；
- `parallel_nodes` 超过 `max_parallel_nodes` 时默认降级，并在报告 `degraded=["parallelism_capped"]` 标记。

`engine.self_check()` 活体校验：

- 三类节点的 request_id 集合一致；
- 每个缓存键由键组成重新哈希后一致；
- 每个产物哈希与落盘内容一致；
- 重算/复用集合不相交且覆盖全部请求；
- 受影响集合被覆盖；
- 每条 trace 可从贡献明细重放。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_VERSION_UNSUPPORTED` | 未知规范版本 |
| `E_FORMAT_INVALID` | 行/字段/数值/样本格式错误 |
| `E_DUP_REQUEST` | 重复 request_id |
| `E_THRESHOLD_INVALID` | 门限策略或门限值非法 |
| `E_TOLERANCE_INVALID` | 容差非法 |
| `E_RESOURCE_LIMIT` | 矩阵内存或并行节点超限 |
| `E_SNAPSHOT_ROLLBACK` | 快照版本回退 |
| `E_CACHE_CORRUPT` | 缓存/快照/追溯内容校验失败 |
| `E_STAGE_BUSY` | 已有增量提交持有锁 |
| `E_LINEAGE_BROKEN` | 旧结果依赖版本已变化 |

## 模块索引

| 模块 | 职责 |
|---|---|
| `ctr_cvr_joint/specs.py` | 三版本读取、分派、规范化和序列化 |
| `ctr_cvr_joint/migration.py` | 双向迁移、补全/丢弃记录和重放 |
| `ctr_cvr_joint/samples.py` | JSONL/Parquet 样本读取、排序、标签修正 |
| `ctr_cvr_joint/assembly.py` | 确定性特征装配和矩阵编码 |
| `ctr_cvr_joint/scoring.py` | 联合打分、贡献、门限、重算 |
| `ctr_cvr_joint/statistics.py` | 统计快照、依赖投影、累积 |
| `ctr_cvr_joint/cache.py` | 内容寻址缓存和非阻塞提交锁 |
| `ctr_cvr_joint/snapshots.py` | 单调快照整体替换和校验 |
| `ctr_cvr_joint/engine.py` | 编排、失效、trace、自检 |
| `ctr_cvr_joint/demo.py` | 完整本地演示和 JSON 报告 |
| `scripts/generate_demo_data.py` | 固定种子样本/规范/统计量生成 |

## 本次实际验证结果

验证环境：

```text
Python 3.11.18
numpy 2.0.2
pyarrow 17.0.0
pytest 8.4.2
```

命令结果：

```text
$ pytest -q
38 passed

$ python -m ctr_cvr_joint verify --out artifacts/demo
business_code = OK
all_migrations_replay = true
same_input_all_nodes_reused = true
threshold_only_scoring_recomputed = true
statistics_update_targeted = true
label_correction_reuses_scores = true
feature_spec_upgrade_all_downstream = true
incremental_equals_full = true
current_trace_replays = true
expected_business_codes = true
self_checks_pass = true
```

关键演示指标（12 条样本）：

```text
同输入重跑：
  assembly      recomputed=0, reused=12
  scoring       recomputed=0, reused=12
  accumulation  recomputed=0, reused=12

仅门限策略变化：
  assembly      recomputed=0, reused=12
  scoring       recomputed=12, reused=0
  accumulation  recomputed=0, reused=12

统计量仅更新 city=shenzhen：
  assembly      recomputed=0, reused=12
  scoring       recomputed=2, reused=10
  accumulation  recomputed=2, reused=10

标签修正 2 条：
  assembly      recomputed=0, reused=12
  scoring       recomputed=0, reused=12
  accumulation  recomputed=2, reused=10

特征规范 v3 修订：
  assembly      recomputed=12
  scoring       recomputed=12
  accumulation  recomputed=12
```

四类增量场景的 `matrix_sha256`、`score_hash`、`accumulation checksum` 均与独立全新工作区的全量重算一致。演示还验证了：

```text
E_LINEAGE_BROKEN  旧门限 trace / 旧规范 trace
E_SNAPSHOT_ROLLBACK
E_CACHE_CORRUPT   缓存与快照篡改
E_STAGE_BUSY
E_RESOURCE_LIMIT
E_THRESHOLD_INVALID
E_TOLERANCE_INVALID
E_DUP_REQUEST
E_VERSION_UNSUPPORTED
E_FORMAT_INVALID
```

## 设计边界

- 这是本地领域组件与演示，不包含线上服务、账号、网络调用或容器；
- 打分公式是显式、可审计的演示口径，不声称对应任何线上排序模型；
- Parquet 中变长特征映射保存为规范 JSON 字符串列，保证 schema 稳定和往返无损；
- 报告中的平台、路径和快照版本属于运行元数据；核心装配矩阵、分数、贡献和累积哈希才是确定性比较对象。
