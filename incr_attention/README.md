# 分块增量注意力（FlashAttention 风格）算子组件

纯 Python 3.11 + NumPy 实现，无 GPU、无外部数据、无容器。算子对本地确定性合成的
Q/K/V 序列按输出块做在线 softmax（流式维护行最大值与指数和），支持 K/V 块增量
追加与中间块替换、精确依赖失效、单次重算预算调度、原子快照恢复、线程池并发以及
float64 串行参考精度审计。

## 目录结构

```
incr_attention/
├── configs/            # 串行/并发/紧预算引擎配置与异常注入配置
├── reports/            # 原子替换写出的 JSON 运行报告
├── samples/            # 固定种子生成的 Q/K/V、编辑序列与 float64 参考
├── snapshots/          # 原子替换写出的 npz 快照
├── scripts/
│   ├── generate_samples.py   # 样本与 float64 参考生成
│   ├── run_example.py        # 端到端增量 + 重分块 + 审计
│   ├── run_injections.py     # 8 类异常注入隔离演示
│   └── demo_recovery.py      # 强制终止后的快照恢复演示
├── src/incrattn/
│   ├── kernel.py       # 分块在线注意力内核（float32 观测、float64 累加）
│   ├── reference.py    # float64 串行全量参考与归因变体
│   ├── dependency.py   # 块级依赖图与精确失效判定
│   ├── engine.py       # 编辑、失效、预算调度、并发与快照编排
│   ├── snapshot.py     # 临时文件 + os.replace 原子快照与篡改检测
│   ├── injection.py    # 注入配置与内核阶段钩子
│   ├── audit.py        # 精度审计与增量/全量等价性
│   └── report.py       # 原子 JSON 报告（按块序号排序）
└── tests/              # 27 个 unittest 用例
```

## 快速开始

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_samples.py
python3 -m unittest discover -s tests -p "test_*.py" -v

python3 scripts/run_example.py --dataset small --config configs/engine_serial.json
python3 scripts/run_example.py --dataset medium --config configs/engine_concurrent.json
python3 scripts/run_example.py --dataset edit_heavy --config configs/engine_tight_budget.json
python3 scripts/run_injections.py
python3 scripts/demo_recovery.py
```

## 观测口径

每个输出块持久化：行最大值 `row_max`、指数和 `exp_sum`、输出张量 `output`、
读取的 K/V 块区间 `kv_block_range` 以及对原始字节做 SHA-256 的块指纹。
真值由 `reference.py` 的 float64 串行全量路径给出；审计报告输出最大绝对/相对误差、
超差元素数，并把误差归因到行最大值选择、指数和累加或加权求和三阶段。

## 核心语义

- 因果掩码：查询位置 `i` 只读取键位置 `j <= i`，输出块 `b` 的 K/V 读区间为 `[0, b]`。
- 精确失效：替换/追加 KV 块 `k` 仅失效读取它的输出块 `b >= k` 及其因果下游；
  未失效块直接复用持久化结果，不重新执行内核。
- 预算调度：每批按块序号升序取 `recompute_budget` 个失效块，永不跳过更早的块；
  预算为 0 或不足时以 `E_RECOMPUTE_PENDING` 保留队列，后续批次收敛。
- 重分块：`change_block_size` 重建依赖图与累加链，清空全部旧分块结果。
- 快照：临时文件 `fsync` 后 `os.replace` 原子替换；校验和不匹配或配置/输入指纹
  不一致时以 `E_SNAPSHOT_STALE` 拒绝续跑。
- 并发：线程池只用于无依赖块计算，结果按块序号稳定排序回收；并发度不改变任何
  输出字节、清单顺序或预算扣减序列。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_OK` | 正常 |
| `E_RECOMPUTE_PENDING` | 受预算限制保留的待重算块 |
| `E_SNAPSHOT_STALE` | 快照版本/校验和/指纹不匹配，拒绝续跑 |
| `E_NON_FINITE_INPUT` | 输入含 NaN/Inf |
| `E_LENGTH_MISMATCH` | Q/K/V 形状或块长度不匹配 |
| `E_INVALID_CONFIG` | 块大小、预算或并发数非法 |
| `E_KERNEL_SCORE` / `E_KERNEL_NORMALIZE` / `E_KERNEL_WEIGHTED` | 内核三阶段失败 |
| `E_EDIT_REJECTED` | 非法 K/V 编辑 |
