# 本地隧道路径裁决内核

一个可集成的 Python 3.11+ 组件，用于在本地回环 UDP/TCP 回显服务上验证：

- 隧道对端、链路画像和转发策略的严格校验；
- 确定性分片、聚合封装与按序重组；
- 小载荷逐包直发、大载荷聚合发送的自适应裁决；
- 链路变化触发的 MTU 下调和确定性重传；
- 注入逻辑时钟驱动的发送、退避与轮次预算；
- SQLite 原子水位、强制终止恢复、多进程会话锁与幂等确认；
- `load/tunnel/forward/resume/status/verify` 六个 CLI 子命令。

不依赖 Docker、真实 VPN 协议栈或外部网络。设计细节见
[`docs/DESIGN.md`](docs/DESIGN.md)，实测记录见
[`docs/VALIDATION.md`](docs/VALIDATION.md)。

## 目录

```text
samples/                 三个固定 JSON 样本
scripts/echo_server.py   本地 UDP/TCP 回显服务启动器
tunnel_kernel/           可集成库与 CLI
tests/                   55 项验收测试
examples/                库示例、CLI 示例、双运行一致性检查
docs/                    设计与验证说明
```

## 环境

要求 Python 3.11+、NumPy、pytest：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

若系统 Python 受 PEP 668 管理，按发行版要求使用虚拟环境，不要直接修改
系统 site-packages。样本使用 `127.0.0.1:39001/39002`。

## 快速开始

终端 A 启动回显服务：

```bash
.venv/bin/python scripts/echo_server.py \
  --udp-port 39001 --tcp-port 39002 \
  --ready-file data/echo-ready.json
```

终端 B 执行固定样本：

```bash
.venv/bin/python -m tunnel_kernel.cli load

.venv/bin/python -m tunnel_kernel.cli tunnel \
  --peer udp-alpha --payload-size 1024 --payload-seed 7
# 从输出取 TUNNEL_ID

.venv/bin/python -m tunnel_kernel.cli forward \
  --tunnel TUNNEL_ID --scenario degrade-udp

.venv/bin/python -m tunnel_kernel.cli status
.venv/bin/python -m tunnel_kernel.cli verify --tunnel TUNNEL_ID
```

或直接运行完整示例：

```bash
bash examples/run_demo.sh
bash examples/repeatability_check.sh
```

`forward` 在已完成隧道上重放时返回持久化的最终发送轨迹，因此重复执行输出
一致。`repeatability_check.sh` 则用两个全新回显进程和临时数据库验证全新运行
也逐字节一致。

## CLI

| 子命令 | 作用 |
|---|---|
| `load` | 校验并输出规范化后的三个文档，不写数据库 |
| `tunnel` | 生成确定性载荷、分片计划、算法裁决并持久化为 `prepared` |
| `forward` | 执行已准备隧道；可指定链路变化场景 |
| `resume` | 原子恢复 `inflight` 为 `pending`，只重传未确认分片 |
| `status` | 稳定排序 JSON：MTU、确认水位、在途/待发区间、重传计数、算法 |
| `verify` | 校验本地 SQLite 状态，并默认查询回显服务的重组摘要 |

公共参数：

```text
--inventory samples/peers.json
--links samples/links.json
--policy samples/policy.json
--db data/tunnel_state.sqlite3
```

非法配置返回非零退出码和稳定 JSON 错误，例如：

```json
{"code": "E_BAD_MTU", "message": "..."}
```

失败检查点只在验证通过后才创建数据库，因此非法文档不会写入任何持久化状态。

## 库接口

```python
from dataclasses import replace
from pathlib import Path

from tunnel_kernel import (
    ForwardEngine, LoopbackEchoService, Store,
    build_plan, load_config, query_echo, synthetic_payload,
)

bundle = load_config("samples/peers.json", "samples/links.json", "samples/policy.json")
service = LoopbackEchoService(udp_port=0, tcp_port=0)
ports = service.start()
peer = replace(bundle.peers["udp-alpha"], port=ports["udp_port"])
payload = synthetic_payload(peer.name, 1024, 7)
plan = build_plan(
    peer=peer, payload=payload,
    policy=bundle.policy, link=bundle.links[peer.name],
)

store = Store(Path("data/example.sqlite3"))
store.initialize()
store.prepare_tunnel(
    peer=peer, payload=payload, policy=bundle.policy,
    link=bundle.links[peer.name], algorithm=plan.decision.algorithm,
    mtu=plan.mtu, message_id=plan.message_id,
    fragments=plan.fragments, clock_start_ms=bundle.policy.clock_start_ms,
)

result = ForwardEngine(
    store=store, peer=peer, link=bundle.links[peer.name],
    policy=bundle.policy,
    scenario_changes=bundle.scenarios["degrade-udp"].changes,
).forward(plan.tunnel_id)

echo = query_echo(peer, bundle.policy, message_id=plan.message_id)
local = store.verify_local(plan.tunnel_id)
service.stop()
```

