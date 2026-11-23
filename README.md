# 客服会话摘要改写 DAG

面向本地客服会话的确定性摘要、改写、增量失效、内容寻址缓存与四阶段恢复应用。项目不依赖网络、外部数据集或容器；唯一第三方依赖为 `numpy==1.26.4`。

## 已实现能力

- 固定种子生成受控词表、客服会话、逐轮真值摘要单元与改写约束。
- 四节点 DAG：`chunk_tokenize -> unit_score -> rewrite -> summary_merge`。
- 四类节点产物均带 SHA-256 内容哈希，并写入可审计的本地内容寻址缓存。
- 追加轮次、改写强度变化、词表版本提升三类定向失效传播。
- 四阶段单调版本快照、原子提交、非阻塞并发锁、强杀后恢复。
- 输入/配置校验、资源上限与降级、跨模块一致性自检。
- 会话增删、参数修改、缓存查询、增量演示和恢复演示 CLI。

## 实际环境

本工作区初始系统 Python 为 3.10.12。已通过 `uv` 安装并验证 Python 3.12.16：

```bash
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python numpy==1.26.4
.venv/bin/python --version
.venv/bin/python -c "import numpy; print(numpy.__version__)"
```

实测输出：

```text
Python 3.12.16
1.26.4
```

## 快速开始

```bash
source .venv/bin/activate

# 1. 生成/校验本地确定性样本
python -m support_summary init --root .

# 2. 全量执行并写报告
python -m support_summary run --root . \
  --cache-dir artifacts/cache \
  --quiet --output artifacts/run.json

# 3. 一致性自检
python -m support_summary self-check --root . \
  --cache-dir artifacts/cache

# 4. 查看某会话的缓存键组成与产物哈希
python -m support_summary cache-query --root . \
  --cache-dir artifacts/cache --session S001

# 5. 三类增量演示
python -m support_summary incremental --root . \
  --cache-dir artifacts/cache-inc-append \
  --scenario append --session S001
python -m support_summary incremental --root . \
  --cache-dir artifacts/cache-inc-strength \
  --scenario strength --strength 0.65
python -m support_summary incremental --root . \
  --cache-dir artifacts/cache-inc-vocab \
  --scenario vocab --new-vocabulary data/vocabulary_v2.json

# 6. 真实子进程强制退出后恢复
python -m support_summary recovery-demo --root .
```

## 可编辑入口

```bash
python -m support_summary sessions list --root .
python -m support_summary sessions add --root . S005
python -m support_summary sessions remove --root . S005
python -m support_summary config show --root .
python -m support_summary config set --root . rewrite_strength 0.65
python -m support_summary interactive --root .
```

## 数据与配置

- `data/sessions.jsonl`：固定种子生成的 4 条会话，每条 6 轮；含真值摘要单元和改写约束。
- `data/vocabulary_v1.json` / `data/vocabulary_v2.json`：受控词表多版本。
- `data/params_v1.json` / `data/params_v2.json`：权重、强度、平滑、精度、块大小和资源上限。
- `checkpoints/demo/`：示例快照目录。
- `scripts/generate_samples.py`：无网络样本重建脚本。
- `scripts/verify_all.py`：完整验收脚本，输出 `artifacts/verification.json`。

## 验证

```bash
python -m unittest discover -v
python scripts/verify_all.py
```

当前记录：

- 21 项单元/集成测试在 Python 3.12.16 下通过。
- 全量摘要指纹：`8975320d9f4b98a7ed7cf70aacc8e5360051f6cc97b5016d5a0d9873d766bfa6`。
- 全量 69 个节点耗时缓存全 miss；二次运行 69 个节点全 hit，摘要逐字节一致。
- 追加：只重算 6 个节点，复用 68 个节点。
- 强度：只重算改写与合并 45 个节点，复用上游 24 个节点。
- 词表升级：69 个节点全部重算。
- 强制退出码 97；恢复只执行 `rewrite`、`summary_merge`，摘要逐字节一致。

详见 `docs/TECHNICAL.md` 与 `docs/VERIFICATION.md`。
