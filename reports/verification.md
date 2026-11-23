# 实测验证摘要

执行命令：

```bash
python scripts/run_experiment.py
python -m unittest discover -v
```

环境：Python 3.10.12，NumPy 1.26.4，本地文件系统，16kHz 单声道，20ms 帧长/帧移。

所有检查通过（10/10）：

```text
baseline_self_check                         true
cache_reuse_observable                      true
deterministic_full_rerun                    true
incremental_commit_atomic                   true
mapping_increment_suffix_only               true
reference_increment_propagation             true
snapshot_resume_after_committed_stage       true
snapshot_resume_after_mid_stage_crash       true
snapshot_self_check_cli                     true
truth_observation_frame_counts              true
```

关键观测：

- 基线快照版本：`features=1, timbre=2, alignment=3, params=4, synthesis=5`
- 基线内容缓存：4 次因同参考音色复用命中，28 次首次计算
- 映射参数增量：16 个受影响节点全部重算，其余 24 个节点复用；重算阶段仅为 `params` 与 `synthesis`
- 参考音色增量：`ref-spk-02` 的 revision 1→2 只影响 `utt-001`、`utt-008`，`features` 全部复用
- 阶段提交后强杀：退出码 86，恢复从阶段索引 2 开始
- 阶段中途强杀：退出码 87，恢复仍从阶段索引 2 开始，未提交中间状态未进入快照
- 增量提交中途强杀：退出码 87，旧快照 `version_counter` 保持 5；恢复后 PCM 与不间断增量逐字节一致
- 真值观测：298 帧全部对齐，F0 MAE 39.061433Hz，能量 MAE 0.0181061，音素一致率 0.110738
- 测试套件：32/32 通过

机器可读的完整结果、所有 PCM SHA-256、缓存键与自检明细见 `reports/verification.json`。

