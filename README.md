# 跨文档事件共指链本地诊断工作台

这是一个从零构建、完全本地运行的跨文档事件共指链可视化与诊断工作台。样本由固定种子、受控词表和实体词典确定性生成，不依赖真实数据集、外部服务或容器环境。

核心能力：

- 确定性文档生成、码点偏移解析与局部事件抽取。
- 四分项相似度、候选对生成与同分稳定排序。
- 新文档增量合并，未受影响链直接复用既有状态。
- 同一事件候选进入多条链时的链间仲裁。
- 链内论元矛盾裁决与落选证据留存。
- 增量结果对全量重建的一致性自检。
- 内容寻址缓存、三类失效传播与后缀重算。
- 链状态、可信度表、仲裁记录的版本化整体快照。
- 资源上限、证据索引降级与非阻塞并发提交锁。
- Flask + 原生 SVG 的本地诊断页面。

## 快速启动

### Windows

建议使用 Python 3.11 或更高版本：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

浏览器打开 `http://127.0.0.1:5000`。服务只绑定回环地址，不配置外网依赖。

如果本机没有 3.11，可使用已安装的 Flask 和 NumPy，直接执行 `python app.py`。代码保持 Python 3.10 兼容。

### Linux / WSL

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

可通过环境变量修改端口：

```bash
EVENTGRAPH_PORT=5100 python app.py
```

启动时会清空并重建 `runtime/` 下的缓存、快照和错误诊断目录，然后自动提交 D01–D03 作为基线。该目录必须可写。

## 演示流程

页面提供以下操作：

1. **冲突仲裁演示**：提交 D04。D04 同时与链 A、链 B 存在候选证据，按优先级选择链 A，并记录链 B 的拒绝方向。
2. **降级合并演示**：补齐 D01–D06 后，以比较预算 3 提交 D07。证据上界剪枝后只精算 1 对，类别为 `HIGHER_SIMILARITY_FLOOR`。
3. **修订 D06 并增量重算**：修正 D06 句子模板，只从 D06 后缀开始重算；链 A、链 B 复用，链 C 重算。
4. **回退 / 损坏 / 并发探针**：分别返回 `E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CORRUPT`、`E_STAGE_BUSY`。
5. **重置**：清空运行时状态并恢复 D01–D03 基线。

## 项目结构

```text
app.py                         Flask 入口与 JSON API
eventgraph/
  generator.py                 固定种子、场景清单、文档拼装与修订
  parser.py                    码点偏移、实体提及、触发词、论元抽取
  similarity.py                四分项打分、贡献明细、候选排序
  engine.py                    增量链、仲裁、自检、全量重建
  cache.py                     内容哈希对象与依赖键指针
  snapshots.py                 三类版本化整体快照
  service.py                  事务、失效传播、并发锁、诊断状态
  config.py                    配置与字段级校验
data/
  vocab.json                   受控模板、触发词、实体、时间、角色
  trust_table.json             来源可信度表
  generated/                   确定性文档与 manifest
config/weights.json            相似度权重与版本
templates/dashboard.html       工作台页面
static/                        原生 CSS / JavaScript / SVG
tests/test_workbench.py        18 项自动化测试
scripts/run_validation.py      运行验证并生成报告
reports/run_report.md          实际运行报告
runtime/                       缓存、快照、错误与修订持久化
```

## 确定性数据

固定种子为 `eventgraph-2026`。生成器使用 SHA-256 派生的稳定随机选择，不依赖 Python `random` 的实现细节。

七篇文档覆盖：

- D01/D02：同一台风事件的两个来源描述。
- D03：另一现实事件，形成独立链 B。
- D04：共享证据同时命中链 A 与链 B 的桥接事件。
- D05：继续归入链 A 的 corroboration 文档。
- D06/D07：独立洪涝链，并用于资源降级演示。

文档文件、内容摘要、清单摘要均记录在 `data/generated/manifest.json`。

