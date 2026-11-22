# 语料去重诊断工作台

一个完全本地运行的 LLM 预训练语料去重可视化与诊断工作台。项目从零生成合成语料，不读取真实语料，不访问网络服务，不使用 Docker。工作台把去重校验拆成可核查的多级流水，并同时提供 CLI、批量快照、逐文档查询和本地 Web UI。

## 能力概览

- 确定性分块签名：NFKC + casefold + token 归一化，按 `signature_length` 个 token 分块，BLAKE2b 受控哈希生成块签名，NumPy 排序生成 MinHash 文档摘要。
- 候选桶与窗口：按摘要首带分桶，桶内按 `doc_id` 排序，仅比较前 `candidate_window` 篇，窗口外绝不比较。
- 两种比较模式：`full` 比较全部对齐块；`digest_summary` 使用共同摘要带门控与摘要锚点块比较。
- 稳定阈值裁决：阈值为十进制字符串，判定使用精确分数叉积；支持 `strict` 与 `tolerance_band`，并显式处理次正规浮点数。
- 可追溯去重：质量分降序、再按 `doc_id` 升序保留代表；每个剔除项包含候选对、邻居、命中块位置、相似度和阈值留痕。
- 查询/批量一致性：逐字段比对块签名、摘要、候选轨迹、候选集合、桶划分、判定与剔除原因，偏差返回 `E_INCONSISTENT`。
- 内容寻址缓存：键包含分片内容哈希、签名长度、阈值、窗口、校验模式及摘要参数；磁盘 + 内存 LRU。
- 配置切换失效：计算受影响文档和缓存条目，整体失效相关键，并报告重算与复用集合。
- 版本化快照：分片统计与剔除清单分别维护单调版本号和 SHA-256 内容校验和，整对象替换，拒绝损坏与回退。
- 资源上限与降级：单批文档数、候选比较次数、缓存条目数均可配置，超限返回 `E_RESOURCE_LIMIT` 或标记降级类别。
- 本地可视化：块签名分布、候选桶/候选对、剔除/保留、逐文档查询、延迟分解、快照、缓存、错误码与模式差异。

## 目录

```text
.
├── config/dedup_config.json          # 固定样本配置
├── data/manifest.jsonl               # 72 篇确定性合成文档
├── data/sample_meta.json             # 生成规模、种子、文档族元数据
├── docs/VALIDATION.md                # 本次实际验证记录
├── src/corpus_dedup_workbench/
│   ├── signatures.py                 # 归一化、块签名、MinHash 摘要
│   ├── pipeline.py                   # 桶、窗口、比较、去重与统计
│   ├── threshold.py                  # 稳定阈值裁决与次正规数保护
│   ├── cache.py                      # 内容寻址 LRU 缓存
│   ├── snapshots.py                  # 版本化不可变快照
│   ├── service.py                    # 批量、查询、一致性、失效、并发
│   ├── web.py                        # 标准库 ThreadingHTTPServer
│   └── webui/                        # 无外部依赖的 HTML/CSS/JS 工作台
└── tests/                            # 34 项单元与集成测试
```

## Windows 本机安装

要求 Python 3.11 或更高版本。以下使用 PowerShell：

```powershell
cd <项目目录>
py -3.11 --version
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

如果尚未安装 Python 3.11，可执行：

```powershell
winget install -e --id Python.Python.3.11
```

也可直接使用完整解释器路径，无需激活虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m corpus_dedup_workbench status
```

本项目只依赖 Python 标准库和 NumPy；不需要 GPU、数据库、网络服务或容器。

## Linux / WSL 安装

本次实测环境使用 Python 3.11.18：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

## 固定样本规格

样本由 `numpy.random.Generator(numpy.random.PCG64(20261123))` 在本地生成：

| 参数 | 值 |
|---|---:|
| 随机种子 | `20261123` |
| 分片数 | 4 |
| 每分片文档数 | 18 |
| 文档总数 | 72 |
| 每文档 token 数 | 192 |
| 分块签名长度 | 12 tokens |
| 每文档块数 | 16 |
| 总块签名字数 | 1152 |
| 摘要带数 × 每带值数 | 4 × 2 |
| 候选窗口 | 4 |
| 相似度阈值 | 0.75 |
| 默认校验模式 | full |
| 阈值策略 | tolerance_band |
| 查询延迟预算 | 50 ms |
| 单批文档上限 | 256 |
| 候选比较上限 | 5000 |
| 缓存条目上限 | 128 |

每分片包含 3 个近重复文档族（每族 4 篇）和 6 篇独立文档。文档族包含完全相同、单块变化、三块变化、四块变化（相似度恰为 `12/16 = 0.75`）的样本，用于门限、代表选择、摘要冲突和模式差异诊断。全部 token 和文本均为合成数据。

