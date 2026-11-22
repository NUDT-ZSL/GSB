# 跨文档事件共指链工作台运行报告

- 生成时间：`2026-11-22T17:02:30.058110+00:00`
- 执行环境：Python `3.10.12` / Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.35
- 依赖版本：Flask `3.0.3`，NumPy `1.26.4`
- 验证载体：WSL2 Linux（非 Docker/容器编排）；仅绑定本地回环地址。
- Windows 原生环境可按 README 的 PowerShell 命令启动，不使用外部数据服务。

## 1. 验证结论

- 自动化测试：`Ran 18 tests in 5.750s`，结果 **PASS**。
- 测试命令：`/home/zsl/.feedback/python-base/bin/python3 -m unittest discover -s tests -v`
- 最终增量/全量一致性：**PASS**。
- 最终链数：3；局部事件数：7；仲裁记录：3 条。
- 增量快照版本：链状态 v8，可信度表 v8，仲裁记录 v8。

## 2. 确定性样本

- 固定种子：`eventgraph-2026`；词表版本：`vocab-2026.11.23.1`。
- 文档清单哈希：`655eb4481351bf78781dd0c39ae92d0a734a846cb0cbc22fc65d912af76d8479`。

| 文档 | 来源 | 内容 SHA-256 | 文件 SHA-256 |
|---|---|---|---|
| D01 | provincial_emergency | `22a5b317e054ade4a0c5…` | `7715bdcbc09a7057df6d…` |
| D02 | local_daily | `68221ad3876fbe12f736…` | `87535360a6a91d57a146…` |
| D03 | city_weather | `bac940ae5ee3bc3c775d…` | `5c12083c720816eb4976…` |
| D04 | provincial_emergency | `9f64cf8c4e91376d6d4c…` | `67f25935ab1c8ed7d4a2…` |
| D05 | national_news | `61c3cf2c02781d2d3ddf…` | `eb6303e275ce2e176163…` |
| D06 | city_weather | `bc9764d562b9dca2418b…` | `247d4773349e4f7e68ff…` |
| D07 | national_news | `b00e2e6af2eb938d2397…` | `8c5ad0e3fb21da069de3…` |

## 3. 增量合并与链间仲裁

- 冲突事件：`D04:E01`。
- 候选链 A：`CH-D01-E01`，证据强度 `0.754`，链规模 `2`。
- 候选链 B：`CH-D03-E01`，证据强度 `0.61025`，链规模 `1`。
- 仲裁结果：并入 `CH-D01-E01`。
- 拒绝方向：`CH-D03-E01`，理由：链证据强度较低。
- 该阶段复用链：CH-D03-E01；重算链：CH-D01-E01。

链内论元矛盾示例：

- `CH-D01-E01`：时间=2024年7月21日；主体=海燕；地点=临海市；机构=省应急管理厅
- `CH-D03-E01`：时间=2024年7月22日；主体=山陀儿；地点=青岚县；机构=市应急管理局
- `CH-D06-E01`：时间=2024年7月23日；地点=青岚县；机构=青岚县防汛办

## 4. 资源降级

- 文档：`D07`。
- 降级类别：`HIGHER_SIMILARITY_FLOOR`。
- 常规下限 `0.56`，实际下限 `0.9`。
- 计划比较 `6` 对，精算 `1` 对，预算上限 `3`。
- 扫描方式：`exact_after_upper_bound_pruning`；复用链：CH-D01-E01, CH-D03-E01。

## 5. 失效传播与后缀重算

- 操作：修订 `D06` 的句子模板并重算。
- 起点：`D06`；重算文档：D06, D07。
- 重算链：CH-D06-E01。
- 复用链：CH-D01-E01, CH-D03-E01。
- 受影响候选对：6 个。
- 缓存命中：113；缓存未命中：35；内容对象复用：43。

## 6. 一致性与错误码探针

- 候选证据复算数：21。
- 事件扫描覆盖数：7。
- 全量重建链数：3。
- 回退探针：`E_SNAPSHOT_ROLLBACK`。
- 损坏探针：`E_SNAPSHOT_CORRUPT`。
- 并发占用探针：`E_STAGE_BUSY`。

## 7. 复现命令

```bash
python3 scripts/generate_samples.py
python3 -m unittest discover -s tests -v
python3 scripts/run_validation.py
python3 app.py
# 浏览器打开 http://127.0.0.1:5000
```

## 8. 测试输出尾部

```text
test_resource_limit_and_degraded_merge (test_workbench.SnapshotAndResourceTests) ... ok
test_snapshot_whole_replace_rollback_and_corruption (test_workbench.SnapshotAndResourceTests) ... ok
test_stage_busy_returns_immediately_without_state_change (test_workbench.SnapshotAndResourceTests) ... ok

----------------------------------------------------------------------
Ran 18 tests in 5.750s

OK
```

## 9. 环境偏差

- 任务建议 Python 3.11+；当前执行容器提供 Python 3.10.12。
- 实现仅使用 Python 3.10 可用语法，并已在该版本完成全部验证；Flask 3.0.3 与 NumPy 1.26.4 均为本地已安装版本。
- 本次验证在 WSL2 Linux 中进行；应用只依赖 Flask 与标准库，Windows Python 3.11+ 可直接运行同一入口。
