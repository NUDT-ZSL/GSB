# GEMM/BMM 增量算子诊断工作台

本地、确定性的 GEMM/BMM 算子执行与诊断系统。项目使用 Python 3.10/3.11、NumPy 1.26.4 和标准库 Web 服务，不依赖 Docker、外部模型或网络服务。

## 快速开始

```bash
python3 -m venv venv
source venv/bin/activate
pip install numpy==1.26.4

# 当前 WSL 镜像缺少 ensurepip 时，可改用：
# virtualenv venv && ./venv/bin/python -m pip install numpy==1.26.4

python -m gemm_bmm_workbench.cli generate-samples --out samples
python -m gemm_bmm_workbench.cli demo --workspace . --state-dir runs/demo
python -m gemm_bmm_workbench.cli serve --state-dir runs/demo --host 127.0.0.1 --port 8899
```

打开 <http://127.0.0.1:8899/>。

一键验证：

```bash
./scripts/validate.sh
```

该脚本运行编译检查、unittest、增量 demo、SIGKILL 恢复演练和跨模块自检。实际验证结果见 `docs/VALIDATION_RESULTS.md`。

## 常用命令

```bash
python -m gemm_bmm_workbench.cli run \
  --jobs samples/batches/batch-001.jsonl \
  --state-dir runs/manual \
  --matrices samples/matrices.npz

python -m gemm_bmm_workbench.cli run \
  --jobs samples/batches/batch-002.jsonl \
  --state-dir runs/manual \
  --matrices samples/matrices.npz

python -m gemm_bmm_workbench.cli query-cache --state-dir runs/demo --job-id bmm_alpha
python -m gemm_bmm_workbench.cli selfcheck --state-dir runs/demo

python -m gemm_bmm_workbench.cli migrate-ledger --state-dir runs/demo --direction v1-to-v2
python -m gemm_bmm_workbench.cli migrate-ledger --state-dir runs/demo --direction v2-to-v1

python -m gemm_bmm_workbench.cli recovery-demo --state-dir runs/recovery-demo

python -m gemm_bmm_workbench.cli hold-lock --state-dir runs/demo --seconds 3 &
python -m gemm_bmm_workbench.cli lock-probe --state-dir runs/demo
```

也可以使用根目录入口 `python main.py demo ...`，或安装后使用 `wb ...`。

## 目录结构

- `gemm_bmm_workbench/sampling.py`：固定 SeedSequence 样本、两批增量清单和 NPZ 数组生成。
- `gemm_bmm_workbench/specs.py`：统一 schema、形状/dtype/layout/block/revision 校验。
- `gemm_bmm_workbench/planner.py`：分块计划与三级缓存键因子。
- `gemm_bmm_workbench/kernels.py`：显式循环顺序、Neumaier 补偿、次正规数保护、float64 参考与偏差谱。
- `gemm_bmm_workbench/cache.py`：内容寻址对象和 NumPy blob 缓存。
- `gemm_bmm_workbench/runtime.py`：增量合并、失效传播、资源预算、并行调度和结果输出。
- `gemm_bmm_workbench/snapshots.py`：四阶段快照、单调版本、校验和、恢复位置。
- `gemm_bmm_workbench/ledger.py`：v1 JSON 扁平行、v2 分片 JSON + SQLite、双向迁移。
- `gemm_bmm_workbench/selfcheck.py`：阶段集合、缓存哈希、增量覆盖和账本语义自检。
- `gemm_bmm_workbench/probes.py`：典型业务错误和容差越限探针。
- `gemm_bmm_workbench/web.py` 与 `gemm_bmm_workbench/static/index.html`：本地诊断工作台。
- `tests/test_workbench.py`：端到端与单元测试。

## 示例场景

固定样本第一批有 5 个 job，第二批有 6 个 job：

- `gemm_content`：revision 提升且 `content_seed` 改变，矩阵内容变化，三级全部重算。
- `bmm_alpha`：alpha 改变，计划节点复用，乘加与聚合后缀重算。
- `gemm_block`：block_size 改变，计划及下游重算。
- `gemm_dtype`：dtype 改变，计划及下游重算。
- `gemm_beta`：beta 改变，计划节点复用，乘加与聚合后缀重算。
- `gemm_added`：新 job，三级全部计算。

增量状态与从零执行最终清单的 `results/*.json` 原始字节逐一相同。

## 业务码

| 场景 | 业务码 |
| --- | --- |
| 非法形状 / BMM 批次不一致 / 矩阵数据维度不符 | `E_SHAPE_INVALID` |
| 不支持的 dtype | `E_DTYPE_UNSUPPORTED` |
| 非法 layout | `E_LAYOUT_INVALID` |
| block_size 非正 | `E_BLOCK_INVALID` |
| 超过 float64 参考容差 | `E_TOLERANCE_EXCEEDED` |
| 重复 job_id | `E_JOB_DUPLICATE` |
| revision 回退 | `E_REVISION_REGRESSION` |
| 账本版本未授权倒退 | `E_LEDGER_VERSION_REGRESSION` |
| 提交锁忙 | `E_STAGE_BUSY` |
| 任务数/内存/并行预算超限 | `E_RESOURCE_LIMIT` |
| 自检失败 | `E_SELFCHECK_FAILED` |

设计细节见 `docs/TECHNICAL_NOTES.md`，实际验证记录见 `docs/VALIDATION_RESULTS.md`，原始任务文本归档于 `docs/TASK_REQUIREMENTS.md`。
