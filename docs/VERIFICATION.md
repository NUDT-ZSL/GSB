# 实际验证记录

验证环境：Linux x86_64，Go 1.21.13，Python 3.11；全部流量限定在 127.0.0.1 回环地址，
仅使用本地样本文件，无容器、无外部网络目标。Windows 产物通过 `GOOS=windows GOARCH=amd64`
交叉编译产出 PE32+ 可执行文件，Windows 本机可直接运行 `scripts/build-windows.ps1`。

## 1. 静态检查与单元测试

命令：

```bash
gofmt -w cmd internal
go vet ./...
go test ./...
```

结果：全部通过。

- `internal/graph`：DAG 构建、确定性 BFS 闭包顺序（A -> [B,C] -> D -> E）、成环输出
  `alpha -> beta -> gamma -> alpha`、缺失依赖引用均按 E_DEPEND 验证。
- `internal/store`：
  - 重复 acquire 返回 E_LOCKED 且携带 holder/到期/版本；错误版本续租返回 E_VERSION；
  - 过期边界 `expires_at_ms <= now_ms` 时续租必败且锁被回收，杜绝续租/回收双写；
  - 释放传播顺序稳定为 B,C,D 且每键只传播一次；force 抢占触发 preempt 传播并升级版本；
  - 快照版本不一致时整体替换，分歧键不残留，恢复后可继续处理新请求；
  - 篡改快照触发 E_REPLAY 并稳定列出差异；损坏文件触发 E_SNAPSHOT。
- `internal/server`：真实回环 TCP 协议链路、20 路并发 acquire 恰好 1 个持有者、
  端口占用返回 E_PORT。

## 2. 端到端演示

命令：`python3 demo/run_demo.py`（退出码 0，完整输出 `artifacts/full-demo.log`，
汇总 `artifacts/demo-report.json`）。

实测要点：

- 正常场景：7 个请求中稳定出现 1 个 E_LOCKED 与 1 个 E_VERSION，随后释放成功、query 为空。
- 依赖传播：释放 `db_primary` 后按
  `cache_layer, db_replica, search_index, report_job` 的 BFS 顺序各产生一条轨迹，
  租约版本统一由 1 递增到 2，事件号/序号/触发源完整记录。
- 抢占场景：`cache_layer` 被 force 抢占后持有者切换、版本升到 2，下游
  `search_index, report_job` 被 preempt 传播失效。
- 崩溃回收：两个 lockctl 子进程被 kill（无退出消息），后台回收器在租约到期后输出：
  - `reclaim key=db_replica holder=crashed-client-0 reason=expired version=1`
  - `reclaim key=cache_layer holder=crashed-client-1 reason=expired version=1`
  观测使用只读 `passive_table`，确保回收由服务端后台路径而非请求惰性回收完成。
- 快照恢复：制造分歧后 restore 识别 `Replaced=true`（OldTableVer 28 -> NewTableVer 27），
  分歧键整体消失而非增量合并，`ReplayDiffs=null`，恢复后新 acquire 正常。

## 3. 异常注入

命令：`python3 demo/run_demo.py --inject all`（退出码 0，报告
`artifacts/fault-injection-report.json`）。

| 注入 | 业务码 | 实测结果 |
| --- | --- | --- |
| 依赖成环样本 `samples/locks.graph.cycle.json` | E_DEPEND | 服务端拒绝启动并输出具体环路 |
| 快照写入非法 JSON | E_SNAPSHOT | restore 稳定返回损坏错误 |
| 篡改快照可信版本基线 | E_REPLAY | 列出 `version_baseline` 差异项（期望 v999，实际 v2） |
| 预置监听占用 127.0.0.1:7813 | E_PORT | bind 失败并返回 E_PORT |

## 4. Windows 产物

```bash
GOOS=windows GOARCH=amd64 go build -o bin/lockd.exe ./cmd/lockd
GOOS=windows GOARCH=amd64 go build -o bin/lockctl.exe ./cmd/lockctl
# file: PE32+ executable (console) x86-64, for MS Windows
```

Windows 上运行方式与 Linux 一致：`python demo\run_demo.py` 与
`python demo\run_demo.py --inject all`，脚本使用标准库 subprocess，进程强杀与端口占用
逻辑均为跨平台实现。
