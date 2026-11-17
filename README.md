# 跨组件平台故障恢复系统（Windows 本地 / 回环 TCP / JSON 行协议）

面向本地模拟组件节点的配置分发与故障恢复平台。每个节点是独立子进程，
通过回环 TCP 上的 JSON 行协议与平台通信；全部状态持久化到本地 SQLite。

## 环境

- Python 3.10+（本机验证：Python 3.11.0rc1）
- `sqlite3` 标准库：可用（SQLite 3.37.2）
- `tkinter`：本环境无 root 权限无法安装 `python3-tk`，但本系统为纯 CLI
  架构，不依赖任何 GUI 组件，不影响交付与验收
- 不使用 Docker / 容器；不访问外部网络，仅 127.0.0.1 回环

## 目录结构

```
platform/
  errors.py      业务码（E_FORMAT/E_PORT/E_VERSION/E_CONVERGE/E_REPLAY）+ 结构化日志
  manifest.py    v1/v2/v3 清单编解码、强校验、规范化摘要、内存升级
  upgrade.py     清单目录逐条升级 + 检查点断点续传 + 只读备份
  plan.py        分发计划编译与期望派生摘要（平台/节点/校验器共用）
  node_sim.py    节点模拟子进程（--fail-attempts/--delay-ms/--downgrade-to）
  distribute.py  并行分发：全局并发上限、单次超时、策略重试、回执收敛
  store.py       SQLite 持久化 + prepared/distributing/committed/failed 状态机
  snapshot.py    会话快照保存/整体替换恢复（版本冲突 -> E_VERSION）
  verify.py      独立恢复一致性校验工具（E_REPLAY）
  cli.py         upgrade / run / verify / snapshot-save / snapshot-restore / inject
samples/         v1/v2/v3 确定性样本、损坏清单、版本冲突快照
tests/           19 个单元/集成测试
```

## 清单格式

- v1：`{"format_version":1,"entries":{k:v}}` 纯键值对
- v2：entries 变为列表，每项增加 `version` 与 `targets`（节点目标）
- v3：增加 `policy`（strategy/max_concurrency/timeout_ms/retries）与
  `derive`（派生关系：node/from/algorithm）

加载时自动检测版本；未知版本或字段缺失以 `E_FORMAT` 整体拒绝并输出字段
路径（如 `$.entries[1].version`）。同一语义清单在不同版本、字段顺序与
空白差异下得到相同规范化摘要（`canonical_digest`，测试
`test_three_versions_same_digest` 验证三份样本摘要一致）。

## 使用

```bash
# 1. 打开清单目录：低版本逐条升级、每条写检查点、旧文件转只读备份
python3 -m platform.cli upgrade --dir work/manifests
python3 -m platform.cli upgrade --dir work/manifests --simulate-kill-after 2  # 模拟强杀

# 2. 并行分发（3 节点，node-2 注入首次超时 -> 重试回执单独记录）
python3 -m platform.cli run --manifest samples/manifest_v3.json \
    --nodes 3 --base-port 19500 --flaky-node node-2

# 3. 独立校验工具：按最近已提交版本重算计划与期望派生摘要并逐项比对
python3 -m platform.cli verify

# 4. 快照保存 / 恢复（清单版本不一致 -> 整体替换；旧快照转只读）
python3 -m platform.cli snapshot-save --manifest samples/manifest_v3.json --out work/snap.json
python3 -m platform.cli snapshot-restore --snapshot work/snap.json --current-manifest-version 2

# 5. 异常注入（仅改输入文件/参数，不改源码；异常后会话可重开重试）
python3 -m platform.cli inject corrupt-manifest --file samples/corrupt_manifest.json   # E_FORMAT
python3 -m platform.cli inject port-busy --port 19500                                  # E_PORT
python3 -m platform.cli inject snapshot-conflict --file samples/conflict_snapshot.json \
    --current-manifest-version 2                                                       # E_VERSION
```

## 业务码链路

| 码 | 触发 | 输出 |
|---|---|---|
| E_FORMAT | 未知版本/字段缺失/JSON 损坏 | 出错字段路径 |
| E_PORT | 节点回环端口被占用 | 端口号 |
| E_VERSION | 快照已提交版本低于当前（降级冲突）；提交跳过未提交版本 | 双方版本号 |
| E_CONVERGE | 派生摘要不一致或节点未回执 | 按节点名稳定排序的 期望值/实际值 列表 |
| E_REPLAY | 配置版本缺失或节点降级导致不可复现 | 具体依据（basis） |

所有异常与关键事件写入 `logs/platform.log`（JSON 行结构化日志）。

## 实际验证结果（2026-11-17，本机）

- `python3 -m unittest discover -s tests`：**19/19 通过**
  - 三版本互操作与规范化摘要一致；字段顺序/空白不变性
  - 损坏清单 E_FORMAT 路径 `$.entries[1].version`；未知版本拒绝
  - 升级强杀（`fail_after=2`）后重启续传，已升级条目不重复处理，备份只读
  - 3 节点并行分发收敛；flaky 节点重试回执 attempt=2 与首次区分记录
  - 节点未回执 -> E_CONVERGE，偏差节点/期望值/实际值稳定排序输出
  - 状态机：重复提交幂等返回 already_committed；版本不可跳过；
    failed->distributing 重试保留完整迁移轨迹
  - 快照整体替换恢复；降级冲突 E_VERSION；冲突后 DB 可重开
  - 校验工具：正常 ok；清单缺失/节点降级 -> E_REPLAY
- CLI 端到端：升级中断恢复（exit=2 -> resume ok -> 幂等空升级）；
  分发 committed（node-2 attempt=2）；verify status=ok；
  三类注入分别以 E_FORMAT/E_PORT/E_VERSION 退出码 1 并写结构化日志