重新生成完全相同样本：

```bash
python -m corpus_dedup_workbench generate \
  --seed 20261123 --shards 4 --docs-per-shard 18 \
  --out data --config config/dedup_config.json
```

## 命令行入口

以下命令在 Windows 中将 `python` 替换为 `.\.venv\Scripts\python.exe`。

```bash
# 批量全量处理，保存完整结果
python -m corpus_dedup_workbench batch \
  --data data/manifest.jsonl \
  --config config/dedup_config.json \
  --out artifacts/batch_result.json --summary

# 按 doc_id 查询，实际耗时超过预算时返回 E_LATENCY_BUDGET
python -m corpus_dedup_workbench query --doc-id doc-000000 --budget-ms 50

# 比较 full 与 digest_summary 的剔除集合、命中位置和归因
python -m corpus_dedup_workbench compare-modes

# 对全部 72 篇文档逐篇校验查询侧与批量侧一致性
python -m corpus_dedup_workbench consistency

# 同配置重跑，观察内容寻址缓存复用
python -m corpus_dedup_workbench refresh

# 端到端自检并写入报告
python -m corpus_dedup_workbench selftest --out artifacts/selftest_report.json

# 启动可视化工作台，默认只监听本机回环地址
python -m corpus_dedup_workbench serve --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765`。工作台支持修改配置并观察失效范围、同配置缓存重跑、模式差异、全量一致性校验和逐文档查询。

## 数据与算法约定

### 输入

每行 JSONL 必填：

```json
{
  "shard_id": "shard-00",
  "doc_id": "doc-000000",
  "text": "synthetic tokens ...",
  "lang": "en",
  "source": "synthetic/numpy-pcg64",
  "token_estimate": 192
}
```

样本额外包含 `quality_score`。缺少该字段时，系统从文本 SHA-256 派生 `[0, 100)` 的确定性质量分。去重仅在分片内进行，分片级统计和快照一一对应。

### 分块签名

1. 对文本做 NFKC、casefold，提取 Unicode 字母数字 token，忽略下划线与标点。
2. 每 `signature_length` 个 token 形成一个块；最后不足一块但非空的尾块保留。
3. 块签名是 `BLAKE2b(domain || signature_length || block_text, digest_size=8)` 的 16 位十六进制串。
4. 文档摘要取全部块签名整数中排序最小的 `summary_bands * values_per_band` 个值，每 `values_per_band` 个值组成一带。
5. 候选桶键为 `band0:<第一带>`；只有同桶文档才产生候选对。

`signature_length <= 0` 返回 `E_SIG_LEN_INVALID`；归一化后 token 数不足一个完整块返回 `E_DOC_TOO_SHORT`。

### 候选与相似度

- 桶内按 `doc_id` 升序排列；第 `i` 篇只与前 `candidate_window` 篇形成候选，记录 `window_distance`。
- 每对候选必须记录 `summary_relation = same|conflict`、共同摘要带、命中块位置、相似度分数、精确分数与阈值留痕。
- `full`：相似度 = 相同位置块签名数 / 两文档最大块数。
- `digest_summary`：先要求共同摘要带数达到 `digest_min_common_bands`；不足时记为 `summary_conflict_omission`。满足门控后，只比较摘要锚点块，相似度 = 共同锚点签名数 / 两文档最大锚点数。
- 无候选的桶保留为 `empty_candidates=true` 的记录，候选总数可由各桶 `candidate_count` 求和复核。

### 阈值稳定性

阈值和容差按十进制字符串保存。相似度以 `Fraction(matched, denominator)` 参与判定，使用整数叉积比较，不经过二进制浮点舍入。`tolerance_band` 仅用于标记和留痕，容差带内仍使用同一精确叉积裁决。若分数落入次正规区间，则显式钳制为 0 并记录 `subnormal_guarded=true`，避免平台舍入翻转。

### 代表选择

所有近重复边在分片内按“质量分降序、`doc_id` 升序”贪心选代表。任何被剔除项都记录最佳保留邻居、候选 ID、桶键、双方摘要键、命中位置、相似度、阈值策略与比较模式。

## 缓存与失效

每个分片的结果工件写入内容寻址缓存：

```text
shard:<sha256(canonical({
  shard_id,
  shard_content_hash,
  signature_length,
  similarity_threshold,
  candidate_window,
  validation_mode,
  threshold_policy,
  threshold_tolerance,
  summary_bands,
  values_per_band,
  digest_min_common_bands,
  normalization_version,
  max_candidate_comparisons,
  resource_policy
}))>
```

缓存内容同时包含签名产物、桶、候选对、剔除与统计，写入前后均校验内容 SHA-256。键相同复用，键不同必然重算。配置切换时：

