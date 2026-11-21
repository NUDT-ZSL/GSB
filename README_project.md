# recall_fusion

单机多进程的四路确定性召回与加权融合实验框架。业务背景与逐条需求映射见
`docs/design.md`，实测结果见 `docs/validation.md`。

## 环境

Python ≥ 3.10，依赖仅 NumPy ≥ 1.24（`pip install -r requirements.txt`），
全部样本本地确定性生成，不联网、不需要任何账号或容器。

## 一键演示

```bash
bash run_demo.sh
```

包含：单元测试 → 单 worker 运行 → 三 worker 重跑 → 逐字节一致性校验 →
快照回退/损坏演示，产物默认落在 `artifacts/demo/`。

## 常用命令

```bash
# 零参数运行（全部参数有显式默认值）
python -m recall_fusion.cli run --output artifacts/demo/run_a

# 环境变量覆盖 worker / 批大小 / 预算 / 内存 / 精度
RF_WORKERS=4 RF_BATCH_SIZE=16 RF_PRECISION=fp32 \
RF_TOTAL_CANDIDATE_BUDGET=6000 RF_MEMORY_LIMIT_BYTES=200000 \
  python -m recall_fusion.cli run --output artifacts/demo/run_b

# 两次运行逐字节比对
python -m recall_fusion.cli verify artifacts/demo/run_a artifacts/demo/run_b

# 快照版本化、回退与损坏演示
python -m recall_fusion.cli snapshot-demo --output artifacts/demo

# 测试与静态检查
python -m unittest tests.test_pipeline -v
python -m pyflakes recall_fusion tests
```

## 业务错误码

`E_CONFIG_INVALID`、`E_RECALL_BUDGET`、`E_RESOURCE_LIMIT`、
`E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CORRUPT`、`E_WORKER_FAILURE`，
均带 `stage/field/query_id` 归因，定义于 `recall_fusion/errors.py`。
