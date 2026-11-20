当前是空白工作区，没有现存项目、代码或数据附件，需要从零构建一个面向跨语言检索前链路的本地查询改写与索引调度引擎，并附结果验证工具。业务对象是本地确定性生成的多语言文档集合与查询集合：文档 JSONL 字段含 doc_id、doc_lang、terms 列表（term 与词频）、doc_len；查询 JSONL 字段含 query_id、query_lang、terms 列表、top_k；另有一份可配置的跨语言词项桥接资源，包含语言对方向表、词项桥接表（源语言 term 到目标语言 term 的多候选映射及桥接置信度）与打分权重表，各自带版本号与内容校验和。引擎需把查询词项经桥接资源投射到文档语言侧，构造多语言共享倒排索引，并在索引上做 BM25 类打分与 top-k 检索。核心业务难点是桥接是多候选且带置信度的，同一查询词项可展开为多个目标词项并影响倒排查找与打分的组合方式；同时桥接资源会版本演进整体替换，索引需按语言对分区重算，长构建被强制终止后只能从已提交分区继续。

任务环境与准备：
使用 Windows 本地工作区，创建项目目录与 Python 虚拟环境
安装 Python 3.10 及 NumPy 依赖（用于确定性样本生成与 TF-IDF 数值计算），版本在依赖清单中固定
准备本地确定性样本生成脚本，按固定随机种子生成多语言文档与查询 JSONL，以及带版本号与校验和的语言对方向表、词项桥接表与打分权重表
预留本地持久化目录用于分区快照、桥接资源快照与提交记录，禁止依赖任何在线翻译服务或真实语料下载

业务要求：
1. 实现确定性的查询词项桥接展开：按语言对方向表与词项桥接表把查询词项展开为目标语言候选词项集合，每候选携带桥接置信度；展开候选须按置信度降序、同分按目标词项字典序排序，并受可配置的最大候选数与置信度下限约束；同一输入与同一桥接资源版本下，展开结果与候选顺序必须逐字节可复现。
2. 实现多语言共享倒排索引的构建与分区：文档按 doc_lang 分入分区，每个分区内按 doc_id 升序、term 字典序生成倒排表并记录词频与文档长度统计；桥接资源版本变化时须识别受影响分区集合（方向表或桥接表条目涉及的语言对对应分区）并仅标记这些分区需重建，未受影响分区必须复用已提交分区快照，不得整体重建。
3. 实现桥接置信度参与的打分与 top-k 检索：对每条查询，按候选词项在各分区的倒排表查找、以桥接置信度加权组合 BM25 类分数，同一文档被多个候选词项命中时按显式声明规则合并贡献；须输出每文档每候选词项的原始分、置信度与加权贡献明细，使最终分可由明细独立重算一致，加权规则非法时返回 E_CONFIG_INVALID 并指明字段。
4. 实现索引分区快照与桥接资源快照的版本化提交：两者各携带单调递增版本号与内容校验和，分区快照只在分区内全部文档处理完成时提交，资源快照与分区快照的版本必须可在提交记录中交叉核对；检测到版本号或校验和变化时必须整体替换旧快照而非增量修补，损坏或校验和不符返回 E_SNAPSHOT_CORRUPT 并指明快照名与版本。
5. 实现强制终止后的分区级恢复：重启时读取已提交分区快照，从未提交分区的起点重建，已提交分区不得重算；桥接资源版本在中断期间发生变化时，须按新版本重新计算受影响分区集合，未被新版本涉及的分区快照仍可复用；恢复后的检索结果须与不间断完整构建的结果逐字节一致，恢复报告须列出复用分区与重建分区。
6. 实现构建与查询的并发切换及资源预算：查询可并发执行，新版本分区快照提交须原子生效，查询侧不得读到半构建分区或新旧桥接混用的统计量；同一时刻只允许一个构建批次提交生效，冲突时后到者立即获得 E_BUILD_BUSY 而不阻塞等待；为单分区倒排表 posting 数、查询展开候选总数与驻留打分中间表设可配置上限，超限返回 E_RESOURCE_LIMIT 或降级到缩减候选数并标记降级类别。
7. 实现输入与资源校验链路：重复 doc_id、term 为空、桥接表目标词项指向不存在语言、置信度越界、查询 top_k 非正分别返回对应业务码（E_DUP_DOC、E_TERM_INVALID、E_BRIDGE_LANG、E_CONFIDENCE_RANGE、E_CONFIG_INVALID）；样本级与资源级异常须归因到具体 doc_id 或桥接表条目与分区名，未被归因的异常须汇总到结果报告中。
8. 实现结果验证工具与跨分区一致性自检：验证器独立复核每个分区的倒排表可还原、每条查询的明细重算分数与最终分一致、top-k 集合与顺序合法；自检校验查询展开所用桥接版本与索引分区版本在提交记录中可核对、被标记重建分区与实际重建分区集合一致、同一 doc_id 不跨分区重复出现，检测到不一致返回 E_INCONSISTENT 并指出类别与版本号。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
用本地样本验证桥接展开排序、分区增量重建、强制终止后分区级恢复与异常码，附验证报告。

