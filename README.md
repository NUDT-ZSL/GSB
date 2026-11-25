# Model Workbench

离线模型热更新、灰度诊断与强制终止恢复工作台。项目只依赖 Python 3.10+、
Flask 和标准库 `sqlite3`，不访问真实模型仓库、外部推理服务或在线更新源。

## Quick Start

```bash
python3 -m pip install -r requirements.txt
python3 -m modelwb.seed --out samples --force

python3 -m modelwb --db var/workbench.db load --input-dir samples
python3 -m modelwb --db var/workbench.db status --compact --pretty

# 比较增量补丁与独立整体替换清单
python3 -m modelwb --db var/workbench.db verify \
  --patch samples/patch_v1_to_v2.json \
  --full samples/full_replacement/classifier-v2.json

# 提交补丁，按 25%、75%、100% 放量
python3 -m modelwb --db var/workbench.db patch \
  --patch samples/patch_v1_to_v2.json \
  --full samples/full_replacement/classifier-v2.json
python3 -m modelwb --db var/workbench.db gray \
  --target-version classifier-v2 --stages 25,75,100

# 开始请求后执行回退；已绑定请求保持原快照
python3 -m modelwb --db var/workbench.db request --request-id req-001
python3 -m modelwb --db var/workbench.db rollback \
  --reason observed_regression --trigger monitor
```

也可以使用根目录包装脚本：`python3 cli.py --db var/workbench.db ...` 和
`python3 seed.py --out samples`。

## 功能

- 合并并校验 v1 单文件 JSON 与 v2 分片 JSON + SQLite 元数据。
- 将 v1 相对时间按 `base_ms + relative * scale_ms` 换算为绝对毫秒，使用
  `Decimal` 和显式舍入模式，保留换算标度、舍入方向与规范化字节。
- 同一版本按 `(logical_clock, generation, source_priority, update_time)` 裁决；
  逻辑时钟和代数相同时 v2 优先于 v1。
- 增量补丁仅覆盖声明分片，校验未声明分片未变化，并逐层逐分片定位首个分歧。
- 沿模型、分片、配置、特征表、上游模型引用图计算缓存失效闭包；未命中闭包的
  缓存和分片链保持原值。
- 预热状态机：`pending -> loading -> warmed -> gray -> rollback -> warmed`。
- 每个请求绑定开始时的版本、快照、分片摘要链、配置组合与补丁链。
- 每档灰度提交都会先校验目标分片健康、目标快照指针和预热完成状态。
- 回退按持久化档位计划倒序执行，记录触发来源、当前档位和已进入灰度副本。
- 补丁、灰度档位和回退步骤均可用 `resume` 从 SQLite 已提交状态续跑。
- CLI 与 Flask API 共用服务层；错误码、业务码和诊断字段一致。

## Commands

| Command | Purpose |
| --- | --- |
| `load` | 加载、校验并合并两代清单，初始化副本和缓存 |
| `patch` | 提交增量补丁并建立目标快照 |
| `gray` | 预热候选副本并逐档推进灰度 |
| `rollback` | 按档位倒序回退并恢复基线 |
| `resume` | 重放未提交操作或缺失的灰度/回退步骤 |
| `status` | 查看当前快照、发布、副本、缓存和操作状态 |
| `verify` | 比较增量补丁与整体替换的分片目录摘要 |
| `versions` | 查看合并后的统一版本视图 |
| `request` | 创建或结束稳定的请求版本绑定 |
| `operation` | 查看单个幂等操作及持久化步骤 |

`--db` 默认取 `MODELWB_DB`，否则为 `var/workbench.db`。命令详情：

```bash
python3 -m modelwb --help
python3 -m modelwb load --help
```

固定种子样本使用 `python3 -m modelwb.seed --out samples --force` 生成。
`samples/MANIFEST.json` 记录全部样本文件的 SHA-256。

## Flask API

```bash
MODELWB_DB=var/workbench.db python3 app.py
```