1. 依据变化字段计算语义受影响文档：
   - 签名/摘要参数变化：全部文档；
   - 阈值/模式/容差变化：所有有候选的文档；
   - 窗口变化：大小大于 1 的桶内全部文档；
   - 资源/预算变化：全部文档。
2. 记录当前全部内容寻址条目，使旧键失效。
3. 输出 `affected_docs`、`affected_cache_entries`、`invalidated_entries`、`recomputed_docs`、`reused_docs`。

同配置 `refresh` 可直接复用 4 个分片工件；阈值、窗口或模式变化会产生新键并整体重算相关分片。

候选比较上限按单批总量执行：分片按固定顺序消耗剩余额度，超限时 `resource_policy=error` 返回 `E_RESOURCE_LIMIT`，`degrade` 则截断后续候选并同时保留 `generated_candidate_pairs` 与实际比较数，便于复核。

## 快照与并发

- 快照类型：`shard_statistics`、`removed_list`。
- 每次物化批量结果都生成新版本；版本只增不减。
- 内容校验和是规范化 JSON 载荷的 SHA-256。
- 发布时深拷贝载荷并整对象替换，不做增量修补。
- 同版本覆盖、版本回退返回 `E_SNAPSHOT_ROLLBACK`；载荷或校验和不符返回 `E_SNAPSHOT_CORRUPT`。
- `ThreadingHTTPServer` 支持并发请求；查询先获取不可变运行时引用，配置切换构建新运行时后一次替换，旧查询不会被部分新快照污染。

## 延迟预算

查询计时覆盖处理器内的实际查询循环：批量记录查找、独立签名重算、候选轨迹重建和一致性比对。`estimated_ms` 按签名块数与候选比较次数给出，仅用于诊断，不替代实测。请求预算会被配置预算封顶，不能通过传更大的 `budget_ms` 放大预算。超预算返回 `E_LATENCY_BUDGET`，附带已完成阶段、预算、实测耗时、估算依据，且 `candidate_comparisons_skipped=0`。

## Web API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 配置、快照、缓存、限额、降级、最近延迟/错误 |
| GET | `/api/dashboard` | 统计、签名直方图、桶、候选、剔除、保留、文档索引 |
| GET | `/api/query?doc_id=...&budget_ms=...` | 逐文档查询 |
| GET | `/api/compare-modes` | 模式差异与归因 |
| GET | `/api/consistency` | 全量文档查询/批量一致性校验 |
| GET | `/api/snapshot?kind=...` | 读取并验证快照 |
| POST | `/api/config` | 部分配置更新、失效与重算 |
| POST | `/api/refresh` | 同配置缓存重跑 |

业务错误统一返回 JSON 错误信封；HTTP 层成功接收请求时业务错误仍使用 HTTP 200，便于前端显示业务码。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_SIG_LEN_INVALID` | 分块签名长度非正 |
| `E_DOC_TOO_SHORT` | 文档不足一个签名块 |
| `E_LATENCY_BUDGET` | 查询实测超过预算 |
| `E_INCONSISTENT` | 查询侧与批量侧首个字段偏差 |
| `E_SNAPSHOT_CORRUPT` | 快照缺失、损坏或校验和不符 |
| `E_SNAPSHOT_ROLLBACK` | 同版本覆盖或版本回退 |
| `E_RESOURCE_LIMIT` | 文档数或候选比较次数超限 |
| `E_CONFIG_INVALID` | 配置或 HTTP JSON 无效 |
| `E_DOC_NOT_FOUND` | `doc_id` 不存在 |
| `E_DATA_INVALID` | JSONL 缺失字段、重复 `doc_id` 或解析失败 |
| `E_CACHE_CORRUPT` | 缓存内容校验失败 |

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

覆盖 34 项测试：签名确定性、长度错误、文档过短、阈值精确/容差/次正规数、窗口边界、空候选桶、桶/候选复核、质量分平局裁决、模式差异归因、72 篇查询/批量一致性、延迟预算、`E_INCONSISTENT`、缓存键与失效、快照损坏/回退、批级资源上限/降级、并发查询与快照切换、Web 页面/API。

本次实际运行结果见 `docs/VALIDATION.md`。

## 说明与边界

- 去重范围是分片内；跨分片候选未纳入本任务模型。分片级缓存与统计因此可独立重算。
- `digest_summary` 是诊断用建模式近似，不应视为生产级语义去重；其差异被明确归因为摘要冲突遗漏或命中块减少。
- 查询延迟按进程内处理计时，不包含浏览器渲染和 HTTP 传输时间。
- 本次执行环境为 WSL2 Ubuntu 22.04；Windows 步骤使用同一套纯 Python/NumPy 代码，未使用 WSL 专属路径或系统依赖。
