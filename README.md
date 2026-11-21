# CascadeLab

本地确定性中文分词与词性标注级联应用。系统只使用本地 JSON/JSONL、词典、受控标签集和 SQLite 快照目录，不访问网络、外部服务或真实语料。

核心能力：

- 固定种子生成中文文本行、v1/v2 词典、v1/v2 标签集及异常样本。
- 四阶段流水：校验与码点化 → 词典最短路径分词 → 词性打分与选择 → 行级汇总。
- 每阶段原子提交带单调版本号、内容 SHA-256 与完整 doc_id 集合的快照。
- 进程硬终止后从最后已提交阶段恢复；未提交中间工作全部丢弃。
- 词典/标签版本变化时按文档计算失效集合，复用未受影响上游快照，整体替换阶段快照。
- 快照损坏、版本回退、并发提交、资源超限、降级与异常链均有明确业务码。
- 本地 Web 工作台与一键可重复演示。

## 环境

```text
Python 3.10.12
NumPy 1.26.4
SQLite (Python 标准库)
```

不使用 Docker、容器或外部服务。启动脚本：

```bash
python3 scripts/generate_samples.py
```

## 快速运行

完整运行：

```bash
python3 -m poscascade.cli run --fresh
```

硬终止并恢复：

```bash
python3 -m poscascade.cli run --fresh --crash-after segment
python3 -m poscascade.cli resume
```

升级到 v2 词典与收缩标签集：

```bash
python3 -m poscascade.cli upgrade
```

启动交互工作台：

```bash
python3 -m poscascade.cli serve --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。工作台可以执行完整运行、指定阶段硬终止、恢复、v2 升级、低预算降级、严格资源上限、异常链与一键对比演示。

执行完整验收演示：

```bash
python3 scripts/run_demo.py
```

演示输出位于 `runs/demo/`，核心报告为 `runs/demo/demo_report.json` 和 `runs/demo/demo_report.md`。

运行测试：

```bash
python3 -m unittest discover -s tests -t . -v
```

## 项目结构

```text
poscascade/
  validate.py       文本行校验、码点化、偏移校验
  segment.py        log 域最短路径分词、候选上限与降级
  tag.py            以分词路径分数为权重的标签打分
  summarize.py      行级与全局汇总
  store.py          SQLite WAL 快照、版本、并发与完整性校验
  pipeline.py       四阶段状态机、恢复、失效探测、代际替换
  selfcheck.py      跨模块一致性自检
  samples.py        固定种子样本生成
  state.py          工作台状态聚合
  diagnostics.py    异常链路归因报告
  demo.py           完整运行/恢复/升级/降级对比
  web.py            本地 HTTP 应用
  static/           Web 工作台
scripts/
  generate_samples.py
  run_demo.py
