# GraphStore：两代图快照的确定性遍历与索引引擎

这是一个完全本地运行的 Python 组件，用同一套索引与遍历链路处理 v1/v2 两代图快照。所有时间来自注入的固定逻辑时钟；代码不读取系统墙钟，不访问网络或外部数据集。

## 环境

- 目标 Python 3.11；当前执行环境只提供 Python 3.10.12，代码保持 3.10+ 兼容并在 3.10.12 上完成全部验证。
- 已确认 `sqlite3` 3.37.2、NumPy 1.26.4 可用。
- Windows 文件锁通过 `msvcrt` 实现，Linux/WSL 通过 `fcntl.flock` 实现；本次自动化验证运行于 WSL2/Linux，Windows 原生分支未在本次容器中执行。
- 不启动 Docker，不依赖任何服务或网络。

## 快速开始

所有命令均在工作区根目录执行，路径为工作区相对路径。

```bash
python3 -m graphstore.cli \
  --db data/graphstore.sqlite3 \
  --clock samples/clock.json \
  load --package samples/packages/base/manifest.json

python3 -m graphstore.cli \
  --db data/graphstore.sqlite3 \
  --clock samples/clock.json \
  traverse --batch-id two-hop --kind reachable --node-id n1 --depth 2

python3 -m graphstore.cli \
  --db data/graphstore.sqlite3 \
  --clock samples/clock.json \
  status

python3 -m graphstore.cli \
  --db data/graphstore.sqlite3 \
  --clock samples/clock.json \
  verify --batch-id two-hop
```

`traverse` 和 `resume` 输出稳定排序的紧凑 JSON。相同索引版本、相同查询、相同 batch ID 重复执行会返回已提交结果，不重复累计统计。

## 样本

- `samples/v1/snapshot.json`：v1 单文件快照，包含扁平邻接表、出边声明、节点权重、相对写入秒数和有序线性索引。
- `samples/v2/manifest.json`、`samples/v2/shards/*.json`、`samples/v2/meta.sqlite`：v2 分片快照、内容哈希和 SQLite 元数据库。
- `samples/packages/base/manifest.json`：可加载的双代完整包。
- `samples/packages/append/manifest.json`：节点数从 9 增至 10 的增量追加分类样本。
- `samples/packages/rollback/manifest.json`：版本回退样本。
- `samples/packages/clock-rollback/manifest.json`：逻辑时钟回拨样本。
- `samples/packages/partial/manifest.json`：分片文件哈希损坏样本。
- `samples/packages/missing/manifest.json`：分片文件缺失样本。
- `samples/clock.json`：固定逻辑时钟与 v1 时间换算配置。
- `samples/injection.json`：恢复与异常链路的注入清单。

样本可由 `python3 scripts/build_samples.py` 重建，生成过程不使用墙钟。

## CLI

| 子命令 | 作用 |
| --- | --- |
| `load` | 校验完整包并提交索引；相同包幂等。 |
| `merge` | 只做校验、合并与替换分类预览，不写持久化状态。 |
| `traverse` | 执行 exact、label_prefix、reachable、filtered 查询并持久化批次。 |
| `replace` | 整体替换或恢复中断的索引构建。 |
| `resume --batch-id` | 从最后检查点恢复遍历批次。 |
| `resume --package` | 恢复中断的索引构建。 |
| `status` | 输出当前索引水位、计数、墓碑数和遍历提交水位。 |
| `verify` | 校验当前索引计数、邻接摘要、合并/迁移裁决、批次与结果摘要。 |

查询示例：

```bash
# 精确查询（墓碑仍可见，带审计字段）
python3 -m graphstore.cli --db data/graphstore.sqlite3 --clock samples/clock.json \
  traverse --batch-id exact-n3 --kind exact --node-id n3

# 标签前缀查询
python3 -m graphstore.cli --db data/graphstore.sqlite3 --clock samples/clock.json \
  traverse --batch-id prefix-person --kind label_prefix --prefix person

# 边标签 + 权重过滤、深度 3
python3 -m graphstore.cli --db data/graphstore.sqlite3 --clock samples/clock.json \
  traverse --batch-id filtered --kind filtered --node-id n1 --depth 3 \
  --edge-label follows --min-weight 0.5 --max-weight 1.0
```

## 异常码

| 场景 | 业务错误码 |
| --- | --- |
| 节点不存在 | `GS_NODE_NOT_FOUND` |
| 边标签非法 | `GS_INVALID_EDGE_LABEL` |
| 深度上限非正或超限 | `GS_INVALID_DEPTH` |
| 分片哈希不匹配 | `GS_SHARD_HASH_MISMATCH` |
| 版本回退 | `GS_VERSION_ROLLBACK` |
| 逻辑时钟回拨超阈值 | `GS_CLOCK_ROLLBACK` |
| 遍历批次摘要不匹配 | `GS_BATCH_DIGEST_MISMATCH` |

错误输出为稳定 JSON，并保留节点、路径、版本、期望/实际摘要等诊断字段。CLI 返回码为 `2`；测试故障注入导致的硬终止返回 `-9`/`137`。

## 验证

```bash
pytest -q
python3 scripts/run_acceptance.py --output docs/verification-result.json
```

当前结果：26 项测试全部通过；验收脚本完成双代合并、两跳遍历、遍历硬终止恢复、替换硬终止恢复、回退拒绝和坏分片整包拒绝。实际输出见 `docs/verification-result.json` 和 `docs/VERIFICATION.md`。

设计与实现细节见 `docs/TECHNICAL.md`，样本格式见 `samples/README.md`。
