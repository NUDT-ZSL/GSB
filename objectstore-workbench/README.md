# 对象存储分片上传与元数据一致性工作台（OSWB）

面向 **Windows 本地环境** 的对象存储分片上传与元数据一致性工作台。仅依赖
Python 3.11+ 标准库（Tkinter 随官方 Windows 安装包提供），不使用任何第三方包、
容器或线上服务。客户端把本地确定性样本按可配置分片大小切分，经 **本地回环 TCP**
上传到存储服务模拟进程；服务端以本地文件持久化分片/对象数据，以 **SQLite**
持久化对象元数据、版本、会话状态机与审计记录。

## 目录结构

```
objectstore-workbench/
├─ oswb/
│  ├─ errors.py        # 确定性业务码（E_CHUNK/E_INTEGRITY/E_VERSION/E_AUDIT/E_REPLAY ...）
│  ├─ digest.py        # 确定性摘要（sha256/sha1/md5/blake2b/sha512）
│  ├─ chunker.py       # 纯函数式确定性分片与清单（非法分片大小 E_CHUNK，零持久化副作用）
│  ├─ sample.py        # 固定 1 MiB 确定性样本生成
│  ├─ storage.py       # 持久化核心：SQLite 元数据 + 分片/对象文件 + 状态机 + 版本 + 审计 + 快照
│  ├─ protocol.py      # 长度前缀 JSON 帧协议 + 端口 1..65535 校验
│  ├─ server.py        # 线程化回环 TCP 服务，Storage 的薄封装
│  ├─ client.py        # 帧 RPC 客户端
│  ├─ uploader.py      # 双重配额并发调度器（单调毫秒计时、尝试序号、重复收敛）+ 上传/续传
│  ├─ runner.py        # 进程内启动真实回环服务（GUI/demo 用，仍走 socket）
│  ├─ demo.py          # 覆盖 8 项业务要求的端到端确定性验证
│  ├─ cli.py           # 统一命令行入口
│  └─ gui.py           # Tkinter 界面
├─ tests/              # 19 个单元/集成测试（真实回环 socket）
├─ scripts/            # Windows .bat 与 Linux .sh 启动脚本
├─ runtime/            # 运行期数据、样本、结构化日志（自动创建）
└─ requirements.txt    # 无第三方依赖声明
```

## 快速开始（Windows）

```bat
REM 1. 端到端确定性验证（推荐先跑，自动起停回环服务）
scripts\run-demo.bat

REM 2. 单独启动回环存储服务（默认端口 9101，可用 --port 覆盖，范围 1..65535）
scripts\run-server.bat

REM 3. 另开一个命令行窗口执行客户端命令
python -m oswb make-sample
python -m oswb upload --file runtime\samples\sample-1MiB.bin --object-key demo/obj --chunk-size 262144
python -m oswb sessions
python -m oswb commit --session-id <SESSION_ID>
python -m oswb audit
python -m oswb gui
```

WSL/Linux 下使用 `scripts/run-tests.sh`、`scripts/run-demo.sh`、`scripts/run-server.sh`。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `make-sample` | 生成固定 1 MiB 确定性样本 |
| `serve` | 启动回环 TCP 存储服务（`--host/--port/--base-dir/--log-dir`） |
| `upload` | 建会话并按全局/单会话配额并发上传全部分片 |
| `resume` | 按持久化状态续传，已登记分片自动跳过，`--complete` 顺带合并 |
| `commit` | 逐片摘要校验后合并为可见对象并发布元数据版本 |
| `sessions` | 列出会话及其状态、已完成分片集合 |
| `versions` / `diff` | 多版本查询与稳定字段/分片清单差异 |
| `audit` | 全量一致性审计，差异返回 `E_AUDIT` 并定位首个差异分片 |
| `snapshot create/restore/list` | 快照与整体替换式恢复（不可复现 `E_REPLAY`） |
| `inject` | 三类故障注入（仅改输入文件/参数，不改源码） |
| `demo` | 一键端到端验证 |
| `gui` | Tkinter 工作台 |

## 业务码

| 业务码 | 触发场景 |
| --- | --- |
| `E_CHUNK` | 非法分片大小、分片长度不符、分片数据被截断 |
| `E_INTEGRITY` | 持久化分片摘要与登记摘要不一致、缺片 |
| `E_VERSION` | 上传/提交时对象元数据版本冲突 |
| `E_AUDIT` | 审计发现元数据、分片数据、对象内容或版本对应关系不一致 |
| `E_REPLAY` | 快照无法复现记录的指纹或审计结论 |
| `E_STATE` | 非法会话状态迁移 |
| `E_NOT_FOUND` / `E_PARAM` / `E_TRANSPORT` | 资源不存在 / 参数非法 / 回环通信异常 |

## 持久化布局

```
<base-dir>/
├─ db/workbench.sqlite3      # objects / object_versions / sessions / session_chunks / audit_runs
├─ chunks/<session-id>/chunk-00000000.part ...
├─ objects/<object-key 路径>
└─ snapshots/<snapshot-id>/{snapshot.json, bundle/}
```

结构化日志写入 `<log-dir>/oswb.server.jsonl` 与 `oswb.client.jsonl`，每行一个
排序键的 JSON 对象，包含时间戳、事件名与上下文。

## 关键一致性保证

- **原子提交**：对象文件先写临时文件、`fsync` 后 `os.replace` 落地，再在单个
  SQLite 事务内发布完成标记与新版本，杜绝“元数据已提交、对象内容缺失”的中间态。
- **状态机持久化**：`created → uploading → verifying → committed/aborted`
  每次迁移落库；重开工作台仅凭持久化状态恢复，已登记分片不重复上传。
- **并发收敛**：服务端按会话串行化分片写入并对已登记分片幂等去重；客户端受
  全局并发上限与单会话配额双重信号量约束，同分片并发重复上传收敛为唯一结果。
- **整体替换**：新版本元数据整行写入，旧版本只读保留；快照恢复在指纹不一致时
  整体替换当前状态而非增量合并。
