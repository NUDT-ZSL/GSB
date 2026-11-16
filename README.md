# 本地跨组件配置分发平台

面向本地（Windows/类 Unix 均可）环境的配置分发平台：管理一组以独立子进程运行的
组件节点，通过回环 TCP（127.0.0.1，动态端口）上的 JSON 行协议下发版本化配置，
收集回执与派生状态摘要，把整个演进会话持久化到本地 SQLite，并提供恢复重算与
结果验证工具。无 Docker、无外部网络服务依赖。

## 结构

- `configdist/manifest.py` — 版本清单加载与强校验（`E_MANIFEST`，整体拒绝并指出 `$.path` 位置）
- `configdist/registry.py` — 节点注册表加载
- `configdist/planner.py` — 版本演进 → 分发计划（full 全量 / shard 分片）
- `configdist/node_sim.py` — 节点模拟子进程（含故障注入：`crash`/`badmsg`/`wrongdigest`/`drop-first`/`slow`）
- `configdist/scheduler.py` — 子进程编排 + 并行下发（全局并发、单节点超时、同 seq 重试、重试回执区分记录、逐节点开始/结束观测）
- `configdist/statemachine.py` — `prepared → distributing → committed/failed`，迁移记录版本/已确认节点/时间，幂等提交
- `configdist/converge.py` — 回执收敛与派生摘要一致性判定（`E_CONVERGE`）
- `configdist/store.py` — SQLite 快照（清单/计划/回执/迁移/观测/节点状态/已提交版本），整体替换恢复，已提交版本不回退
- `configdist/session.py` — 会话编排：prepare / distribute / recover / resume
- `configdist/replay.py` — 恢复后对最近已提交版本重算计划与期望摘要并逐项比对（`E_REPLAY`）
- `configdist/verify.py` — 结果验证工具（CLI：`python -m configdist.verify <db>`）
- `configdist/web.py` — Flask 状态接口（`/status`、`/verify`）
- `samples/` — 样本清单（v1 全量、v2 分片、非法清单）与节点注册表
- `demo.py` — 固定顺序演示；`tests/` — 28 个 pytest 用例

## 业务码

| 码 | 含义 |
|---|---|
| `E_MANIFEST` | 清单非法，整体拒绝，details 指出 `$.` 错误位置 |
| `E_NODE` | 节点崩溃或协议非法，节点标记 degraded，不阻塞其他节点 |
| `E_CONVERGE` | 回执未收敛/派生摘要不一致，列出偏差节点与版本，禁止部分成功 |
| `E_REPLAY` | 恢复重算不可复现（节点降级/版本缺失），列出节点与配置项 |
| `E_STATE` | 非法状态迁移或提交受阻（附原因） |

## 运行

```bash
python -m venv .venv && .venv/bin/pip install flask pytest   # Windows: .venv\Scripts\pip
.venv/bin/python demo.py            # 三阶段演示，结构化总结按固定顺序输出
.venv/bin/python -m pytest tests -q # 28 个用例
.venv/bin/python -m configdist.verify out/normal.db   # 验证工具，退出码 0/1
.venv/bin/python -m configdist.web out/normal.db      # http://127.0.0.1:8765/status
```

## 实测结果（2026-11-17，Python 3.11）

- `pytest`：28 passed。
- 演示阶段 1：v1 全量下发 3 节点，全部回执收敛，committed=1，replay/verify 均 OK。
- 演示阶段 2：注入 `node-b` 崩溃（`E_NODE`、标记 degraded），模拟平台中断后从
  SQLite 快照恢复为 `distributing` 一致视图；恢复重算与存储回执一致；resume 后
  提交受阻（`E_CONVERGE`，缺失 node-b 回执），committed 保持 0 不回退。
- 演示阶段 3：升级到 v2（分片），快照版本不一致触发整体替换；v2 提交成功，
  重复提交幂等；committed 不允许回退；注入 node-c 降级后 replay(v2) 报
  `E_REPLAY` 并列出节点与配置项，replay(v1) 因版本缺失报 `E_REPLAY`。
