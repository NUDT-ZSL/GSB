# 本地 LLM 指令微调数据构造编辑器

这是一个从零实现的、可交互的六阶段数据构造工具，重点验证：

- 并行 worker、候选比较次数、驻留内存三重预算准入；
- 去重候选窗口、采样质量档与总配比之间的显式裁决；
- 强制终止后从最后一个已提交阶段一致恢复；
- v1 单文件策略与 v2 分片 JSON + SQLite 策略的互迁和单写者提交。

所有原料由固定种子脚本在本地生成，不访问网络。

## 环境

```bash
python3 --version          # 需要 >= 3.10
python3 -m pip install numpy
```

运行时代码主要使用 Python 标准库；`numpy` 按任务环境要求安装。

## 快速开始

```bash
# 1. 生成固定种子数据、模板、规则、配额和两代策略
python3 generate_data.py --out data --force

# 2. 运行完整六阶段
python3 -m ifdata run \
  --data data \
  --state .ifdata-state \
  --out export

# 3. 查看状态或某个已提交阶段
python3 -m ifdata status --data data --state .ifdata-state --out export
python3 -m ifdata show-stage dedup --data data --state .ifdata-state --out export
```

也可以安装为命令：

```bash
python3 -m pip install -e .
ifdata generate --out data --force
ifdata run --data data
```

## 交互式编辑器

```bash
python3 -m ifdata repl \
  --data data \
  --state .ifdata-state \
  --out export
```

可用命令：

| 命令 | 作用 |
|---|---|
| `status` | 查看策略、运行指纹和快照链 |
| `run` / `resume` | 运行或从最后提交阶段恢复 |
| `stage <name>` | 查看 ingest/template/quality/dedup/sampling/export 快照 |
| `set workers <n>` | 调整本次会话的 worker 上限请求 |
| `set compare <n>` | 调整本次会话的比较预算 |
| `set memory <n>` | 调整本次会话的驻留内存预算 |
| `budget` / `unset` | 查看或清空临时预算覆盖 |
| `quit` | 退出 |

## 数据与产物

```text
data/
  raw_samples.jsonl          # 原始 JSONL，字段完整
  templates.json             # 模板库
  cleaning_rules.json        # 清洗评分规则
  quota_targets.json         # source_tag × quality_tier 配额
  generation_manifest.json   # 固定种子产物的校验和
  strategy/
    v1/strategy.json         # v1 单文件快照
    v2/
      strategy.sqlite        # v2 活跃指针、历史、提交锁
      manifest.json          # 当前活跃分片清单
      active.json            # 便于审计的活跃指针副本
      generations/.../shards/*.json

export/
  dataset.jsonl              # 训练数据，按声明顺序导出
  export_manifest.json       # 行顺序、配比达成、校验和

.ifdata-state/
  run.json                   # 策略与输入指纹
  events.jsonl               # 阶段开始、提交、终止、失败事件
  stages/01-ingest.json ...  # 六个阶段快照
```

每条导出记录包含 `trace`，可定位原料行、原料对象校验和、模板、质量贡献、
去重簇、采样目标、采样种子、档位放宽情况与导出行校验和。

## 六阶段

1. **ingest**：按 `sample_id` 升序校验并归一化；重复 ID 返回
   `E_DUP_SAMPLE`，字段/受控集合/修订号错误返回 `E_BAD_FIELD`。
2. **template**：按 `lang + source_tag` 选模板，展开并归一化
   `instruction/input`；缺少必需或未知占位符返回
   `E_TEMPLATE_PLACEHOLDER`。
3. **quality**：长度、重复率、噪声字符比例分别计算分量并记录
   `weight / component_score / contribution / metrics`。
4. **dedup**：MinHash 分块签名 + 候选窗口 + 门限聚类；保留质量分最高者，
   同分按 `sample_id`；被剔除样本记录邻居、命中块和阈值比。
5. **sampling**：固定种子分层配额采样，优先总量，允许受限欠配；
   每个目标记录实际达成量、欠配原因和种子。
6. **export**：按目标声明顺序原子写 JSONL 与清单，逐行可追溯。

详细公式和状态语义见 [docs/DESIGN.md](docs/DESIGN.md)。

## 关键命令

```bash
# 三重预算检查
python3 -m ifdata budget-check \
  --workers 5 --comparisons 200 --memory 5000000 \
  --max-workers 4 --compare-budget 100 --memory-limit 4000000

# 低比较预算：超限窗口会被拒绝并记录裁决
python3 -m ifdata run ... --compare-budget 200

# 低内存：worker 数自动降级并记录 E_MEMORY_LIMIT
python3 -m ifdata run ... --memory-limit 600000

# 在 quality 计算完成后、提交前模拟强制终止
python3 -m ifdata run ... --fail-stage quality

# 从最后提交阶段恢复，已提交的 ingest/template 不重算
python3 -m ifdata resume ...

# 注入 worker 异常，返回阶段和批次归因
python3 -m ifdata run ... --failpoint quality:1=worker-boom

# 策略迁移
python3 -m ifdata migrate-v1-to-v2 \
  --input data/strategy/v1/strategy.json \
  --output-store /tmp/strategy-v2
python3 -m ifdata migrate-v2-to-v1 \
  --input-store data/strategy/v2 \
  --output /tmp/strategy-v1.json
```

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_DUP_SAMPLE` | 重复 `sample_id`，所有重复行均拒绝 |
| `E_BAD_FIELD` | 必填字段、类型、长度、受控集合或修订号非法 |
| `E_TEMPLATE_PLACEHOLDER` | 模板缺少必需占位符或引用了未知占位符 |
| `E_TEMPLATE_NOT_FOUND` | 无匹配模板 |
| `E_CONCURRENCY_LIMIT` | 请求 worker 数超过硬上限 |
| `E_COMPARE_BUDGET` | 候选比较代价超过总预算 |
| `E_MEMORY_LIMIT` | 驻留样本内存估算超过上限 |
| `E_STAGE_BUSY` | 策略快照/状态锁被其他提交者占用 |
| `E_STALE_VERSION` | 期望或传入策略版本落后 |
| `E_SNAPSHOT_CORRUPT` | 校验和、分片、链关系或运行指纹不一致 |
| `E_WORKER_FAILURE` | worker 异常，附带阶段和批次 |
| `E_FORCED_TERMINATION` | 提交前注入强制终止；已提交状态保留 |

预算同时越界时，按策略快照的 `runtime.budget_error_priority` 选择返回码。
默认顺序为并发、比较预算、内存。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前 27 个测试全部通过，覆盖摄入错误码、占位符、质量贡献、去重簇、
三重预算、worker 异常归因、快照链、强制终止恢复、输入指纹变化、
v1/v2 迁移、分片篡改、整体替换和并发提交冲突。

实际验收数据、校验和与冲突裁决结果见
[docs/VERIFICATION.md](docs/VERIFICATION.md)，原始证据位于
`artifacts/verification/`。
