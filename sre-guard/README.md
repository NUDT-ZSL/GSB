# sre-guard

面向本地环境的 SRE 限流、熔断与降级决策领域组件。以本地 JSON 描述治理策略、
NDJSON 录入业务事件，按事件顺序推进决策，核心比例/速率/权重运算全部采用
**整数定点**并给出可追溯误差界，支持策略版本演进、会话快照整体替换恢复与
历史决策重放校验。无需任何网络账号或外部服务。

## 环境要求

- Python 3.11+
- NumPy（仅用于确定性样本事件流的批量构造与摘要）

```bash
python -m pip install -e .
# 或不安装，直接设置 PYTHONPATH
```

Windows PowerShell：

```powershell
set PYTHONPATH=src
python samples\generate_samples.py --out data\events.ndjson
python -m sre_guard.cli --base-dir .
```

## 目录结构

```
sre-guard/
├── config/policy.json          # 本地治理策略（版本化）
├── data/events.ndjson          # 确定性样本事件流（NumPy 生成）
├── samples/
│   ├── generate_samples.py     # 固定种子的样本生成器
│   └── demo_continue.py        # 快照断点续写示例
├── src/sre_guard/
│   ├── errors.py               # E_POLICY/E_EVENT/E_GRID/E_DEGRADE/E_REPLAY
│   ├── fixedpoint.py           # 网格、整数量化、乘除、误差界合成
│   ├── models.py               # 策略/事件数据模型
│   ├── validation.py           # 严格整数与行号定位
│   ├── policy.py               # 策略强校验、版本 diff
│   ├── events.py               # NDJSON 强校验
│   ├── ratelimiter.py          # 整数定点令牌桶
│   ├── breaker.py              # 滑动窗口 + 半开状态机
│   ├── degrade.py              # 确定性权重选择与误差沿链传播
│   ├── engine.py               # 限流→熔断→降级编排
│   ├── snapshot.py             # 快照持久化与整体替换恢复
│   ├── replay.py               # 重放校验与首个差异定位
│   ├── logging_utils.py        # 结构化 JSON 日志
│   └── cli.py                  # 命令行入口
├── tests/                      # 35 个 unittest 用例
└── docs/design.md              # 定点数与误差界推导
```

## 公共 API

```python
from sre_guard import (
    load_policy, load_events, Grid, GovernanceEngine,
    quantize, mul_fixed, compose_chain_probability,
    choose_degrade, save_snapshot, restore_snapshot, replay_snapshot,
    E_POLICY, E_EVENT, E_GRID, E_DEGRADE, E_REPLAY,
)

policy = load_policy("config/policy.json")
events = load_events("data/events.ndjson")
engine = GovernanceEngine(policy, Grid.from_exponent(4))
decisions = engine.run(events)
```

## 三个演示场景

```bash
python -m sre_guard.cli --base-dir .
```

1. **冷启动决策**：55 个事件全量推进，输出限流/熔断/降级结果与误差界。
2. **修改失败率阈值后决策**：版本号 1→2 单调递增，字段级 diff 可辨识。
3. **快照恢复重放**：当前 v2 与快照 v1 不一致时整体替换限流/熔断状态，
   再按快照网格与策略重放，55 个决策逐字段比对全部匹配。

## 三类异常注入（业务码 + 结构化日志）

```bash
python -m sre_guard.cli --inject bad-policy   # E_POLICY 阈值越界，含行号/字段路径
python -m sre_guard.cli --inject bad-event    # E_EVENT  时间戳回退，含行号
python -m sre_guard.cli --inject bad-grid     # E_GRID   快照缩放因子缺失
```

错误同时以 NDJSON 写入 `logs/structured.ndjson`。

## 误差界要点

详见 `docs/design.md`。一句话总结：所有乘除 floor 截断、单次上界 1 ulp；
令牌桶余数结转使累计欠发 `< 1 ulp` 且超发恒为 0；降级链误差按
`e_m = e_{m-1} + 输入量化 + 1` 逐级合成，并给出误差主导标注。

## 运行测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
