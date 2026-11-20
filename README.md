# MTStream Workbench

面向本地机器翻译流式解码的可视化与诊断工作台。项目从固定种子生成受控双语样本，使用确定性哈希嵌入、词级对齐注意力、逐 step 前缀缓存和贪心采样完成解码；同时提供按块查询、整段重解码校验、延迟预算、依赖失效传播、快照版本化、并发预览和浏览器诊断台。

运行时不访问外部数据、不调用远程模型、不使用 GPU 或容器。

## 快速开始

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python numpy pytest

export PYTHONPATH=src
python -m mtstream generate --out data/sample_bundle.json
python -m mtstream decode --block-size 4
python -m mtstream selfcheck --out artifacts/selfcheck.json
python -m mtstream serve --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765` 即可查看工作台。也可安装为本地包：

```bash
python -m pip install -e .
mtstream decode
```

## 验收命令

```bash
./scripts/verify.sh
```

脚本会运行测试、生成样本、执行整段/分块解码、自检、四类失效分析和进程池/临时目录探针，并写入 `artifacts/verification_summary.json`。

## 核心实现

- `src/mtstream/core.py`：SHA-256 种子嵌入、位置加权源编码、统一 `decode_step`、softmax 注意力、前缀状态合并、整段与块路径记录比较。
- `src/mtstream/session.py`：按块交互查询、首块观测计时、整段独立计时、逐 step 前缀检查点、失效分析与输入切换。
- `src/mtstream/cache.py`：包含源编码摘要、对齐版本、块大小、上下文窗口和上游节点哈希的缓存键；键、载荷和状态三重校验。
- `src/mtstream/snapshots.py`：词表、对齐表、块大小、解码参数和源句的单调版本快照，整体替换、回退拒绝和损坏检测。
- `src/mtstream/selfcheck.py`：逐字节确定性、块/整段一致性、缓存哈希、集合覆盖、缓存重放和未对齐源词修改检查。
- `src/mtstream/server.py`：本地 HTTP API；`src/mtstream/web/index.html`：无外部依赖诊断台。
- `tests/`：40 项 pytest，覆盖一致性、失效、快照、异常码、资源限制、并发预览、进程池、CLI 和 HTTP。

## 一致性口径

整段与分块路径调用同一个 `decode_step`：

- `state.step` 始终是全局目标位置，块边界不重置位置偏移。
- 前缀状态包含 token、置信度、对齐投影、上一投影、候选归一化常数和注意力归一化累计量。
- 每 step 的候选分数、softmax 概率、归一化统计量与投影在两条路径中完全复用相同代码。
- 语义哈希排除计时字段；计时只作为可观测性数据。
- 缓存键使用“前缀作用域”源编码摘要：只覆盖已消费的、被对齐表引用的源位置，因此未引用源词修改不会误伤缓存。

## 业务错误

错误统一以 `{ok:false,error:{code,message,field?,data?,exit_code}}` 输出。CLI 的进程退出码始终为整数；HTTP 同时返回对应状态码。完整字段见 `docs/OUTPUT_SCHEMA.md`。

| 业务码 | 场景 |
|---|---|
| `E_DUP_CASE` | 重复 `case_id` |
| `E_TOKEN_UNKNOWN` | 源 token 不在受控词表 |
| `E_ALIGN_INVALID` | 对齐表引用不存在的目标词或非法对齐 |
| `E_CONFIG_INVALID` | 块大小非正、上下文窗口越界等 |
| `E_CHUNK_MISMATCH` | 分块与整段漂移；附首个偏差 step/字段 |
| `E_LATENCY_BUDGET` | 首块实际解码循环超预算；附完成阶段和 step 数 |
| `E_INCONSISTENT` | 查询结果、版本或并发预览不一致；附块号/类别 |
| `E_CACHE_CORRUPT` | 缓存或快照键/校验和/状态损坏；附条目键/版本 |
| `E_SNAPSHOT_ROLLBACK` | 快照版本回退或同版本内容变更 |
| `E_STAGE_BUSY` | 分块计划提交冲突；立即返回，不等待 |
| `E_RESOURCE_LIMIT` | 源长度、缓存条目或块内存超限 |

## 文档

- `docs/ARCHITECTURE.md`：算法、缓存键、失效传播、并发与资源策略。
- `docs/OUTPUT_SCHEMA.md`：JSON 输出字段清单与错误码。
- `docs/VERIFICATION.md`：本机实际验证结果与复现命令。
