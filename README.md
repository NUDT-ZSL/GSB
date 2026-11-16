# 全景分割结果数据处理管线与结果验证工具

面向本地确定性样本，解决像素掩码在 v1 二进制行程包与 v2 base64 JSON 包之间的
互操作与版本兼容。统一中间表示（IR）支撑双向转换、跨格式融合、版本化快照恢复、
增量/全量一致性与一致性验证报告。详细设计见 `docs/ARCHITECTURE.md`，运行步骤见
`docs/RUNBOOK.md`。

## 能力对照

- 双格式解析 + 统一 IR：`panseg/converters.py`、`panseg/v1_package.py`、`panseg/v2_package.py`
- 业务码：`E_RLE_OUT_OF_RANGE`、`E_MASK_LENGTH_MISMATCH`、`E_PALETTE_MISSING`、`E_BOUNDARY_SENSITIVE`
- 双向转换与信息保留：升级推定置信度/crowd/调色板，降级损失写入旁路清单，两档 RLE 无损可逆
- 跨格式融合：同类别 IoU 匹配、置信度加权、三级冲突裁决、crowd 只覆盖不竞争、像素唯一归属
- 指纹与失效：实例缓存键 + 融合依赖指纹，局部失效、未失效实例复用且不重复解码
- 版本化快照：临时目录 + 整体原子替换、manifest 命中判定、强制终止仅按完整快照恢复
- 增量/全量一致：两压缩档 x 两来源 x 五步编辑序列逐字段比对
- 并发与隔离：线程池 + `instance_id::stage` 稳定键，单实例异常不影响其余实例
- 边界诊断：f64 参考值 vs f32 实际判定，区分阈值敏感与策略敏感，旁路可复现

## 快速开始

```bash
pip install numpy pillow
python scripts/generate_samples.py --out sample_data
python -m unittest discover -s tests -v
python scripts/run_acceptance.py --sample sample_data --out validation/acceptance
```

## 实际验证结果（本机 Python 3.11 / NumPy 2.4 / Pillow 13）

- 单元与集成测试：31/31 通过
- 一键验收 `acceptance_report.json`：`passed=true`
- 业务码：三类损坏包分别命中 `E_RLE_OUT_OF_RANGE`、`E_MASK_LENGTH_MISMATCH`、`E_PALETTE_MISSING`
- 边界诊断：IoU 翻转（1677727/2396753@0.7）与覆盖率翻转（1/100@0.01）均记 `E_BOUNDARY_SENSITIVE`
- 一致性矩阵：`c0-v1`、`c0-v2`、`c1-v1`、`c1-v2` 全部 true
- 并发确定性：workers=1 与 workers=8 的像素、融合顺序、冲突与失败清单完全一致
- 信号：SIGTERM 子进程以退出码 0 完成原子提交；硬崩溃模拟无完整快照且可从空状态恢复
