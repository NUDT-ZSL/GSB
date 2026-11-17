# 技术说明：配置中心热更新中间件 (hotcfg)

## 架构

```
hotcfg/
  errors.py    业务错误码: E_CONFIG/E_COMMIT/E_PUSH/E_SUB/E_SAMPLE/E_VERSION/E_VERIFY/E_REPLAY
  loader.py    配置项/任务/样本数据加载与强校验 (E_CONFIG, details.position 定位)
  center.py    配置中心: 版本链、两阶段原子提交、订阅注册、热更新推送、确认记录
  latency.py   延迟观测: 单调时钟采样, 定点统计 (均值/P95/最大), E_SAMPLE
  tasks.py     计算任务: 按指定版本取完整生效值, NumPy 聚合, 结果摘要 (E_VERSION)
  verify.py    结果一致性校验命令 (E_VERIFY)
  snapshot.py  会话快照持久化与版本识别后的整体替换恢复
  replay.py    恢复后重放校验 (E_REPLAY)
  cli.py       命令行: run / verify / restore / replay / inject
samples/       确定性本地样本: config.json / tasks.json / data.json (等差序列, 无随机源)
tests/         36 个 pytest 用例
```

## 关键设计

- **版本链与原子提交**：`VersionRecord` 不可变，含变更键集合、完整生效值集合、父版本号。
  `stage()` 校验并构造待生效记录，`commit()` 在锁内原子切换当前版本；任何读取
  (`get_effective`) 都返回某版本完整生效值的深拷贝，测试
  `test_no_mixed_state_under_concurrent_commits` 在 1 写 4 读并发下验证无新旧混合。
  暂存/提交失败抛 E_COMMIT 且当前版本不变。
- **订阅与推送**：提交生效后按订阅键集合 ∩ 变更键集合确定推送目标；通知携带版本号与
  变更键集合，任务在 `on_push` 中以一次原子操作取到该版本完整生效值。推送失败记录
  E_PUSH（结构化日志 `push_error` 事件），`retry_push` 只投递当前最新已提交版本，
  任务不会观察到中间版本（`test_push_failure_recorded_and_retry_skips_intermediate`）。
- **延迟观测**：时钟基准 `time.monotonic`（本地单调计数，真值来源），每次推送/查询记录
  起止毫秒差；按 (任务, 版本, 类型) 聚合，整数定点表示（scale=1000，量化误差上界
  0.0005ms）；任一分组样本数 < 3 时输出 E_SAMPLE，不以缺失值参与统计。
- **结果与版本严格对应**：任务按其订阅声明版本运行，不一致即以 E_VERSION 拒绝，
  不会用最新版本静默替代；结果摘要含版本号、参与键、数值口径与定点结果值。
- **一致性校验**：`verify` 按结果摘要版本号重取生效值重算并逐任务比较，首个偏差异
  E_VERIFY 报告任务、期望版本与实际版本。
- **快照与恢复**：快照含版本链摘要(SHA-256)、当前版本、订阅、推送确认、结果摘要与
  延迟统计口径版本 (`latency-stats-v1`)。重开时比较链摘要：一致复用，不一致整体替换
  （配置/订阅/结果），不做增量合并；恢复后可继续提交新版本。
- **重放校验**：`replay` 由父版本生效值+变更键推算每次提交完整生效值、重算推送目标
  集合、任务结果摘要与延迟统计，逐项比较，不可复现以 E_REPLAY 报明依据。
- **异常注入**（仅改变输入文件或参数）：`inject corrupt-config`（截断配置文件→E_CONFIG）、
  `inject missing-sub-ref`（订阅引用缺失→E_SUB）、`inject push-timeout`
  （推送确认超时→E_PUSH），均写入 JSONL 结构化日志。

## 使用

```bash
python -m hotcfg.cli run --commits 4 --queries 5 --snapshot-out logs/snapshot.json
python -m hotcfg.cli verify  --snapshot logs/snapshot.json
python -m hotcfg.cli restore --snapshot logs/snapshot.json --continue-commits 2
python -m hotcfg.cli replay  --snapshot logs/snapshot.json
python -m hotcfg.cli inject corrupt-config|missing-sub-ref|push-timeout
python -m pytest tests -q
```

## 实际验证结果 (2026-11-17, Python 3.11 + NumPy 2.4.6)

- `pytest tests -q`：**36 passed**（覆盖 8 项业务要求与三类异常注入）。
- `run --commits 4 --queries 5`：版本链 v1→v5，12 个任务结果全部通过 `verify`
  （`{"checked": 12, "ok": true}`），推送错误 0；延迟统计按任务聚合输出
  （如 agg_weighted/query: count=24, mean=31, p95=55, max=56，定点 scale=1000，
  量化误差上界 0.0005ms，时钟基准 time.monotonic）。
- `restore`：全新会话链摘要与快照不一致 → `replaced: true` 整体替换，
  恢复后继续提交至 v7。
- `replay`：`{"ok": true, "versions_checked": 5, "push_acks_checked": 12,
  "results_checked": 12, "latency_groups_checked": 6}`。
- 异常注入：corrupt-config → E_CONFIG(position=$)；missing-sub-ref → E_SUB；
  push-timeout → 3 条 E_PUSH 确认超时记录，均写入 logs/inject.jsonl。
- E_SAMPLE：单一样本分组请求统计时返回
  `{"code": "E_SAMPLE", "details": {"min_samples": 3, ...}}`。

注：目标环境为本地 Windows（Python 3.10+，Tkinter 随官方安装包提供，本工具不依赖
GUI）；本次开发与验证在 Linux 环境完成，代码仅用标准库 + NumPy，跨平台可运行。