主要接口：

```text
GET  /health
GET  /api/status
GET  /api/versions
GET  /api/caches
POST /api/load
POST /api/patch
POST /api/gray
POST /api/rollback
POST /api/resume
POST /api/verify
POST /api/requests
GET  /api/requests/<request_id>
POST /api/requests/<request_id>/finish
GET  /api/operations/<op_id>
```

请求体字段与对应 CLI 参数同名，例如：

```bash
curl -s -X POST http://127.0.0.1:5000/api/load \
  -H 'Content-Type: application/json' \
  -d '{"input_dir":"samples","baseline_version":"classifier-v1"}'
```

## Error Codes

每个错误返回 `code`、`business_code`、`message` 和 `diagnostics`。

| Business code | Code | Meaning |
| --- | --- | --- |
| `MWB-1001` | `SHARD_MISSING` | 声明分片缺失或索引集合不完整 |
| `MWB-1002` | `SHARD_DIGEST_MISMATCH` | 分片、分片 JSON 或字节范围摘要不匹配 |
| `MWB-1003` | `LAYER_REFERENCE_MISSING` | 补丁或清单引用了不存在的层 |
| `MWB-1004` | `CONFIG_REFERENCE_CYCLE` | 配置引用图成环 |
| `MWB-1005` | `GRAY_RATIO_INVALID` | 灰度比例越界、重复或非递增 |
| `MWB-1006` | `CLOCK_ROLLBACK` | 逻辑时钟回拨超过容差 |
| `MWB-1201` | `PATCH_FULL_REPLACEMENT_MISMATCH` | 增量结果与整体替换不一致 |
| `MWB-1202` | `PATCH_UNDECLARED_SHARD_CHANGE` | 整体替换修改了补丁未声明的分片 |
| `MWB-1203` | `PATCH_DIGEST_MISMATCH` | 补丁自身规范化摘要不匹配 |
| `MWB-1301` | `INVALID_STATE` | 当前持久化状态不允许该转换 |
| `MWB-1404` | `NOT_FOUND` | 版本、快照、操作或请求不存在 |

## Crash Recovery

测试和诊断可设置：

```bash
MODELWB_CRASH_AT=patch_before_commit
MODELWB_CRASH_AT=gray_after_stage:2
MODELWB_CRASH_AT=rollback_after_step:1
```

命中注入点时进程以退出码 `86` 直接终止，不执行清理。随后运行：

```bash
python3 -m modelwb --db var/workbench.db resume
```

恢复只完成未提交步骤；已提交快照、阶段和回退计划不会重写。

## Verification

```bash
python3 -m unittest discover -v
```

当前实际结果：

```text
Ran 17 tests in 3.847s
OK
```

覆盖两代合并、相对时间换算、墓碑、增量/整体摘要一致、未声明分片、缓存闭包、
预热失败隔离、灰度请求绑定、回退、六类业务错误码、Flask API、固定种子可复现性，
以及补丁、灰度档位、回退步骤被强制终止后的恢复一致性。

固定种子复现核对：

```text
SHA-256 samples/MANIFEST.json
= ed354e9028c6da21a395a39b756fcd17a971d1a04e59a8cd0f5a6405624b0921
```

完整 CLI 演练的实际结论：

```text
merged active versions = shared-model-v1, classifier-v1, ranker-v1,
                         classifier-v2, aux-v1
incremental digest      = ad213fccea239910c45da58ce83f7616f2d4c48475a860563278d40e194c20f6
full replacement digest = ad213fccea239910c45da58ce83f7616f2d4c48475a860563278d40e194c20f6
invalidated caches      = replica-a/b and transitive ranker replica-d
reused caches           = replica-c
gray stages             = 25%(replica-a), 75%(a,b), 100%(a,b)
in-flight request       = classifier-v2 after rollback
final snapshot          = classifier-v1, release=rolled_back
```

详细设计、schema 和事务不变量见 [TECHNICAL.md](TECHNICAL.md)。
