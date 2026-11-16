# uid8 — 内部资料目录采集与编辑操作台

面向本地授权环境的目录采集与字段级编辑系统：确定性本地站点服务 + 自适应背压采集调度器 + 版本化编辑存储层 + 可交互操作台 + 确定性演示。不使用 Docker，不访问任何外部目标。

## 环境准备

```bash
python3.11 -m venv .venv          # 或 python -m venv .venv
.venv/bin/pip install -r requirements.txt   # fastapi uvicorn httpx jinja2 pytest python-multipart
```

## 组件

| 模块 | 说明 |
| --- | --- |
| `app/site/` | 本地确定性目标站点：分页列表、脚本二次渲染、双 schemaVersion 详情页，可注入 503 / 慢响应 / 429 限流 / 会话过期 |
| `app/crawler/` | 采集器：列表就绪判定、分页游标、自适应背压调度器、有界退避重试、会话重放、检查点恢复 |
| `app/storage/` | SQLite 存储：原始响应、规范化记录版本、编辑修订、E_EDIT_CONFLICT 待裁决、失败明细、任务检查点 |
| `app/console/` | 操作台（FastAPI + Jinja2）：任务创建/进度/失败明细、字段级编辑、版本历史、冲突裁决、恢复 |
| `demo.py` | 确定性演示：完整采集 → 人工编辑 → 冲突重采 → 崩溃恢复 |

## 运行

```bash
# 1. 启动本地目标站点（异常与版本切换均由配置注入）
.venv/bin/python run_site.py --port 8701 --config artifacts/site_cfg.json

# 2. 启动操作台
.venv/bin/python run_console.py --port 8800 --db artifacts/uid8.db --site-url http://127.0.0.1:8701
# 浏览器打开 http://127.0.0.1:8800

# 3. 确定性演示（采集+编辑+冲突+崩溃恢复，一次性跑完）
.venv/bin/python demo.py

# 4. 测试
.venv/bin/python -m pytest tests/ -q
```

站点配置项（`SiteConfig`，可经 `POST /__config` 运行时注入）：`session_ttl`（会话有效期请求数）、`session_always_expired`、`rate_limit_inflight`（在途超限返回 429）、`fail_503`（按条目注入 503 次数）、`slow_items`（按条目延迟毫秒）、`version_overrides`（按条目切换 schemaVersion）、`unknown_items`（未知版本样本）。

## 关键设计

**列表就绪判定与分页**：列表页 HTML 只含空容器 `#items` 及其 `data-render-src / data-total-pages / data-next-page` 声明；条目由脚本二次渲染后才出现。采集器轮询 render 源，以“容器已声明 + 条目计数连续两次观测一致”作为就绪标记（`crawler.py:_await_render`），超时抛 `E_RENDER_TIMEOUT` 并把观测快照写入失败明细；分页游标由总页数与下一页标识共同推进。

**schemaVersion 分派解析**：详情页 `<meta name="schemaVersion">` 声明版本，内嵌 JSON 按版本结构不同（v1 平铺字段，v2 嵌套 category/pricing）。`parser.normalize` 把两个版本映射到同一份共享规范化字段集 `SHARED_FIELDS`；未知版本保留原始字段于 `_raw` 并标记 `unresolved`，不丢数据。每条记录携带版本证据（parser、源字段列表），供编辑层与差异报告引用。

**自适应背压**（`scheduler.py`）：观测输入为在途限额使用率、有界重试等待队列长度、近期延迟滑动窗口。遇 429/5xx/超时或平均延迟超水位 → 限额乘性收缩（下限 `min_inflight`）；连续健康观测 → 按有界步长加性放开（硬上限 `max_inflight`）；重试等待由信号量封顶（`retry_queue_max`），等待期间不占用在途配额。等待由条件变量驱动，不用固定 sleep 冒充背压。限额变化历史可用于审计。

**有界退避重试与分类**：429 → `rate_limited`，5xx/超时 → `transient`，解析失败 → `parse`，三类分别统计；指数退避有界（`backoff_base/max_ms`），重试耗尽记为条目级失败（`E_RETRY_EXHAUSTED`），任务继续。

**版本共存规则**：采集以 `item_key + schemaVersion` 幂等写入 rev 0；人工编辑追加递增 rev（source=edit）；重新采集若无编辑则刷新 rev 0，若已有编辑且载荷不同则写入待裁决记录并返回 `E_EDIT_CONFLICT`，双方来源均保留，绝不静默覆盖。裁决支持“保留人工编辑 / 接受采集数据”。

**会话过期重放**：401 SESSION_EXPIRED → 重新获取会话并重放当前请求（重放同样走背压配额），上限 `session_replay_max`，超限返回 `E_SESSION` 且任务保持可恢复；过期响应体不入库。

**检查点与恢复**：每个条目确认后与每次翻页时，把已完成条目键集合、分页游标、调度器限速状态、统计写入 `tasks` 表。进程被强制终止（演示用 `crash_after_items` 模拟）后仅凭库中状态续跑，已确认条目不重复入库，修订号序列保持一致。

## 验证结果（本机实测）

- `pytest`：26 passed（解析分派、调度器收缩/放开/硬上限/有界重试、版本共存与冲突、端到端采集、渲染超时快照、会话重放上限、崩溃恢复一致性）。
- `demo.py` 连续执行 3 次，确定性汇总逐字节一致（`artifacts/demo_run*.txt`）：
  - 主任务：24 条目全部确认，`entries_inserted=23`，`unresolved=1`（item-024 未知版本），`failures={"transient": 1}`（item-003 持续 503，重试耗尽记条目级失败）；
  - 人工编辑 item-001 → rev 1；重采 → `conflicts=1`，业务码 `E_EDIT_CONFLICT`，其余 22 条 identical；
  - 崩溃任务在 16 条确认后被 kill（状态 `crashed`），仅从 SQLite 续跑至 24 条 `done`，版本序列校验 `revision_sequences_ok=true`，总版本数 24；
  - 背压事件（信息项，随定时变化）：限额在 429/高延迟下收缩至 1–2 后按步长回放至 5。
- 操作台冒烟：任务创建/进度/失败明细、记录编辑（生成 rev 1）、重采冲突上屏、冲突裁决（keep_edit）均 200/303 正常。