tests/
data/               确定性样本、词典、标签集与异常输入
runs/               本地快照与演示输出
```

## 确定性口径

### 文本单元

每条输入行是一个处理单元：

- `doc_id` 必须唯一。
- `line_no` 从 1 开始且连续。
- `text` 必须非空。
- `punct_class` 属于 `none`、`terminal`、`comma`、`quote`、`space`。
- 所有偏移与长度均以 Unicode code point 为单位，不使用 UTF-8 字节数或 UTF-16 单元数。

### 分词

词典词条按词频构造候选边。边分数：

```text
edge_score = log(word_freq)
path_score = Σ edge_score
```

未命中词典的单码点回退边使用 `freq = 1`，分数为 `0.0`。路径分数不减去词典总频，因此词典总频变化不会使完全未命中旧/新词条的行产生分数变化；这也是版本失效判定的必要条件。

同分路径裁决顺序固定为：

1. 路径总分更高；
2. 词条起始偏移序列按字典序更小；
3. 词条结束偏移序列按字典序更小；
4. 词条文本序列按字典序更小。

路径总分在比较和输出前统一量化到 12 位小数。实现不依赖字典文件条目顺序或 Python 字典遍历顺序。每个词输出 `word`、码点 `start`/`end`、`freq`、`edge_score`、`source` 和受控词性候选。

### 词性标注

标注函数只接收分词快照、受控标签集和配置，不读取输入校验快照或原始文件。

每个词的路径权重：

```text
token_weight = abs(line_path_score) / token_count
base = token_weight × (1 + log(freq)) × (1 + 0.05 × min(8, codepoint_len))
label_score = base × label_prior
```

同分时按受控标签集合序号升序选择。置信度为选中标签分数占该词全部候选分数的比例。输出逐词标签、候选分数、置信度和行级汇总。

### 汇总

汇总阶段从标注快照生成逐行标签计数、平均置信度、路径分数、token 数和全局标签计数。最终业务输出不包含快照版本等运行元数据，因此完整运行、恢复运行和升级后的等价运行可以逐字节比较。

## 快照与恢复

持久化文件默认位于：

```text
runs/current/snapshots.sqlite3
```

SQLite 使用 WAL、`synchronous=FULL` 和 `BEGIN IMMEDIATE`。提交事务同时写入：

- 阶段名；
- 全局单调递增 `snapshot_version`；
- 规范 JSON 内容 SHA-256；
- 完整 `completed_doc_ids`；
- 受影响、重算、复用 doc_id 集合；
- 词典/标签/输入/配置版本与校验和。

只有 `recomputed ∪ reused` 精确覆盖代际全部 doc_id 时才允许提交。部分完成阶段不能提交。

启动时读取活动代际各阶段最新快照。已提交阶段完全跳过；首个缺失阶段从起点重建。硬终止进程不写入任何未提交内存结果。恢复报告同时区分：

- 代际级来源：`affected`、`recomputed`、`reused`；
- 本次调用执行情况：`executed_stages_this_run`、`skipped_committed_stages`、`invocation_recomputed`。

读取快照时会重新计算内容 SHA-256，不符合返回 `E_SNAPSHOT_CORRUPT`。

## 版本替换与失效复用

每次资源身份变化创建新代际：

```text
(input_checksum, config_checksum, dict_version, dict_checksum,
 labels_version, labels_checksum)