## 局部事件模型

每个局部事件包含：

- `document_id`、`doc_index`、`local_id`、`event_uid`。
- 触发词 `surface`、`lemma`、`start`、`end`。
- 论元 `role`、`kind`、`value`、实体或时间 ID、`start`、`end`。
- 所有偏移单位显式标记为 `unicode_code_point`。

解析器规则：

- 相同文档内 `local_id` 必须唯一。
- 偏移必须满足 `0 <= start < end <= len(content)`。
- 触发词文本必须与偏移切片一致。
- 同一触发词区间不得被多个事件占用。
- 事件按触发词起点、局部编号稳定排序。
- 偏移越界、文本不匹配或触发词重复占用统一返回 `E_SPAN_INVALID`，并标注文档与事件编号。

## 相似度与候选对

相似度固定由以下四分项组成：

```text
total =
  0.38 * entity_jaccard
  + 0.24 * shared_time
  + 0.23 * trigger_form_similarity
  + 0.15 * average_source_trust
```

每个分项保存 `weight`、`raw`、`contribution` 和 `evidence`。总分严格按
`round(sum(contribution), 6)` 重算。

候选规则：

- 仅跨文档生成候选。
- 只保留总分不低于当前有效下限的候选。
- 同分时按文档序号、局部事件编号升序。
- 下限非法或权重为负返回 `E_CONFIG_INVALID`，并指出字段。

## 增量合并

新文档到达时：

1. 解析新文档并生成其局部事件。
2. 只将新事件与既有事件进行候选扫描。
3. 将候选伙伴映射到既有链。
4. 对每个新增事件选择一条目标链，或创建单例链。
5. 未出现在候选关联中的链不修改成员和论元状态，链内容哈希原样复用。

每个局部事件通过 `membership` 恰好归入一条链。`validate_membership` 检查：

- 同一链内重复成员：`E_CHAIN_DUP_MEMBER`。
- 同一事件跨多条链：`E_CHAIN_DUP_MEMBER`。
- 事件未归入任何链：`E_CHAIN_MISSING`。

## 链间仲裁

若一个新事件被候选并入多条既有链，优先级为：

```text
链证据强度 desc
→ 链规模 desc
→ 首次出现顺序 asc
→ 链编号字典序 asc
```

仲裁记录保存：

- 每条候选链的证据强度、规模和首次出现位置。
- 胜出链。
- 每个被拒绝方向及具体理由。
- 所用优先级和内容校验和。

## 链内论元裁决

同一角色出现多个取值时，每个成员事件贡献：

```text
contribution = source_trust * role_weight
```

胜出顺序：

```text
聚合支持度 desc
→ 最大来源可信度 desc
```

若前两名支持度和最大可信度完全相同，返回 `E_ARBITRATION_UNRESOLVED`，附带争用值、支持度、可信度和成员明细。胜出值与全部落选值、成员证据均保留在链状态中。

## 一致性自检

每次成功提交后自动执行：

1. 从空状态按相同文档顺序全量重建共指链。
2. 比较每条链的成员集合。
3. 比较论元胜出值和链间仲裁结论。
4. 重算候选相似度分项与总分。
5. 检查每个事件的候选扫描清单。
6. 检查候选证据哈希和链链接引用。

任何缺失、证据不复算或结果偏差返回 `E_INCREMENTAL_DRIFT`，并定位文档、链和事件。

## 内容寻址缓存

缓存对象按 SHA-256 内容哈希落盘：

```text
runtime/cache/objects/<hash>.json
runtime/cache/keys/<kind>/<key-prefix>/<key>.json
```

三类产物：

- `local_events`
- `similarity`
- `chain_state`

键材料显式包含：

- 文档内容摘要与上游产物哈希。
- 受控词表版本。
- 相似度权重版本。
- 可信度版本与内容摘要。
- 解析器版本、候选下限、事件哈希、候选哈希等上游依赖。

### 失效传播

