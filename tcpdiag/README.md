# TCP 连接状态与拥塞控制诊断工作台（tcpdiag）

面向本地 Windows 环境的教学用诊断工作台：本地回环（127.0.0.1 高位端口）上运行
确定性模拟对端，客户端侧实现 TCP 状态机 + 拥塞控制 + RTT 估计，支持会话快照
持久化、恢复后继续交互与一致性重放校验。仅使用 Python 标准库
（socket / threading / json / tkinter / unittest），不访问外网。

## 运行环境

- Python 3.11+；Windows 官方安装包默认包含 tkinter。
- 本仓库无任何第三方依赖，不需要 Docker。

## 目录结构

```
tcpdiag/
  run_demo.py            # 三场景确定性演示（命令行）
  workbench/
    clock.py             # 时钟基准：monotonic / deterministic
    statemachine.py      # TCP 状态机（E_STATE 拒绝非法迁移）
    congestion.py        # cwnd / ssthresh 整数推进
    rtt.py               # SRTT/RTTVAR/RTO 整数定点估计
    core.py              # 会话核心：状态机+拥塞+RTT 绑定推进
    peer.py              # JSON 脚本驱动的确定性模拟对端
    client.py            # 回环 socket 传输绑定
    snapshot.py          # 快照保存/恢复（版本识别、整体替换）
    replay.py            # 恢复后重放校验（E_REPLAY 定位）
    gui.py               # Tkinter 界面：python -m workbench.gui
  scripts/               # 对端行为脚本（真值来源）
  tests/                 # unittest 测试（30 例）
```

## 快速开始（Windows）

```
cd tcpdiag
python run_demo.py                          # 三场景演示，输出稳定总结
python -m unittest discover -s tests -t .   # 运行全部测试
python -m workbench.gui                     # 启动 Tkinter 界面
```

GUI 操作顺序：选择对端脚本 -> 启动本地对端 -> 连接(三次握手) -> 发送数据 ->
可随时 保存快照 / 恢复快照 / 重放校验 -> 关闭连接。界面展示顺序固定：
TCP 状态、拥塞窗口、慢启动阈值、平滑 RTT、RTO、已发送未确认、事件轨迹。

## 时钟基准与真值来源

- 时钟基准在每个快照中以 `clock_base` 显式声明：
  - `monotonic`：`time.monotonic` 真实单调时钟（GUI 在线诊断）；
  - `deterministic`：确定性虚拟时钟（演示/测试/重放）。对端报文携带虚拟延迟
    `delay_ms`，到达时刻 = max(当前时刻, 发送时刻 + delay_ms)，时钟只前进不后退。
- 真值来源：对端行为脚本（`scripts/*.json`）+ 对端运行时 `truth_log`
  （收/发/丢弃的完整报文序列）。重放校验以快照记录的输入序列与检查点为准。

## RTT 估计公式与缩放因子

RFC 6298 风格，alpha=1/8、beta=1/4，全整数定点：

```
首样本:  SRTT = R,  RTTVAR = R/2
后续:    RTTVAR = (3/4)*RTTVAR + (1/4)*|SRTT - R|
         SRTT   = (7/8)*SRTT   + (1/8)*R
RTO    = SRTT + max(G, 4*RTTVAR)，夹取到 [min_rto_ms, max_rto_ms]
```

- 缩放因子：SRTT 以 q8（×8）保存，RTTVAR 以 q4（×4）保存，全部为整数移位运算。
- 显式规则：样本不足时 RTO 取 `initial_rto_ms`；非正样本（时钟回拨）记录
  `rtt_rejected` 事件并丢弃（估计器同时抛出 E_CLOCK）；重传段的确认按 Karn
  规则排除，记录 `karn_excluded` 事件。

## 拥塞控制整数规则

- 慢启动（cwnd < ssthresh）：每确认 1 字节 cwnd += 1。
- 拥塞避免：cwnd += max(1, MSS*acked/cwnd)（每往返约 +1 MSS 的整数近似）。
- 重复确认达 3 次：快速重传最早已发送未确认段，ssthresh = max(cwnd/2, 2*MSS)，
  cwnd = ssthresh。
- RTO 超时：重传最早已发送未确认段并重置定时器，ssthresh = max(cwnd/2, 2*MSS)，
  cwnd = 1*MSS，回到慢启动。
- 累计确认确认所有 <= 该序号的段；重复确认不推进确认点；确认号越界报 E_SEQ。

## 快照与重放

- 快照内容：格式版本、会话配置（含 config_version）、TCP 状态、cwnd/ssthresh、
  SRTT/RTTVAR/RTO、序号空间（send_una/next_seq）、在途段、事件轨迹、输入序列
  与逐输入检查点。
- 恢复策略：`snapshot_version` 不符 -> E_VERSION 拒绝；`config_version` 不符 ->
  整体替换（配置与状态全部采用快照，记录 `config_replaced`），绝不增量合并；
  恢复后可继续推进新的报文交互。
- 重放校验：从快照的初始配置与确定性时钟重建会话，按记录的输入时间戳逐步
  重放，逐检查点比对状态/cwnd/ssthresh/SRTT/RTO；首个不一致输出 E_REPLAY
  并定位输入序号、关联报文序号与字段名。

## 实际验证结果（本机，Python 3.11）

`python -m unittest discover -s tests -t .`：

```
Ran 30 tests in 1.579s
OK
```

`python run_demo.py`（连续两次运行输出逐字节一致）：

```
[scenario=normal] state=CLOSED cwnd=4485 ssthresh=4000 srtt_ms=10 rto_ms=100 send_una=4001 unacked=0 events=25 checkpoints=11 result=OK
[scenario=loss] state=CLOSED cwnd=2000 ssthresh=2000 srtt_ms=6 rto_ms=100 send_una=6001 unacked=0 events=42 checkpoints=17 fast_retransmit=1 timeouts=2 karn_excluded=2 result=OK
[scenario=snapshot] state=CLOSED cwnd=8000 ssthresh=65535 srtt_ms=6 rto_ms=100 send_una=4001 unacked=0 events=26 checkpoints=11 replay=OK(checked=5) version_replace=OK tamper=E_REPLAY(index=1,field=cwnd) result=OK
DEMO COMPLETE
```

注：本开发容器未安装 tkinter（Linux 最小环境），GUI 在 Windows 官方
Python 上运行；`workbench/gui.py` 已通过语法编译检查。
