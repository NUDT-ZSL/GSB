# 领域分词与词性标注本地诊断工作台

纯 Python 3.10+ 实现，仅使用标准库（`sqlite3`、`multiprocessing`、`logging`、`unittest`）。工作台把领域分词与词性标注拆成四阶段流水，提供确定性并行、双版本配置迁移、内容寻址缓存、定向失效、检查点恢复和 HTML 诊断。

## 快速开始

无需第三方依赖，可直接以源码目录运行：

```bash
python3 --version                       # 要求 >= 3.10
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 scripts/run_acceptance.py --output artifacts/acceptance
```

也可以安装为本地可执行命令：

```bash
python3 -m pip install -e .
dsn-workbench --help
```

典型 CLI 流程：

```bash
# 1. 固定种子生成 v1/v2 配置与 JSONL 任务
PYTHONPATH=. python3 -m dsn_workbench generate-sample \
  --output artifacts/demo --seed 20261127 --count 24

# 2. 并行执行，自动与独立串行参考逐句比对
PYTHONPATH=. python3 -m dsn_workbench run \
  --config artifacts/demo/config/v1.json \
  --tasks artifacts/demo/tasks.jsonl \
  --cache-dir artifacts/demo/cache \
  --state-dir artifacts/demo/state \
  --output artifacts/demo/output.jsonl \
  --report artifacts/demo/report.json \
  --html artifacts/demo/diagnostic.html \
  --workers 3 --batch-size 3

# 3. 配置迁移
PYTHONPATH=. python3 -m dsn_workbench migrate \
  --source artifacts/demo/config/v1.json \
  --target artifacts/demo/config/v2-migrated --to-version 2 \
  --report artifacts/demo/migration.json

# 4. 从最近一次运行状态重新渲染诊断页
PYTHONPATH=. python3 -m dsn_workbench diagnose \
  --state-dir artifacts/demo/state \
  --output artifacts/demo/diagnostic-again.html
```

## 功能映射

- `dsn_workbench/normalization.py`：按 locale/domain 配置做 NFKC、大小写与空白归一化，保留规范化字符到原文码点跨度的完整映射。
- `dsn_workbench/segmentation.py`：精确词典匹配与前缀/后缀规则候选生成；按“来源优先级、较短词形、码点字典序”贪心消歧；未覆盖字符落为单字 `unknown` token。
- `dsn_workbench/tagging.py`：受控领域词性表驱动 HMM，Viterbi 解码，forward-backward 后验置信度；校验原文/规范化文本逐字覆盖、无交叉、POS 受控。
- `dsn_workbench/config.py`：v1 单 JSON 与 v2 分片 JSON + SQLite 元数据双向读取、校验和验证、升级补全审计和降级显式丢弃。
- `dsn_workbench/cache.py` / `invalidation.py`：四阶段内容寻址缓存、完整缓存键、词汇/规则依赖失效和阶段后缀重算。
- `dsn_workbench/state.py`：四阶段状态机、批次 pending、全阶段成功后原子提交、全局单调检查点版本、非阻塞提交锁、SQLite 账本。
- `dsn_workbench/pipeline.py`：确定性进程池分块调度、资源策略、串并行逐字节校验、运行清单与报告。
- `dsn_workbench/report.py`：无外部依赖的 HTML 可视化诊断。
- `dsn_workbench/sample.py`：固定种子、固定规则/词典/任务模板的本地样本生成器。

配置格式、失效算法、恢复语义和错误码的详细说明见 [TECHNICAL.md](TECHNICAL.md)。

## 实际验证结果

环境：WSL2 Linux，Python `3.10.12`，SQLite `3.37.2`。固定种子 `20261127`，24 句。

| 验收项 | 结果 |
|---|---|
| 单元/集成测试 | `33/33` 通过 |
| 并行 vs 独立串行输出 | 一致，`sha256=efb7f1ecd15d1688b545e81bb8868f8edad14003ec1796b19fcc50f2a62edeb7` |
| 串并行逐句 token/POS 比对 | `24/24` 句一致，首个差异为 `null` |
| 参考 `token_list` / `pos_tag_list` | 各 `24/24` 命中 |
| v1 vs v2 业务输出 | 一致，同一 `sha256` |
| 定向失效（删除词典项“财报”） | 重算 7 句，复用 17 句，复用 75 个阶段产物 |
| 中断恢复 | 规范化/候选检查点复用；消歧/标注各重建 24 句；恢复后输出哈希与干净运行一致 |
| 检查点版本 | `2,3,5,6`，全局单调递增；pending `disambiguation.json` 被丢弃 |

证据文件：

- `artifacts/acceptance/verification.json`：机器可读验收摘要。
- `artifacts/acceptance/diagnostic.html`：可视化诊断页。
- `artifacts/acceptance/parallel_output.jsonl`：并行输出。
- `artifacts/acceptance/recovered_output.jsonl`：中断恢复输出。
- `artifacts/acceptance/sample/`：固定种子样本、v1/v2 配置。
- `artifacts/acceptance/*/state/ledger.sqlite3`：版本化运行账本。

验收脚本会保留样本、检查点、账本、摘要和输出，运行结束后裁剪体积较大的缓存对象；缓存结构在实际运行过程中已完整参与调度与恢复。

## 资源与错误约定

资源超限优先级为：`workers` 硬上限 → `batch_size` 硬上限 → token 缓存内存估算。前两者直接返回 `E_RESOURCE_LIMIT`；内存超限时按 `resource_policy` 返回 `E_RESOURCE_LIMIT` 或降级到串行并记录 `serial_due_token_cache_memory`。检查点/配置快照提交冲突立即返回 `E_STAGE_BUSY`，不阻塞等待。

主要业务码：`E_PARALLEL_MISMATCH`、`E_VERSION_UNSUPPORTED`、`E_FORMAT_CORRUPT`、`E_COVERAGE_MISMATCH`、`E_TOKEN_OVERLAP`、`E_TAG_INVALID`、`E_CHECKPOINT_CORRUPT`、`E_STAGE_BUSY`、`E_RESOURCE_LIMIT`。
