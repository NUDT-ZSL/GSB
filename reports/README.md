# 验证报告

- `experiment_report.json`：16 页确定性样本的全量基线报告，含真值校验和规模决策。
- `resume_reuse_report.json`：对项目内持久快照 `snapshots/experiment` 的完全复用运行，逐页四阶段均未重复检测。
- `incremental_final_report.json`：阈值、分栏间距、排序规则、单页替换与页面增删后的最终增量报告。
- `full_rebuild_final_report.json`：使用相同最终输入，从空白快照全量重建的报告。
- `acceptance_report.json`：14 项验收检查摘要、环境信息、失败码和耗时。
- `signal_cli_test.json`：真实 CLI 子进程在 128 页输入上收到 SIGINT 后的报告与清单进度。
- `unittest_results.txt`：`unittest` 的完整实际输出。

增量与全量的一致性比较针对逐页块、角色、栏组、阅读序号和诊断摘要；缓存复用清单与运行耗时属于运行诊断，不参与该比较。