```

失效探测流程：

1. 输入或配置变化：四阶段全部失效。
2. 仅词典/标签变化：校验阶段复用。
3. 使用新词典对全部行做只读分词探测，与旧分词逐行比较，得到精确分词失效集合。
4. 使用新标签集做只读标注探测，与新/旧分词结果比较，得到精确标注失效集合。
5. 汇总失效集合为分词与标注失效并集。
6. 新代际提交完整阶段快照：未失效行从基线代际复用，失效行重算。

若某行既未命中旧词典也未命中新词典，则禁止进入分词失效集合；实现会显式检查并在违反时返回 `E_INCONSISTENT`。

词典或标签版本低于活动代际时返回 `E_SNAPSHOT_ROLLBACK`。旧代际快照保留用于审计，但不能覆盖活动新版本。

## 并发与资源上限

- 写提交通过 `BEGIN IMMEDIATE` 串行化，`busy_timeout=0`。
- 后到并发提交立即返回 `E_STAGE_BUSY`，不阻塞等待。
- 单行最大码点数超限返回 `E_RESOURCE_LIMIT`。
- 词典候选边总数超过预算时：
  - `candidate_limit_policy=error`：`E_RESOURCE_LIMIT`；
  - `candidate_limit_policy=degrade`：每个起始位置限制候选数，标记 `SEGMENT_CANDIDATE_CAP`。
- 标注中间结果估算超限时：
  - `tag_memory_policy=error`：`E_RESOURCE_LIMIT`；
  - `tag_memory_policy=degrade`：释放候选分数中间缓存，标记 `TAG_MEMORY_TRIM`。
- 所有降级记录均声明 `offset_unit=unicode_codepoint`，且不改变字符偏移口径。

配置示例：

```json
{
  "max_codepoints_per_line": 240,
  "max_candidate_edges_per_stage": 20000,
  "candidate_limit_policy": "degrade",
  "degrade_max_candidates_per_start": 3,
  "max_tag_resident_bytes": 8000000,
  "tag_memory_policy": "degrade",
  "fallback_pos": "x"
}
```

## 业务错误码

| 错误码 | 触发条件 |
|---|---|
| `E_DUP_DOC` | 重复 `doc_id`，带 `doc_id` 与 `line_no` |
| `E_LINE_NO` | `line_no` 非连续或非整数，带 `doc_id` 与行号 |
| `E_TEXT_EMPTY` | `text` 为空或仅空白，带 `doc_id` 与行号 |
| `E_DICT_FORMAT` | 词典条目结构、词频、重复词条等格式错误 |
| `E_LABEL_INVALID` | 词典或标注引用越界标签 |
| `E_OFFSET_LEN` | 偏移不连续、长度不符或未覆盖全行，带 `doc_id` 与行号 |
| `E_UNATTRIBUTED` | 未被业务阶段归因的异常，保留异常类型与 traceback |
| `E_INCONSISTENT` | 自检发现偏移、标签或集合关系不一致，带类别与 doc_id |
| `E_SNAPSHOT_CORRUPT` | 快照内容校验和不符或 JSON 损坏 |
| `E_SNAPSHOT_ROLLBACK` | 词典/标签版本回退或快照版本回退 |
| `E_STAGE_BUSY` | 并发阶段提交冲突，后到者立即失败 |
| `E_RESOURCE_LIMIT` | 码点数、词典候选或标注内存超过硬上限 |
| `E_STAGE_INCOMPLETE` | 阶段未覆盖全部 doc_id 或重算/复用集合不一致 |
| `E_STAGE_ORDER` | 阶段前置依赖未提交 |
| `E_STAGE_CONFLICT` | 同代际同阶段内容冲突或非法版本 |

## 自检

`run_selfcheck` 校验：

- 分词偏移从 0 开始、首尾相接、末偏移等于行码点长度；
- `end - start == len(word)` 且切片文本等于词条；
- 每个标签属于当前受控集合；
- 标注 token 计数与汇总标签计数一致；
- 重算集合与复用集合无交集；
- 两集合并集覆盖全部文档，且受影响集合等于重算集合。

失败返回 `E_INCONSISTENT`，逐项给出类别、阶段、doc_id、line_no 与上下文。

## 实际验证结果

环境：

```text
Python 3.10.12
NumPy 1.26.4
```

测试命令：

```bash
python3 -m unittest discover -s tests -t . -v
```

实际结果：17 项测试全部通过。

一键演示实际结果：

- 完整运行：`OK`
- 在 `segment` 提交后硬终止：进程返回码 `137`
- 重启最后已提交阶段：`segment`
- 恢复继续阶段：`tag`
- 恢复标注与完整运行：逐字节一致
- v2 升级：校验阶段复用 26 行；分词/标注/汇总各重算 5 行、复用 21 行
- v2 升级结果与 v2 全新运行：逐字节一致
- 两次算法生成：分词、标注、汇总逐字节一致
- 低预算：产生 `SEGMENT_CANDIDATE_CAP` 与 `TAG_MEMORY_TRIM`，自检 `OK`
- 严格上限：返回 `E_RESOURCE_LIMIT`，定位到 `D0001` 第 1 行
- 异常链：`E_DUP_DOC`、`E_LINE_NO`、`E_TEXT_EMPTY`、`E_DICT_FORMAT`、`E_LABEL_INVALID`、`E_OFFSET_LEN`、`E_UNATTRIBUTED` 全部按预期返回
- 并发提交：`E_STAGE_BUSY`
- 快照篡改：`E_SNAPSHOT_CORRUPT`
- 版本回退：`E_SNAPSHOT_ROLLBACK`

演示报告会记录实际 SHA-256，便于重复验证。