支持三类变更：

- 修正文档句子或模板。
- 调整相似度下限。
- 更新来源可信度表。

系统先计算受影响的候选对与链，再从最早受影响文档的后缀开始重算。后缀之前的检查点直接恢复，不重新生成前缀链。输出：

- 受影响候选对。
- 受影响链。
- 重算文档与复用文档。
- 重算链与复用链。

## 版本化快照

以下三类快照独立维护：

- `chains`：链成员、论元胜出值、链产物哈希。
- `trust_table`：可信度版本与来源表。
- `arbitration`：全部链间与论元仲裁记录。

每个快照包含：

- 单调递增版本号。
- 内容 SHA-256 校验和。
- 完整 payload。
- 历史版本文件与 `current.json` 全量替换。

规则：

- 旧版本覆盖新版本：`E_SNAPSHOT_ROLLBACK`。
- 同版本但校验和变化：拒绝修补，要求递增版本后整体替换。
- 文件损坏或校验和不符：`E_SNAPSHOT_CORRUPT`。
- 错误详情包含快照类别与版本号。

## 资源与并发

配置项：

- `max_doc_events`：单文档事件数。
- `max_candidate_comparisons`：阶段候选精算次数。
- `max_chain_memory_bytes`：链状态与成员映射的规范 JSON 字节估算。

事件数或内存超限返回 `E_RESOURCE_LIMIT`。

候选比较超预算时：

1. 将有效下限提升至 `degrade_floor`。
2. 用实体/时间/最大触发词/来源可信度构造安全上界。
3. 上界低于下限的候选只记录 `pruned`，不精算。
4. 所有事件与所有候选伙伴均保留扫描记录。
5. 精算数仍超限则返回 `E_RESOURCE_LIMIT`。
6. 成功时记录降级类别与计划/实际比较数。

增量提交使用非阻塞互斥锁。已有提交生效时，后到请求立即返回 `E_STAGE_BUSY`，不排队等待。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 诊断工作台 |
| GET | `/healthz` | 健康检查 |
| GET | `/api/state` | 完整诊断状态 |
| POST | `/api/commit-next` | 提交下一篇待处理文档 |
| POST | `/api/demo/conflict` | 冲突仲裁演示 |
| POST | `/api/demo/degrade` | 资源降级演示 |
| POST | `/api/demo/invalidate` | 修订/阈值/可信度失效重算 |
| POST | `/api/probes/rollback` | 快照回退探针 |
| POST | `/api/probes/corruption` | 快照损坏探针 |
| POST | `/api/probes/stage-busy` | 并发占用探针 |
| POST | `/api/reset` | 清空运行时并恢复基线 |

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `E_SPAN_INVALID` | 偏移越界、触发词失配或重复占用 |
| `E_CONFIG_INVALID` | 下限、权重、可信度或资源字段非法 |
| `E_CHAIN_DUP_MEMBER` | 事件重复或跨链重复归属 |
| `E_CHAIN_MISSING` | 事件未归属任何链 |
| `E_ARBITRATION_UNRESOLVED` | 论元争用无法按证据裁决 |
| `E_INCREMENTAL_DRIFT` | 增量结果、扫描或证据与全量重算不一致 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本覆盖新快照 |
| `E_SNAPSHOT_CORRUPT` | 快照内容或校验和损坏 |
| `E_RESOURCE_LIMIT` | 资源上限超限 |
| `E_STAGE_BUSY` | 已有增量提交正在生效 |

## 测试与运行报告

```bash
python3 scripts/generate_samples.py
python3 -m unittest discover -s tests -v
python3 scripts/run_validation.py
```

实际结果见：

- `reports/run_report.md`
- `reports/validation_summary.json`

本次验证环境为 Python 3.10.12、Flask 3.0.3、NumPy 1.26.4。任务建议 Python 3.11+，当前容器版本略低；实现保持 3.10 兼容，并已在当前环境完成 18 项测试。