完整可运行版本：`examples/library_demo.py`。主要导出接口：

- `load_config` / `validate_documents`
- `fragment_payload` / `parse_fragment` / `reassemble_packets`
- `build_plan` / `decide_algorithm`
- `Store` / `ForwardEngine`
- `LoopbackEchoService` / `make_transport` / `query_echo`

## 样本语义

`peers.json` 每个对端包含唯一名称、回环监听地址、协议、外层 MTU、内层载荷
长度分布、权重和加密占位标记。`links.json` 提供画像及按逻辑时刻生效的场景；
`policy.json` 提供分片阈值、并发上限、轮预算、重传预算、容差、时钟步进和
显式采样点。

只有字面回环 IP（如 `127.0.0.1` 或 `::1`）被接受；主机名不被接受。样本端口
必须与服务监听一致。

## 关键语义

### 尺寸与分片

```text
capacity = min(outer_mtu, fragment_threshold_bytes) - 64 - crypto_overhead
```

同一输入的分片头、原始报文和 SHA-256 完全确定。加密标记只增加确定性的占位
字节，不做真实加密。聚合报文 `TKA1` 把多个分片装入一个不超过当前 MTU 的
外层数据报。

### 算法裁决

`wire_load <= fragment_threshold_bytes` 时选择 `direct`；高负载且聚合窗口
至少为 2 时选择 `aggregate`；带宽、MTU 或并发只允许窗口 1 时回退 `direct`。
输出包含触发条件、被否决原因、观测项、聚合窗口和每个配置采样点的确定性选择。

### 链路变化与重传

丢包率或 RTT 相对变化超过容差阈值时，按步长下调 MTU，只重新规划“从未发送”
的尾部；已经发送但未确认的分片保留原始 BLOB 并按原尺寸重传。尝试次数超过
重传预算时，隧道原子进入 `failed` 并返回 `E_RETRY_EXHAUSTED`。

### 逻辑时钟

发送时刻、聚合窗口、MTU 变化生效点、退避和轮预算全部只依赖注入的逻辑
毫秒值。I/O 等待使用真实非阻塞超时和主动中止事件；墙钟/随机数/PID 不进入
发送轨迹。

### 持久化与恢复

SQLite 使用 WAL、`BEGIN IMMEDIATE` 和文件锁。分片状态原子地在
`pending → inflight → confirmed` 间迁移；确认日志主键保证幂等。强制终止后，
`resume` 将 `inflight` 恢复为 `pending`，保留已确认水位与原分片字节，只重传
未确认部分。

同一隧道整个发送会话持有一个 advisory file lock，其他实例会收到
`E_TUNNEL_LOCKED`；不同隧道可并行。读取方通过 WAL 只能看到完整已提交水位。

## 业务错误码

| 错误码 | 场景 |
|---|---|
| `E_BAD_ENDPOINT` | 非回环地址、非法端口/协议 |
| `E_BAD_MTU` | MTU 非正、过小、场景 MTU 不可封装 |
| `E_BAD_PAYLOAD` | 空载荷、非正长度、超过封装上限 |
| `E_DUPLICATE_PEER` | 对端名重复 |
| `E_BAD_LINK` / `E_BAD_POLICY` | 画像或策略字段非法 |
| `E_NO_PROFILE` | 对端缺少链路画像 |
| `E_OUT_OF_ORDER` | 严格顺序模式的乱序片 |
| `E_MISSING_FRAGMENT` | 缺少中间序号 |
| `E_DUPLICATE_FRAGMENT` | 严格模式的重复片 |
| `E_CONFLICTING_FRAGMENT` | 同序号不同字节 |
| `E_DIGEST_MISMATCH` | 整体摘要或分片元数据不一致 |
| `E_RETRY_EXHAUSTED` | 重传预算耗尽，隧道进入失败态 |
| `E_TUNNEL_LOCKED` | 另一进程持有同一隧道发送会话锁 |
| `E_SEQUENCE_INFLIGHT` | 同一序号已被并发实例抢占 |
| `E_ABORTED` | 主动中止 |
| `E_STATE_CORRUPT` | 持久化状态不自洽 |
| `E_ECHO_MISMATCH` | 本地摘要与回显摘要不一致 |

## 验证

```text
$ .venv/bin/python -m pytest -q
55 passed

$ bash examples/repeatability_check.sh
repeatability: OK (11279 bytes identical)
```

详细实测记录见 [`docs/VALIDATION.md`](docs/VALIDATION.md)。
