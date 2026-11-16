# locklease：本地分布式锁与租约中间件实验

面向本地 Windows 环境的实验项目：以本地文件持久化锁记录与租约状态，通过回环 TCP 上的
JSON Lines 协议向多个本地客户端子进程提供加锁、续租、释放语义，并支持依赖图失效传播、
客户端崩溃回收、会话快照整体替换恢复与重放一致性校验。不使用任何容器。

## 目录结构

```
cmd/lockd            服务端入口（回环 TCP + 后台回收器）
cmd/lockctl          客户端 CLI（单次请求或 --batch 批量 JSONL）
internal/model       持久化模型（锁、轨迹、回收、状态文件、快照）
internal/graph       依赖图：DAG 校验、环路输出、确定性 BFS 下游闭包
internal/store       锁状态机：租约判定、版本、传播、快照、重放
internal/server      JSON Lines 协议、连接管理、E_PORT 绑定错误
internal/proto       请求/响应结构
samples/             确定性样本图（含成环样本）与客户端 JSONL 输入
demo/run_demo.py     Python 编排：四个场景 + 三类异常注入 + 结果汇总
scripts/             本机/Windows/交叉编译脚本
docs/DESIGN.md       设计与一致性说明
data/ logs/ artifacts/ 运行期目录（状态、快照、日志、报告）
```

## 环境准备

- Go 1.21+（Windows 命令行可执行 `go version`）
- Python 3.10+（仅用于 `demo/run_demo.py` 编排与汇总）
- 确认回环端口 7801/7811/7812/7813 未被占用

### Windows 本机构建（PowerShell）

```powershell
go version
.\scripts\build-windows.ps1
```

产物为 `bin\lockd.exe` 与 `bin\lockctl.exe`。

### Linux/macOS 构建或交叉编译 Windows 产物

```bash
bash scripts/build.sh                 # 本机二进制 bin/lockd bin/lockctl
bash scripts/cross-build-windows.sh   # 交叉编译 bin/lockd.exe bin/lockctl.exe
```

## 运行完整演示

```bash
# Linux/macOS
bash scripts/build.sh
python3 demo/run_demo.py

# Windows
python demo\run_demo.py
```

演示顺序固定：
1. 正常加锁、`E_LOCKED` 重复获取、`E_VERSION` 版本不匹配、释放；
2. 释放上游锁触发确定性依赖失效传播（含完整轨迹与版本递增）；
3. `force` 抢占触发 preempt 传播；
4. 两个客户端子进程被强杀，服务端在租约到期后自动回收（记录原因与租约版本）；
5. 快照保存、制造分歧、版本不一致时整体替换恢复并通过重放校验，恢复后继续服务新请求。

汇总写入 `artifacts/demo-report.json`，完整输出写入 `artifacts/full-demo.log`。

### 异常注入（稳定业务码）

```bash
python3 demo/run_demo.py --inject cycle      # E_DEPEND（输出具体环路）
python3 demo/run_demo.py --inject snapshot   # E_SNAPSHOT（损坏文件）+ E_REPLAY（篡改版本）
python3 demo/run_demo.py --inject port       # E_PORT（端口占用）
python3 demo/run_demo.py --inject all        # 三项顺序执行，报告见 artifacts/
```

## 协议示例（JSON Lines，一行一个请求/响应）

请求字段：`op, key, holder, lease_version, ttl_ms, now_ms, force, snapshot_id, path`。

```bash
bin/lockctl acquire key=db_primary holder=w1 ttl_ms=10000 now_ms=2000000
bin/lockctl acquire key=db_primary holder=w2 ttl_ms=10000 now_ms=2000000   # E_LOCKED
bin/lockctl renew   key=db_primary holder=w1 lease_version=1 ttl_ms=3000 now_ms=2001000
bin/lockctl release key=db_primary holder=w1 lease_version=1 now_ms=2002000
bin/lockctl query   key=db_replica now_ms=2002000
bin/lockctl table
bin/lockctl snapshot snapshot_id=session-A
bin/lockctl restore path=data/session-snapshot.json
bin/lockctl shutdown
# 批量：bin/lockctl --batch samples/requests/scenario2_propagation.jsonl
```

`now_ms` 为可选注入时间（毫秒）；省略时以服务端墙钟为唯一判定源。过期判定为
`expires_at_ms <= now_ms`（包含边界）。

## 测试

```bash
go vet ./...
go test ./...
```

## 持久化文件

- `data/lock-state.json`：每次变更原子覆盖写的锁状态（表版本、图版本、活动锁）。
- `data/session-snapshot.json`：完整会话快照（图、锁表、每键版本、传播轨迹、回收记录）。
- `logs/lockd.log`：服务端回收日志（持有者、原因、租约版本）。

设计细节与一致性论证见 `docs/DESIGN.md`。
