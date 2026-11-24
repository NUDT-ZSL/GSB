# 实际验证记录

执行日期：2026-11-25  
固定种子：`20261125`  
执行环境：容器自带 Python 3.10.12、NumPy 1.26.4（Windows 目标环境按任务要求使用 Python 3.11+）

## 执行命令

```text
python -m unittest discover -s tests -v
python tools/run_acceptance.py --samples samples --out artifacts/acceptance --seed 20261125
```

## 单元测试

```text
Ran 27 tests in 3.549s
OK
```

覆盖校验、编辑、确定性、增量、快照摘要/裁决、持久化恢复、内存预算和重连错误定位。

## 固定种子验收

机器报告：`artifacts/acceptance/acceptance_report.json`  
自动生成摘要：`artifacts/acceptance/VALIDATION.md`

关键结果：

| 项目 | 结果 |
|---|---|
| 样本帧数 / 事件数 | 151 / 5 |
| 9 组分块×并发组合 | 摘要全部相同，最大姿态差 0.0 |
| 基础输出摘要 | `sha256:e86dcee360d89ca9d6caf34e7ef2b73e60613f5eb5783f269ef67da723f212be` |
| 全量编辑增量 | 重算 22 块，复用 4 块，事件一致，超容差位置 null，NumPy 最大差 0.0 |
| 仅关键帧增量 | 重算 19 块，复用 7 块（10–16），事件一致，NumPy 最大差 0.0 |
| 并发内存 | 1/2/4 worker 峰值均 387600 B，预算 393216 B |
| 分块边界中断 | 第 7 块后退出码 130，无完成报告 |
| 恢复 | 恢复 7 块，后续完成；摘要与全量一致 |
| 坏块恢复 | 仅恢复有效前 4 块；第 4 块摘要失配被拒绝；最终摘要与全量一致 |
| 版本替换 | `E_VERSION`，原块字节不变 |
| 快照合并 | 53 记录、50 条逐通道可定位冲突；墓碑在 432 ms 生效；代数冲突按 `state_generation` |
| 篡改 v2 | `E_DIGEST`，丢弃第 3 代，回退第 2 代；v1 仍有效 |
| 篡改 v1 | `E_DIGEST` |
| 重连 | 20 个缺失姿态按毫秒升序补齐，随后回放 4 条事件 |
| 注入偏差 | `E_REPLAY` 定位 800 ms / `run` / `hips.rotation` / 分量 0，差值 0.25 |

## 复现性说明

- 所有输入、编辑、快照均由固定种子脚本生成，不读取网络或外部资源。
- 姿态比较在帧、骨骼、通道、分量四个层级进行；确定性测试使用零容差摘要比较。
- 增量与全量比较同时记录 Python 标量差和 NumPy 向量差。
- SQLite 文件的物理字节可能随 SQLite 版本产生不同页布局；逻辑分片摘要、元数据摘要、合并结果与回放摘要均由规范化 JSON 内容决定。

