# 对账明细采集引擎（recon）

从三个异构数据源（本地模拟 REST 分页接口、毫秒时间戳增量接口、CSV 快照文件）
稳定拉取对账明细，统一幂等落库到本地 SQLite。全部样本由固定种子确定性生成，
不依赖任何线上账号或外部真实数据，不使用容器。

## 环境准备

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt # POSIX
```

> 任务目标为 Python 3.11；本实现仅使用 3.10+ 通用语法，在本机可用的 3.13 上验证。
> 依赖锁定 requests==2.31.0、pytest==7.4.0。

## 目录结构

```
recon/
  config.py        配置加载与边界校验（稳定业务错误码，零副作用）
  errors.py        业务错误码与可重试/不可重试异常体系
  locking.py       OS 级单实例文件锁（O_EXCL + PID 存活探测 + 内核字节锁）
  applog.py        按日期滚动应用日志（仅运行元数据，禁止业务字段原值）
  generator.py     确定性样本生成器（固定种子，三源数据/CSV/注入规则/真值）
  mock_server.py   本地模拟 HTTP 服务（显式错误/延迟注入、请求侧写、停止计数）
  adapters.py      REST / 时间戳 / CSV 三适配器，统一 fetch(cursor, limit)
  ratelimit.py     滑动窗口 QPS、并发闸门、最小间隔（等待可中断）+ 运行统计
  retry.py         带抖动指数退避、分级重试、熔断器
  storage.py       SQLite 主表/批次/冲突审计/写审计/进度/运行元数据，单事务原子写
  report.py        JSON 运行报告、aborted 临时报告、内置 JSON Schema 校验
  engine.py        采集编排：确定性提交顺序、断点续跑、中断与恢复
  cli.py           命令行入口（先校验配置再加锁，稳定退出码）
tests/             12 个测试文件、38 条确定性验收用例
```

## 快速运行

```bash
# 1. 生成种子 42 的样本、CSV 快照、规则与配置（默认端口 8765）
python -m recon.generator workspace 42 8765

# 2. 启动模拟服务（独立进程）
python -m recon.mock_server workspace/data/server_rules.json 8765

# 3. 运行采集（另一个终端）
python -m recon.cli --config workspace/configs/base.json

# 停止模拟服务（输出已处理请求计数并在 2s 内释放端口）
curl "http://127.0.0.1:8765/shutdown?token=shutdown-token"
```

## 运行全部测试

```bash
python -m pytest tests/ -q
```

测试自行在临时端口启动/停止模拟服务、生成隔离工作区，不占用固定 8765。

## 关键设计与验收对照

| 任务 | 实现位置 | 要点 |
|---|---|---|
| 1 三适配器/游标/CSV 部分写入 | `adapters.py` | 页码游标回传总页数；TS 半开区间 end→next start；CSV 内容摘要识别版本、字节行偏移、残缺行 `should_retry` 不计批 |
| 2 进度状态机/断点续跑 | `storage.py`,`engine.py` | 游标推进与批次写入同事务；每源单行进度；控制文件/信号受控中断与强杀 SQLite 恢复 |
| 3 幂等落库/冲突合并 | `storage.py` | serial_no 全局主键、source 仅追踪；新值覆盖 NULL、非空冲突保留旧值并逐项写冲突审计；三表同事务回滚 |
| 4 限速/并发/统计 | `ratelimit.py` | 滑动 1s 窗口 ≤ QPS、在途 ≤ 并发、TS 发起间隔；等待可中断；state.json 暴露三统计 |
| 5 分级重试/熔断 | `retry.py` | 429/5xx/连接/CSV 部分写入可重试退避；400/401/解码/校验致命；连续失败 >5 熔断 open |
| 6 乱序/重复窗口 | `engine.py`,`storage.py` | 自然键去重，跨页/边界重复计入 duplicate_dropped，游标不回退；TS 最终游标 max_ts+1 |
| 7 JSON 报告/aborted | `report.py`,`engine.py` | 内置 Schema 校验；aborted 临时报告含 run_id；恢复后 resumed_from_run_id |
| 8 日志/审计分离 | `applog.py`,`storage.py` | 日志仅 run_id/源/批次/条数/错误码；审计表记录写库条数与冲突，run_id 关联 |
| 9 确定性生成器/服务 | `generator.py`,`mock_server.py` | 固定种子一次定型，注入全部显式声明、无运行期随机 |
| 10 端到端恢复 | `engine.py` | 武装门保证中断时序；恢复首个游标=上次已提交下一游标；哈希与不中断真值一致 |
| 11 配置校验 | `config.py` | 六类稳定错误码、非零退出、失败不产生任何 db/log/lock 文件 |
| 12 单实例互斥/清理 | `locking.py`,`cli.py`,`mock_server.py` | 5s 内 E_LOCKED 退出；退出后可重锁；服务停止输出请求计数并释放端口 |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | success |
| 2 | 配置校验失败（E_CFG_*） |
| 3 | 单实例锁冲突（E_LOCKED） |
| 4 | 运行失败（含致命业务错误、熔断后无成功源、事务钩子失败） |
| 5 | 受控中断（aborted） |

## 中断与恢复机制

- **受控中断**：在 `control_file` 写入 `stop`，或发送 SIGINT/SIGBREAK/SIGTERM。
  引擎完成当前写事务后生成 `report_<run_id>.aborted.json`，退出码 5。
- **武装门**（确定性测试时序）：配置 `arm_stop_after_rest_batches=N` 后，
  控制文件中断在 REST 提交满 N 批后才生效，保证中断点精确。
- **强制终止**：无 aborted 报告时，重启依据 SQLite `run_meta` 与 `source_progress`
  持久化的 run_id/游标恢复，最终报告 `resumed_from_run_id` 指向上一运行。
