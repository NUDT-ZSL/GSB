# DDP 环形 All-Reduce 通信仿真台 — 技术说明

面向本地环境的分布式数据并行训练通信仿真台：4–8 个独立工作进程通过
`127.0.0.1` 上的 TCP 回环端口组成环形拓扑，执行分块 all-reduce
（reduce-scatter + all-gather），并用归约得到的全局梯度做一步参数更新。

## 目录结构

- `ddp_sim/model.py` — 确定性参数初始化与本地梯度分片生成（SeedSequence
  派生，逐位可复现）；非法种子/维度/进程数返回 `E_INIT`。
- `ddp_sim/worker.py` — 工作进程：TCP 服务、环形状态机 `RingReducer`、
  延迟注入、交错深度信号量、计时与 JSONL 结构化日志。
- `ddp_sim/controller.py` — 控制器：拉起/回收工作进程、版本校验
  （`E_VERSION`）、一致性校验（`E_DIVERGE`）、参数更新、快照恢复。
- `ddp_sim/snapshot.py` — 快照持久化（参数向量、版本号、种子、维度、
  已完成步数、sha256 校验和）；损坏文件返回 `E_SNAPSHOT`。
- `ddp_sim/gui.py` — Tkinter 界面：每进程阶段/收发块数/耗时/参数版本/
  一致性状态，按钮触发单步训练、保存/恢复快照。
- `ddp_sim/cli.py` — 子命令 `run` / `verify` / `ports` / `demo` / `gui`。
- `configs/seeds.json` — 样本随机种子配置；`docs/PORTS.md` — 端口范围说明。

## 一致性设计（关键难点）

- **固定累加顺序**：块 `c` 从 rank `c % N` 注入，沿环单向流转，每个节点
  把"已有部分和 + 本地分片"作为唯一累加次序；浮点加法顺序由环序决定，
  与网络到达时序无关。消息头携带 `order` 字段记录实际累加顺序。
- **单点归约 + 字节广播**：每块只在归约终点节点计算一次最终和，其余
  节点通过 all-gather 接收同一份字节，因此各进程全局梯度逐位一致。
- **阶段推进**：`RingReducer` 状态机强制 reduce 完成（收到 N 份贡献）
  后才允许进入 gather；提前广播、重复 reduce、错位注入均抛 `E_PHASE`。
- **延迟注入**：仅作用于本进程发送线程的调度时序（`time.sleep`），不改
  变消息内容与归约顺序，因此不改变最终字节摘要；真实超时由 socket
  超时独立判定（`E_TIMEOUT`），不由注入延迟伪造。
- **交错深度**：信号量限制每进程"已注入未完成"的块数；环内转发永不阻
  塞，避免深度为 1 时的循环等待死锁。

## 退出与恢复语义

- `SIGTERM`/`SIGINT`：工作进程尽力写入 `exit_report` 日志记录后退出；
  **不承诺**每个进程都生成退出报告。
- `SIGKILL`（强制终止）：无任何退出报告，重启后依赖已持久化快照恢复。
- 恢复快照时先比较版本号/步数，不一致则**整体替换**会话状态（参数、
  版本、种子、维度、步数），不做增量合并。

## 业务码

| 代码 | 含义 | 退出码 |
| --- | --- | --- |
| `E_INIT` | 种子/维度/进程数非法，拒绝启动 | 2 |
| `E_PORT` | 回环端口被占用 | 3 |
| `E_BLOCK` | 块大小超过分片维度 | 4 |
| `E_SNAPSHOT` | 快照文件损坏/校验失败 | 5 |
| `E_PHASE` | 阶段违规推进 | 6 |
| `E_DIVERGE` | 全局梯度字节摘要分叉 | 7 |
| `E_VERSION` | 参数版本落后/超前 | 8 |
| `E_TIMEOUT` | 环消息真实超时 | 9 |

## 常用命令

```bash
python3 -m ddp_sim run --config configs/seeds.json --steps 1
python3 -m ddp_sim run --workers 6 --dim 512 --seed 7 --block-size 64 \
    --interleave 3 --delay 0=50 --delay 3=120 --log-dir runs/delayed
python3 -m ddp_sim verify --log-dir runs/delayed     # 一致性校验命令
python3 -m ddp_sim run --inject port-busy            # -> E_PORT
python3 -m ddp_sim run --inject bad-block            # -> E_BLOCK
python3 -m ddp_sim run --inject corrupt-snapshot     # -> E_SNAPSHOT
python3 -m ddp_sim demo                              # 三场景可复现演示
./scripts/run_gui.sh                                 # Tkinter 界面
python3 -m pytest tests/ -q                          # 测试
```

异常注入仅通过命令行改变输入/参数（预占端口、非法块大小、写入损坏
快照文件），不修改源码制造失败。

## 验证记录（2026-11-17 实测）

- `pytest tests/`：36 项全部通过（确定性分片、环形归约字节一致、延迟/
  交错深度不变性、快照整体替换、E_INIT/E_PORT/E_BLOCK/E_SNAPSHOT/
  E_PHASE/E_VERSION/E_DIVERGE 各业务码）。
- `python3 -m ddp_sim demo`：正常步与延迟步 `grad_digest` 相同
  （`0b515d26…`）；kill -9 后从快照 version=2 整体替换恢复并继续到
  version=3，输出顺序稳定。
- GUI 冒烟（本机 X :0）：界面触发单步训练，4 进程状态推进至 done，
  一致性显示"一致 (version=1)"。
- 环境：Python 3.11 + numpy 2.4.6 + pytest 9.2.0；tkinter 8.6 经本地
  解包 deb 提供（见 `scripts/env.sh`）。
