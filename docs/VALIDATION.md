# 实际验证记录

验证日期：2026-11-21，环境：Linux，Python 3.11.15。

## 环境

```text
python3.11 --version
Python 3.11.15

.venv/bin/python -c "import numpy, pytest; ..."
numpy 2.4.6
pytest 9.2.0 / pluggy 1.7.0
```

系统默认解释器是 3.10，因此本项目在 `.venv` 中使用 3.11；依赖通过
`uv pip install --python .venv/bin/python numpy pytest` 从本机缓存安装。

## 自动测试

```text
.venv/bin/python -m pytest -q
55 passed
```

覆盖内容：

- 三个 JSON 文档的唯一性、回环地址、MTU、载荷、链路指标和策略校验；
- 相同输入分片逐字节一致、连续序号、结束标记；
- 乱序、缺片、重复片、冲突片、摘要错误的不同业务错误码；
- 小载荷直发、大载荷聚合、带宽受限回退和固定采样点确定性；
- 真实 UDP/TCP 回显、重组摘要、丢包后的真实等待超时；
- 链路变化触发 MTU 下调、尾部重规划、已发送分片原尺寸重传；
- 重传预算耗尽后的 `E_RETRY_EXHAUSTED` 可诊断失败态；
- CLI 强制终止后的 `inflight` 状态、`resume` 恢复、确认载荷不重复；
- 多进程同一序号抢占、同隧道会话锁、WAL 已提交读隔离、幂等确认；
- 六个 CLI 子命令、稳定排序 JSON、同库重放和双进程全新运行输出一致。

## 端到端固定样本

```bash
.venv/bin/python scripts/echo_server.py \
  --udp-port 39001 --tcp-port 39002 \
  --ready-file data/echo-ready.json

.venv/bin/python -m tunnel_kernel.cli load
.venv/bin/python -m tunnel_kernel.cli tunnel \
  --peer udp-alpha --payload-size 1024 --payload-seed 7
.venv/bin/python -m tunnel_kernel.cli forward \
  --tunnel <TUNNEL_ID> --scenario degrade-udp
.venv/bin/python -m tunnel_kernel.cli status
.venv/bin/python -m tunnel_kernel.cli verify --tunnel <TUNNEL_ID>
```

实测结果要点：

- 1024 字节内层载荷被拆为 3 片，算法选择 `aggregate`；
- `degrade-udp` 在逻辑时刻 4 ms 把 MTU 从 1200 下调到 500；
- 已在途的 0/1 号分片以原 512 字节报文重传，2 号片按 500 MTU 尾部计划；
- `confirmed_bytes=1024`、`confirmed_through=2`、`inflight_ranges=[]`；
- `verify` 的本地重组摘要与回显摘要一致。

## 强制终止恢复

子进程在发送第一份聚合报文后执行 `os._exit(137)`：

- 终止后状态为 `in_progress`，`confirmed_through=-1`，
  `inflight_ranges=[[0,1]]`；
- `resume` 只重发 0/1，然后发送未下发的 2；
- 最终 `recovery_count=1`、确认字节正好 640，无重复计入；
- 同一消息回显端重复报文按幂等处理。

## 双运行一致

```bash
bash examples/repeatability_check.sh
repeatability: OK (11279 bytes identical)
```

该脚本每次启动全新的本地回显服务和临时数据库，连续运行两次库示例并比较
完整 JSON 输出。发送轨迹中的时间全部来自注入逻辑时钟，不含墙钟、PID、
临时路径或随机数。