---

# 项目交付说明

## 结构

- `xretrieval/canon.py` — 规范化 JSON 序列化与 SHA-256 内容校验和，所有持久化产物字节可复现。
- `xretrieval/bridge.py` — 桥接资源模型（方向表 / 词项桥接表 / 打分权重表，各自带版本号与校验和）、确定性查询词项展开、版本间受影响语言集合 diff。
- `xretrieval/corpus.py` — 文档 / 查询 JSONL 加载与业务校验（E_DUP_DOC、E_TERM_INVALID、E_CONFIG_INVALID）。
- `xretrieval/index.py` — 分语言分区倒排索引构建（doc_id 升序、term 字典序）与分区快照内容。
- `xretrieval/scoring.py` — BM25 类打分、桥接置信度加权、显式 `weighted_sum` 合并规则。
- `xretrieval/store.py` — 版本化快照与提交日志：分区快照、桥接快照、批次提交记录、原子 CURRENT 指针、非阻塞 BUILD.lock。
- `xretrieval/engine.py` — 构建调度（增量 / 恢复 / 单批次互斥）、一致性查询视图、资源预算与降级。
- `xretrieval/validate.py` — 独立验证器（分区可还原、明细重算、top-k 合法）与跨分区一致性自检（E_INCONSISTENT）。
- `xretrieval/samplegen.py` — 固定种子（20261121）确定性样本生成：4 语言 240 文档、24 查询、v1/v2/v3 三版桥接资源及非法样本。
- `xretrieval/demo.py` — 验收演示驱动，输出 `artifacts/verification_report.json`。
- `tests/` — 30 项 pytest 用例。

## 快速开始

```bash
python3 -m venv --system-site-packages .venv   # Python 3.10，依赖见 requirements.txt
.venv/bin/python -m xretrieval.cli gen-sample --out artifacts/demo/data
.venv/bin/python -m xretrieval.cli build --state artifacts/demo/state --resource artifacts/demo/data/bridge_v1.json --docs artifacts/demo/data/documents.jsonl
.venv/bin/python -m xretrieval.cli query --state artifacts/demo/state --queries artifacts/demo/data/queries.jsonl --out results.jsonl --workers 4
.venv/bin/python -m xretrieval.cli verify --state artifacts/demo/state --docs artifacts/demo/data/documents.jsonl --queries artifacts/demo/data/queries.jsonl --results results.jsonl
.venv/bin/python -m xretrieval.cli selfcheck --state artifacts/demo/state --docs artifacts/demo/data/documents.jsonl
.venv/bin/python -m xretrieval.cli demo            # 完整验收，写 artifacts/verification_report.json
.venv/bin/python -m pytest tests -q                # 单元测试
```

## 关键设计

- **确定性展开**：候选按（置信度降序，目标词项字典序）排序，受 `max_candidates_per_term` 与 `min_confidence` 约束；同输入同资源版本展开结果逐字节可复现。
- **增量重建**：分区按 doc_lang 划分；资源版本演进时对方向表 / 词项桥接表做条目级 diff，仅目标语言涉及变更的分区标记重建，其余复用已提交快照；仅权重表变化不触发分区重建。
- **打分合并**：每文档每候选输出 `(query_term, candidate, confidence, raw_score, weighted)` 明细，最终分为 `fsum(weighted)`，验证器可独立重算；非法 `combine_rule` 在资源加载时返回 E_CONFIG_INVALID 并指明 `scoring_weights.combine_rule`。
- **版本化提交**：分区快照在分区内全部文档处理完成时提交进日志；桥接快照按版本号 / 校验和整体替换；批次提交记录交叉登记桥接快照版本与各分区版本，CURRENT.json 原子切换。
- **分区级恢复**：分区提交即持久化，崩溃后重启仅从日志中未提交的分区续建；中断期间资源版本变化时按新版本重算受影响集合；恢复结果与不间断构建逐字节一致（含提交序号）。
- **并发与预算**：查询并发只读已提交视图，批次提交原子生效；BUILD.lock 非阻塞，冲突立即 E_BUILD_BUSY；posting 数 / 展开候选总数 / 打分中间表三档上限，按 `resource_policy` 返回 E_RESOURCE_LIMIT 或降级并标记 `EXPANSION_TRUNCATED` / `SCORING_CANDIDATES_DROPPED`。

## 验证结果

- `pytest`：30 项用例全部通过。
- 验收演示：`artifacts/verification_report.json`，30 项检查全部通过，覆盖展开排序与可复现性、增量重建（v2 仅 de、v3 仅 en/fr）、崩溃恢复逐字节一致、中断期间版本演进、9 类业务异常码、快照损坏检测、构建并发互斥与查询一致性、资源限额与降级、验证器与一致性自检。
