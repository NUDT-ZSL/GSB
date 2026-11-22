# 倒排索引构建 / 压缩 / 查询 / 恢复 / 验证管线

面向本地搜索与推荐前链路的数据处理管线：从确定性生成的文档集合构建倒排索引，
对每个 term 的倒排表做分块 + 差分/变长整数压缩，在压缩索引上完成 BM25 类打分
与 top-k 检索，并提供阶段快照恢复、整体替换防护与独立结果验证工具。

纯本地运行，无网络依赖，无容器依赖。Python 3.10+，依赖 NumPy（样本生成）。

## 目录结构

- `pipeline/encoding.py` — varint + doc_id 差分编码、倒排表分块编解码
- `pipeline/sample_gen.py` — 固定种子的文档/查询 JSONL 确定性生成器
- `pipeline/snapshot.py` — 带版本号与 SHA-256 校验和的快照存储（tmp+`os.replace` 原子提交）
- `pipeline/builder.py` — 三阶段（ingest / compress / stats）状态机构建器与崩溃恢复
- `pipeline/index_reader.py` — 压缩索引查询侧：原子切换、BM25 打分、top-k、缓存与降级
- `pipeline/verify.py` — 独立结果验证工具
- `pipeline/cli.py` — 命令行入口（generate / build / query / verify）
- `tests/test_pipeline.py` — 22 个验收测试

## 快速开始

```bash
pip install -r requirements.txt
python -m pipeline.cli generate --out data                 # 确定性样本
python -m pipeline.cli build --docs data/docs.jsonl --store store --block-size 8
python -m pipeline.cli query --store store --queries data/queries.jsonl
python -m pipeline.cli verify --store store --docs data/docs.jsonl --queries data/queries.jsonl
python -m pytest tests/ -q
```

## 核心设计

### 1. 确定性构建
文档按 `doc_id` 升序、term 按字典序处理；所有序列化使用
`json.dumps(sort_keys=True, separators=(",", ":"))` 加 LF 换行；不依赖字典遍历
顺序与任何未声明随机性。同一输入与配置下 `index.json` 逐字节一致
（`test_build_is_deterministic`）。

### 2. 分块与压缩
每个 term 的倒排表按 `block_size`（默认 8）切块。块格式（全部为无符号 varint）：
块头 = `count, first_doc_id, payload_byte_len`；负载 = doc_id 差分序列 + 原始词频
序列。任意块可独立解码，解压结果与原始倒排表逐项一致
（`test_block_roundtrip_and_independent_decode`）。非法 `block_size` 或资源上限
返回 `E_CONFIG_INVALID`。

### 3. 压缩索引上的 BM25 与 top-k
查询逐块解码参与打分：`idf = ln(1 + (N - df + 0.5)/(df + 0.5))`，
`score = idf * tf*(k1+1) / (tf + k1*(1 - b + b*dl/avgdl))`。
`query_checked` 将压缩路径分数与未压缩基线逐 doc 对比，偏差超过声明精度
（默认 1e-9）抛出 `E_SCORE_MISMATCH`，携带 `query_id / doc_id / delta`。

### 4. 阶段状态机与快照
三个阶段 `ingest → compress → stats` 各自产出快照文件
`stages/<stage>.snap.json`，头部显式包含 `stage`、`version`（单调递增）、
`committed_docs`、`checksum`（payload 的 SHA-256）。只有阶段完整处理完才提交；
提交 = 快照文件原子替换 + manifest 原子更新。未提交的中间态只写入 `wip/`，
重启时一律丢弃。

### 5. 强制终止与恢复
通过环境变量 `INDEX_PIPELINE_KILL_AT` 注入确定性 kill 点
（`ingest:doc`、`ingest:pre_commit`、`compress:doc`、`compress:pre_commit`、
`stats:pre_commit`），进程以 `os._exit(99)` 模拟 SIGKILL，不产生退出报告。
重启后从最后一个已提交阶段继续，已提交阶段不重算（版本号不变），
恢复产物与不间断运行逐字节一致（`test_crash_recovery_matches_uninterrupted`
对全部 5 个 kill 点参数化验证）。

### 6. 版本变化与整体替换
输入或配置变化（以 ingest 快照中记录的源文件 SHA-256 与配置比对判定）时，
所有阶段以更高版本号整体重建并原子替换，不做增量修补。旧版本提交被拒绝并
返回 `E_SNAPSHOT_ROLLBACK`；校验和不符或不可解析的快照返回
`E_SNAPSHOT_CORRUPT` 并指明阶段名与版本号，绝不使用部分可解析数据。

### 7. 并发与资源上限
查询侧通过锁保护的单一引用原子切换索引视图，并发查询要么看到整个旧索引、
要么看到整个新索引，不会读到半构建倒排表或混合统计量。构建提交使用非阻塞锁，
并发提交冲突立即返回 `E_BUILD_BUSY`。资源上限：`max_postings` 超限返回
`E_RESOURCE_LIMIT`；解码缓存超过 `max_cache_blocks` 或 `max_resident_bytes`
时降级为流式逐块解码，并记录降级类别（`cache_block_limit` /
`resident_memory_limit`）与涉及的 term 集合。

### 8. 独立验证工具
`pipeline/verify.py` 从原始文档独立重算并交叉核对：逐 term 分块解压一致性、
任意查询基线分数 vs 压缩索引分数、top-k 集合与排序合法性、快照统计量 vs
逐表明细、重算集合与复用集合（降级路径 term 单独记录）的无交集与全覆盖。
不一致返回 `E_INCONSISTENT` 并指出类别与首个偏差位置。

## 错误码

| 代码 | 含义 |
|---|---|
| `E_CONFIG_INVALID` | 分块/压缩/资源参数非法 |
| `E_SCORE_MISMATCH` | 压缩路径与基线分数偏差超精度（含 query_id/doc_id/delta） |
| `E_SNAPSHOT_ROLLBACK` | 旧版本快照试图覆盖新版本已提交阶段 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏或校验和不符（含阶段名与版本号） |
| `E_BUILD_BUSY` | 并发构建提交冲突，后到者立即失败 |
| `E_RESOURCE_LIMIT` | posting 总数等资源超限 |
| `E_INCONSISTENT` | 验证工具发现不一致（含类别与首个偏差位置） |

## 实际验证结果

- `python -m pytest tests/ -q`：**22 passed**（确定性、块编解码、BM25 一致性、
  5 个 kill 点崩溃恢复、快照回滚/损坏、整体替换、并发查询、构建忙、资源超限、
  缓存降级、验证工具正反向用例）。
- CLI 端到端（默认样本 200 文档 / 60 词项 / 20 查询，block_size=8）：
  - build 摘要：`doc_count=200, term_count=60, total_postings=1473, avgdl=21.82`；
  - verify 报告：`ok=true, terms_checked=60, queries_checked=20`，
    受影响 term 全覆盖，降级集合为空（未触发降级时）。
