# 实际验证记录

验证日期：2026-11-22

执行环境：Linux/WSL，Python 3.10.12，SQLite 3（Python 标准库），numpy 1.26.4。
代码只使用 Python 3.10/3.11 均支持的语法和标准库。Windows 锁实现使用
`msvcrt`，在当前 Linux 验证环境中实际走 `fcntl` 分支。

## 1. 自动化测试

命令：

```bash
python -m unittest discover -s tests -v
```

结果：`Ran 36 tests in 7.614s`，`OK`。

覆盖的具体场景：

| 类别 | 结果 |
| --- | --- |
| 输入校验 | 唯一性、整数资源、污点/容忍、插件覆盖、权重总和、注入轮次/阶段、非法输入零持久化均通过 |
| 过滤链 | 资源、污点、节点亲和/反亲和、Pod 反亲和、拓扑散布及首个证据来源均通过 |
| 打分 | 原始分、权重、归一化项、6 位量化、非整数权重误差上界、同分节点名决胜均通过 |
| 注入 | 四种异常码、目标节点/插件、消费水位、同轮控制 Pod 正常绑定均通过 |
| 强杀恢复 | `filter`、`score`、`bind` 三个强杀点均回滚；恢复后无重复扣减，状态与无强杀运行逐字节一致 |
| 预算与阶段 | 过滤阶段 `plugin_missing`、单轮逻辑时间预算耗尽和零资源扣减均通过 |
| 并发幂等 | 双进程同时调度时仅一个实例执行绑定；重复 `schedule` 返回空执行列表 |
| 回放/对账 | 摘要不匹配、注入水位篡改、负余量、悬挂扣减、悬挂归属、重复扣减、不可变触发器均通过 |
| 确定性 | 两个全新数据库的 `load`、`schedule`、`status`、`verify`、`inject` 输出逐字节一致 |

## 2. 固定样本演示

命令：

```bash
./scripts/demo.sh
```

样本包含 3 个节点、8 个 Pod、4 个轮次和 4 个注入。结果：

- 输入摘要：`aa347627d4cae30a669c6c31dab857a890f251134e8c76f9c930db5fccc103a8`
- 账本版本：`8`
- 账本摘要：`28b7312d578ac68cad28385b62395a725670daeb80adb5868947bad5cfdee439`
- 轮次水位：`4`
- 注入消费水位：`4`
- 逻辑时钟：`94 ms`
- 绑定：4 个对照 Pod
- 注入失败：4 个目标 Pod

四种注入的实际归属：

| Pod | 阶段 | 插件 | 节点 | 错误码 |
| --- | --- | --- | --- | --- |
| `r1-web-injected` | filter | `NodeAffinity` | `node-a` | `E_INJECT_NODE_LABEL_CONFLICT` |
| `r2-oversell-injected` | filter | `ResourceFit` | `node-c` | `E_INJECT_RESOURCE_OVERCOMMIT` |
| `r3-weight-injected` | score | `ResourceBinPack` | `node-a` | `E_INJECT_INVALID_WEIGHT` |
| `r4-plugin-injected` | score | `NodeAffinityMatch` | `node-c` | `E_INJECT_PLUGIN_MISSING` |

最终节点余量：

| 节点 | CPU 毫核余量 | 内存 MiB 余量 |
| --- | ---: | ---: |
| `node-a` | 3500 / 4000 | 7680 / 8192 |
| `node-b` | 2300 / 3000 | 5120 / 6144 |
| `node-c` | 3000 / 5000 | 5888 / 8192 |

`verify` 输出摘要：

```json
{
  "ok": true,
  "checks": {
    "allocations": 4,
    "attribution_entries": 8,
    "dangling_attributions": 0,
    "dangling_debits": 0,
    "duplicate_debits": 0,
    "injection_watermark": 4,
    "negative_balances": 0,
    "nodes": 3,
    "round_watermark": 4
  }
}
```

完整输出保存在 `reports/demo-*.json`。

## 3. 两次运行逐字节一致

命令：

```bash
./scripts/repeatability.sh
```

实际输出：

```text
repeatability: byte-identical
```

脚本分别对两个全新数据库执行 `load`、`schedule`、`status`，并对三份报告执行
`cmp`。报告保存在 `reports/repeat-{1,2}-*.json`。

## 4. 强杀恢复与并发

强杀由 `--crash-at filter|score|bind` 触发，进程在事务提交前直接
`exit(97)`。测试确认：

- 强杀后账本版本仍为 0，资源余量等于总量，所有 Pod 阶段仍为 `pending`。
- `resume` 后完成 8 个归属条目，最终 `status` 与无强杀运行逐字节一致。
- `allocations` 始终为 4 条，`duplicate_debits=0`、`dangling_debits=0`。

双实例测试同时启动两个 `schedule` 进程。实际结果为一个实例执行 8 个 Pod，
另一个执行 0 个；最终仍为 4 条 allocation，未出现重复绑定。
