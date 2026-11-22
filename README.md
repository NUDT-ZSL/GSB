# HotGray 模型分片热更新与灰度工作台

一个完全本地、确定性、可中断恢复的模型权重分片热更新实验台。项目在注入逻辑时钟上复现：

1. 增量补丁物化与逐层逐分片目录一致性校验；
2. 模型版本、层分片、预处理、后处理、特征表、上游模型之间的失效闭包；
3. 预热状态机、灰度档位推进、请求版本快照与回退裁决；
4. SQLite 事务、跨进程文件锁、幂等提交和强制终止后的步骤重放；
5. 不依赖 CDN 的本地可视化诊断工作台。

不使用 Docker、GPU、线上模型仓库或付费资源。推理只使用 NumPy CPU 运算和确定性样本。

## 实际环境

本次实际验证环境来自仓库容器的 Linux/WSL 分支：

- Python `3.12.13`（项目 `.venv`）
- NumPy `2.5.5`
- SQLite `3.53.1`
- WSL2 / Ubuntu 22.04

系统最初只有 Python 3.10，因此使用 `uv` 创建了 Python 3.12 虚拟环境。项目声明
`requires-python = ">=3.11"`。Windows 兼容代码已通过 `fcntl` / `msvcrt` 分支和仅标准库
路径实现，但当前容器不是 Windows，无法替代 Windows 实机验收。Windows 启动方式如下：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m hotgray validate --samples samples
```

## 快速开始

Linux / WSL：

```bash
uv venv --python 3.12 .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/generate_samples.py
.venv/bin/python -m hotgray validate --samples samples
.venv/bin/python -m hotgray demo --reset --samples samples
.venv/bin/python -m hotgray serve --db artifacts/demo-workbench/state.db --port 8088
```

打开 `http://127.0.0.1:8088/` 查看诊断工作台。`demo` 会执行 `samples/clock_steps.json`
中的 `normal` 场景，最终状态为 `released`，并写出 `artifacts/demo-report.json`。

测试：

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

## 样本

`samples/` 中的内容全部由 `scripts/generate_samples.py` 确定性生成：

- `models.json`：版本 1/2，三层八个分片；配置引用链包含预处理、后处理、特征表、上游模型。
- `weights/v1`、`weights/v2`：真实二进制 float32 分片。
- `patches/v1_to_v2.json`：只声明真正变化的 `encoder.1#1` 与 `head#0`。
- `replicas.json`：`replica-a`、`replica-b` 为灰度成员，`replica-control` 为非灰度对照。
- `plan.json`：`0.1 -> 0.5 -> 1.0` 三档灰度，逻辑时钟从 `1000 ms` 开始。
- `clock_steps.json`：`normal`、`rollback`、`rollback_missed`、`warmup_timeout` 四个场景。

目标版本复用了 `post:v1`，其余配置引用新版本；因此补丁提交后可以同时观察分片失效、
配置失效和未变化配置的安全复用。

## CLI

```bash
# 只读校验，不建立数据库
python -m hotgray validate --samples samples

# 初始化持久化状态
python -m hotgray init --db workbench/state.db --samples samples

# 增量补丁 / 预热 / 逻辑时钟推进 / 灰度
python -m hotgray apply-patch --db workbench/state.db --at-ms 1000
python -m hotgray warmup --db workbench/state.db --at-ms 1000
python -m hotgray tick --db workbench/state.db --at-ms 1100
python -m hotgray stage --db workbench/state.db --at-ms 1200 --level 1

# 请求绑定与完成
python -m hotgray request --db workbench/state.db --at-ms 1210 \
  --replica-id replica-a --request-id req-1 --input '[1.0, 0.5, -0.25]'
python -m hotgray complete --db workbench/state.db --at-ms 1220 --request-id req-1

# 人工中止 / 恢复 / 报告
python -m hotgray abort --db workbench/state.db --at-ms 1320 --reason injected_failure
python -m hotgray resume --db workbench/state.db --at-ms 1420
python -m hotgray report --db workbench/state.db --output artifacts/report.json

# 逻辑时钟脚本与 Web 工作台
python -m hotgray run-clock --db workbench/state.db --scenario rollback
python -m hotgray serve --db workbench/state.db --port 8088
```

强制终止恢复测试使用：

```bash
python -m hotgray apply-patch --db state.db --at-ms 1000 \
  --crash-after-materialize 5
```

该参数只用于故障注入。进程以 `os._exit(70)` 结束，不保证输出退出报告；再次执行
`resume` 会依据 SQLite 中的步骤水位只重放未完成步骤。

## 关键设计

- **原子状态**：分片目录、已提交版本组合、失效裁决、轮次水位和步骤日志在同一个
  `BEGIN IMMEDIATE` 事务中提交。
- **内容寻址**：物化路径为 `blobs/sha256/<digest>.bin`，`materializations.digest` 主键
  保证同一摘要只登记一次。
- **请求不可变**：请求开始时序列化版本、完整分片摘要链、配置资源摘要与参数快照。
  进行中的请求不从运行中版本读取分片或配置。
- **确定性路由**：`sha256(request_id)` 映射到 `[0,1)`，与当前档位比例比较，不使用随机数。
- **逻辑时钟**：业务时间只来自 CLI/API 传入的 `at_ms`。SQLite 锁等待与 HTTP 服务内部
  调度不是业务时间。HTTP 服务移除了标准库默认的墙钟 `Date` 头。
- **回退**：失败时刻 + 回退窗口是截止边界；超过边界仍未恢复时写入
  `rollback_not_achieved`，同时把副本切回基线，不报告成功。

更多细节见 [架构说明](docs/architecture.md) 与 [验证记录](docs/verification.md)。

## 目录

```text
hotgray/
  models.py       输入解析与业务校验
  storage.py      SQLite schema、WAL、事务与文件锁
  shards.py       内容寻址物化、增量重组、目录一致性
  dependency.py   引用图与失效闭包
  inference.py    绑定快照驱动的确定性 CPU 推理
  engine.py       轮次、预热、灰度、回退、请求与恢复
  server.py       标准库 HTTP API 与静态资源
  cli.py          命令行入口
  web/            无外部依赖的诊断工作台
samples/          确定性样本
scripts/          样本生成器
tests/            单元与端到端验收
```
