# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）

环境：WSL Ubuntu 22.04，Python 3.10.12，NumPy 1.26.4。

## 执行命令与结果

```bash
python3 scripts/generate_sample.py --output samples
python3 -m unittest discover -s tests
python3 scripts/run_acceptance.py
python3 -m diagnostic_engine.cli validate --manifest samples/tasks.jsonl
python3 -m diagnostic_engine.cli self-check --state-dir workspace/acceptance/full_state
```

实际结果：

- 单元/集成测试：`Ran 23 tests ... OK`。
- 一键验收：`workspace/diagnostic_report.json` 中 `ok = true`。
- 样本清单校验：`validate True 3 0`。
- 全量样本状态自检：`self-check True 3`。

## 强制终止恢复

验收脚本启动独立子进程，在 `clip_alpha` 第 5 帧写入后、第二个 block 检查点提交前使用 `SIGKILL` 强杀。

`workspace/diagnostic_report.json` 记录：

- `sigkill_returncode = -9`；
- 恢复前检查点版本：`1`；
- 恢复前 `next_frame = 4`；
- 恢复时复用已提交帧：`[0, 1, 2, 3]`；
- 重建/计算帧：`[4, 5, 6, 7, 8, 9, 10, 11]`；
- 已提交帧的噪声生成集合不包含 `[0, 1, 2, 3]`；
- 恢复数组与不间断运行数组逐字节一致：`true`；
- 两侧完整数组 SHA-256 均为：
  `fd41db4c576bbe6aceb6d9527b460fc965e74bb9247247f5a1df96a35578df0f`。

## 缓存失效

定点 revision 提升到第 5 帧时，block 大小为 4，失效起点对齐到第 4 帧：

- 复用集合：`[0, 1, 2, 3]`；
- 重算集合：`[4, 5, 6, 7, 8, 9, 10, 11]`；
- 两个集合并集覆盖全部 12 帧且无交集；
- 最终检查点版本：`5`（初始 3 个 block 版本，失效后两个 block 新版本）；
- 对定点 `noise_seed` 和 `propagation_steps` 变更也有相同前缀复用测试。

## 错误码矩阵

验收脚本实际触发并核对了以下 11 个场景，全部返回预期业务码：

- `E_PROPAGATION_INVALID`
- `E_DUPLICATE_CLIP_ID`
- `E_NOISE_SHAPE_MISMATCH`
- `E_REVISION_ROLLBACK`
- `E_CHECKPOINT_CONFLICT`
- `E_RECOVERY_MISMATCH`
- `E_CACHE_KEY_INVALID`
- `E_INVALIDATION_MISMATCH`
- `E_STATE_INCONSISTENT`
- `E_STAGE_BUSY`
- `E_RESOURCE_LIMIT`

最终报告中的 `latest_error.code` 为 `E_RESOURCE_LIMIT`，来自错误码矩阵最后执行的资源上限用例；完整错误历史写入对应 state 的 `errors/error_log.jsonl`。

## 产物

- 样本清单：`samples/tasks.jsonl`
- 噪声参数：`samples/initial_noise_params.json`
- 最终验收报告：`workspace/diagnostic_report.json`
- CLI 全量运行报告：`workspace/acceptance/full_manifest_report.json`
- SIGKILL 后恢复报告：`workspace/acceptance/sigkill_resume_report.json`
- 自检报告：`workspace/self_check_report.json`
