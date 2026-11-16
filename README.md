# 动态页面采集控制台（本地演示）

面向内部运维团队的本地运行动态页面采集控制台：对本地模拟工单系统（分页、延迟渲染、
偶发 503、会话过期、多版本详情结构）按可配置并发度采集列表与详情，原始与规范化数据
写入 SQLite，支持断点恢复；控制台基于 FastAPI，附带简单前端页面。整个演示可重复运行
且结果确定。

## 目录结构

- `app/mock_site.py` — 本地模拟工单系统（FastAPI）：静态 HTML + JSON 接口，内置 24 条
  确定性样本（v1/v2/v9 三种详情结构），分页、延迟渲染、503 与会话过期均通过
  `POST /__config` 配置注入，无需改源码。
- `app/store.py` — SQLite 持久化：任务状态机（pending→running→succeeded/failed/paused，
  非法迁移抛 `E_STATE`）、幂等入库（工单标识+版本号唯一键，冲突保留最新并记录来源）、
  检查点与分页游标、条目级失败与观测快照。
- `app/parsers.py` — 详情解析：按页面 `<meta name="schema-version">` 选择 v1/v2 规则；
  未知版本降级为原始字段保留（`parse_status=raw_fallback`），不静默丢弃数据。
- `app/collector.py` — 异步采集器：显式渲染就绪判定（容器标记 + 条目计数稳定，轮询而非
  固定 sleep，超时 `E_RENDER_TIMEOUT` 并记录快照）、并发上限 8、503 有界指数退避
  （耗尽记条目级失败 `E_HTTP_503`）、会话过期重登重放（超限 `E_SESSION`，任务置 paused
  可恢复，过期响应不入库）、基于检查点/游标的断点续跑。
- `app/console.py` + `app/static/index.html` — FastAPI 控制台与前端：创建/启动/暂停/
  恢复任务、进度与失败明细查询；进程重启时将遗留 running 任务迁移为 paused 等待恢复。
- `demo.py` / `run_demo.sh` — 确定性演示（见下）。
- `tests/` / `run_tests.sh` — pytest 测试（20 项）。

## 环境准备（Windows 10/11 或 Linux，Python 3.11+）

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows；Linux 用 source .venv/bin/activate
pip install -r requirements.txt   # playwright fastapi uvicorn httpx pytest（sqlite3 为标准库）
playwright install chromium
```

仅访问本机回环地址（127.0.0.1）的模拟站点，不使用 Docker，不访问未授权目标。

> 本仓库环境说明：当前容器无 sudo 且缺 `libatomic`/X11 等系统库，已将依赖库提取到
> `.runtime-libs/`，`run_demo.sh` / `run_tests.sh` 会自动设置 `LD_LIBRARY_PATH`。

## 运行

```bash
./run_demo.sh        # 一键确定性演示（四个场景，约 1 分钟）
./run_tests.sh       # 运行 20 项测试
```

手动体验控制台：

```bash
python -m app.mock_site --port 8100        # 终端 1：模拟工单站点
python -m app.console --port 8200 --db console.db   # 终端 2：控制台
# 浏览器打开 http://127.0.0.1:8200/ 创建任务、查看进度与失败明细、触发恢复
```

## 演示场景（demo.py）

每次运行使用独立临时数据库，运行前清空重来；异常注入全部通过配置参数完成。

- **A 干净运行**：并发 4 采集 24 条工单；注入 `TK-1003` 前 2 次 503（退避后恢复）、
  `TK-1007` 持续 503（重试耗尽 → 条目级失败 `E_HTTP_503`，任务仍 succeeded）、
  会话每 6 请求过期（自动重登重放）。结果：入库 23、条目失败 1。
- **B 强制终止续跑**：运行中 `SIGKILL` 控制台进程，重启后仅凭库中状态将遗留 running
  迁移为 paused，恢复后续跑；最终汇总与 A 完全一致，无重复行、无遗漏。
- **C 会话恢复**：开启 `session_fail_always`，重放超限 → 任务 paused + `E_SESSION`，
  过期响应未入库；关闭开关后恢复至 succeeded。
- **D 渲染超时**：`render_timeout_ms` 小于渲染延迟 → 任务 failed +
  `E_RENDER_TIMEOUT`，并记录观测快照（页面 URL、计数序列、HTML 摘录）。

## 实际验证结果（2026-11-16，本容器）

- `./run_demo.sh` 连续执行两次，规范化汇总逐字节一致（`diff` 为空）：
  A/B/C 均 `succeeded`、入库 23、distinct 23、`parse_status={ok:15, raw_fallback:8}`、
  唯一条目失败 `TK-1007: E_HTTP_503`；D 为 `failed + E_RENDER_TIMEOUT` 且快照 ≥1。
- `./run_tests.sh`：20 passed（状态机合法/非法迁移、幂等键与冲突保留、检查点/游标、
  v1/v2/未知版本解析、端到端采集、条目级 503、会话耗尽可恢复、渲染超时快照、
  协作式暂停、暂停后恢复跳过已确认条目）。
- 手动冒烟：控制台 `POST /api/tasks` → `succeeded`，24/24 入库无重复；
  重复 `resume` running 任务返回 409 `E_STATE`。
