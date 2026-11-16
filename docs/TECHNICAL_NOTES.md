# 索引构建与倒排压缩工作台 — 技术说明

## 1. 环境与目录

- 运行时：Python 3.10+（实测 Python 3.11），仅依赖 NumPy（样本生成的 Zipf 抽样），索引/编解码/查询全部使用标准库。
- 工作区目录：
  - `workbench/`：工作台包（归一化、编解码、存储、构建、查询、并发、报告、样本生成、CLI）。
  - `tests/`：24 个 `unittest` 用例，含真实 SIGTERM 子进程恢复测试。
  - `configs/`：索引配置（常规预算与零预算两档）。
  - `samples/`：固定种子生成的词表、JSONL 语料与查询集。
  - `output/`：索引产物、分片、快照、清单与 JSON 报告。

## 2. 模块与设计要点

### 2.1 词项归一化与解析（`normalization.py`）
- 全角 ASCII/标点折叠为半角（U+3000 折叠为空格，FF01–FF5E 平移 0xFEE0）。
- `str.casefold()` 大小写折叠，`split()` 合并任意空白后切词，位置为词项序号。
- 每文档统计词频与位置列表；空正文、非法 JSON、重复 ID 分别产出
  `E_EMPTY_DOC` / `E_PARSE_INVALID` / `E_DUP_DOC_ID`，进入失败清单且不阻断后续文档。

### 2.2 倒排分块与压缩（`codec.py`）
- 无符号 LEB128 变长整数；文档序号与位置均采用差值编码（delta）。
- 块容器：魔数 `IBLK01` + 档位 + 位置开关 + 块键 + 负载长度 + 差值负载。
- 档位一：纯变长整数差值编码；档位二：先按 `low_freq_threshold` 过滤文档频率过低的词项，
  再使用与档位一完全相同的差值编码，因此两档位对保留内容的编解码路径一致、逐项无损。
- 位置开关关闭时不编码位置，解码得到空位置元组；查询请求位置摘要时返回
  `E_POSITION_UNAVAILABLE`（每条位置查询一条失败记录，命中文档仍返回）。

### 2.3 分片、原子提交与指纹（`store.py`）
- 文档分片为 JSONL，倒排分片为带魔数的二进制容器，均按配置大小切分。
- 所有写盘走“同目录临时文件 + fsync + `os.replace`”原子替换。
- 版本指纹 = SHA256（词表指纹 + 配置结构指纹 + 档位 + 位置开关 + 全部分片摘要），
  写入 `manifest.json`，并在 `snapshots/` 留存每次提交的清单快照。
- 块内容哈希与档位/位置无关（统一按带位置的规范负载计算），用于判断逻辑内容是否变化。

### 2.4 版本失效与块级复用（`builder.py`）
- 新会话或恢复时，从已提交文档分片重建内存倒排表（只信任已提交产物）。
- 切换档位/位置开关后，`detect_version_mismatch()` 比对五类指纹要素并列出失配字段。
- 逐块判定：格式标志或内容哈希不一致即重建，一致则解码校验后复用；
  被低频过滤移除的块标记退役。复用/重建清单逐块列出原因，受影响分片才重写。

### 2.5 查询与延迟预算（`engine.py`）
- 合取检索：逐词解码相关块，候选文档集合做交集，结果按文档 ID 升序。
- 每次查询记录解码块数、解码词项数、过滤词、单调时钟（`time.monotonic_ns`）耗时。
- 超预算不终止线程：继续完整解码并返回全部结果，仅标注 `E_BUDGET_EXHAUSTED`
  与中间候选状态。预算为 0 时用于确定性演示该路径。

### 2.6 一致性与并发（`runner.py`）
- 增量构建与全量重建使用规范化结果（剔除耗时）逐项比对：文档 ID 顺序、命中词项、
  位置摘要、失败清单与中间状态；四组（两档位 × 两位置开关）分别比对。
- 线程池并发时每个查询使用独立引擎实例，结果携带由查询序号派生的 `sort_key`，
  回收后按稳定键排序，保证结果顺序、失败清单顺序与串行逐字段一致。

### 2.7 异常链路与可处理退出
- 文档/查询/块在 parse、build、encode、decode 各阶段的异常统一记录
  标识、阶段、业务码、异常类型与消息，其余项继续执行。
- SIGTERM/SIGINT 置停止标记，块循环在当前块处理完成后检查，确保当前批次
  完整原子提交并写进度，再以退出码 0 结束（Windows 无 SIGTERM 时该测试自动跳过）。
- 强制终止后仅依据已提交分片与 `progress.json` 恢复；损坏分片在恢复时直接报错，禁止静默复用。

## 3. 运行方式

```bash
# 1. 生成确定性样本（固定种子）
python3 -m workbench generate --sample-dir samples --seed 20261117 \
  --doc-count 120 --vocab-size 160

# 2. 完整验收流水线（四组合一致性 + 档位切换 + 并发一致性）
python3 -m workbench verify --config configs/index.config.json \
  --corpus samples/corpus.jsonl --queries samples/queries.json \
  --workspace output/verify --report output/report.json --batch-size 12

# 3. 单独构建 / 查询
python3 -m workbench build --config configs/index.config.json \
  --corpus samples/corpus.jsonl --output-dir output/demo --clean \
  --report output/build.json
python3 -m workbench query --config configs/index.config.json \
  --queries samples/queries.json --output-dir output/demo \
  --report output/query.json

# 4. 可中断增量构建（配合 kill -TERM 验证退出码 0 与恢复）
python3 -m workbench incremental --config configs/index.config.json \
  --corpus samples/corpus.jsonl --output-dir output/incr --clean \
  --batch-size 3 --report output/incr.json

# 5. 测试
python3 -m unittest discover -s tests -v
```

## 4. 实际验证结果（本机）

- `python3 -m unittest discover -s tests`：24/24 通过。
- `verify` 报告 `all_consistent=true`：
  - 四组档位/位置组合下，增量与全量的版本指纹相同、查询规范化结果逐项一致。
  - 串行与 4 线程并发的查询结果、失败清单顺序逐字段一致。
  - 档位 L1→L2、位置 pos→nopos 及切回均正确识别版本失配，全部旧格式块重建、
    无静默复用（`no_silent_reuse=true`），仅受影响分片重写。
- 档位二实测过滤 5 个 df=1 低频词；过滤词在 L1 有命中、L2 解码 0 块并返回空结果。
- 零预算配置下 11 条查询全部标注 `E_BUDGET_EXHAUSTED`，但仍返回完整命中文档与中间状态。
- 语料注入 4 条坏数据，解析失败清单稳定输出
  `E_PARSE_INVALID / E_EMPTY_DOC / E_DUP_DOC_ID / E_EMPTY_DOC`。
- SIGTERM 子进程测试：退出码 0，部分提交后恢复续建，查询结果与一次性全量重建一致。

## 5. 关键约束与取舍

- 索引范围为文档正文（body）；标题与来源标签原样写入文档分片但不进入倒排表，
  与查询集基于正文词表分布的设计一致。
- 文档 ID 在分片内以摄入序号（doc_seq）做差值，查询输出再映射回原始文档 ID 并升序。
- 一致性比对刻意剔除 `elapsed_ms` 等时钟字段，仅比较确定性业务字段；
  延迟统计的查询数与预算标注结构在串行/并发间仍要求一致。
