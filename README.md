# 本地授权镜像站点公告采集器

从**已获书面授权的本地镜像站点快照**中批量采集公告，按配置抽取目录页条目、
按分页规则翻页、按详情链接去重，输出结构化 CSV 清单与来源可用性报告。
**不访问公网**：所有页面都从本地站点目录读取，仅在 127.0.0.1 上临时启动一个
只读回环服务来如实复现「页面暂时不可读 / 路径无响应」两类镜像故障。

## 1. 本地启动

环境要求：Python 3.10+，`requests`、`beautifulsoup4`（`soupsieve` 随 bs4 安装）。

```powershell
# 安装依赖
python -m pip install -r requirements.txt

# 1) 确定性生成本地授权站点样例（空目录；重复生成内容逐字节一致）
python scripts\generate_sample_site.py --out sample-site

# 2) 执行采集（Windows 命令行入口）
run_collector.cmd --config config\sample.json --out output

# 等价写法
python -m collector --config config\sample.json --out output
```

运行结束打印：

```
清单：output\announcements.csv
报告：output\source-report.json
  不完整来源：公告（incomplete，目录页暂时不可读） — ...
  不完整来源：招标（partial，部分条目无法采集，来源数据不完整） — ...
清单条数：11
未完成来源数：2
```

`清单条数` 等于 CSV 实际数据行数，`未完成来源数` 等于报告中
`incomplete_sources` 的条数。

退出码：`0` 全部来源完整；`2` 配置无效（不产出任何文件）；`3` 输出目录不可写；
`4` 采集完成但存在不完整来源（产物照常写出）。

## 2. 配置

UTF-8 JSON，示例见 `config/sample.json`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `site.root` | 是 | 站点快照根目录，相对路径以配置文件所在目录为基准 |
| `selectors.list.row/title/date/link` | 是 | 目录页行、标题、发布日期、详情链接选择器 |
| `selectors.detail.title/date` | 是 | 详情页标题与发布日期选择器 |
| `pagination.next` | 是 | 「下一页」链接选择器 |
| `pagination.max_pages` | 是 | 单个来源最多翻页数 |
| `request.timeout_seconds` | 否 | 单次请求时限，默认 5 |
| `request.deadline_seconds` | 否 | 整次运行总时限，默认 60 |
| `sources[].id/name/url` | 是 | 来源标识、显示名、目录首页站点内路径 |

校验规则：字段缺失、类型错误、选择器语法无效、来源 URL 含协议/主机名、来源 ID
重复，都会逐条打印原因并以退出码 2 结束，**不生成清单和报告**。缺分页字段的
样例见 `config/missing-pagination.json`。

## 3. 业务规则

- **清单**：UTF-8 无 BOM，`\n` 换行，固定表头 `标题,发布日期,详情链接`。
  数据取自目录页；日期统一规范为 `YYYY-MM-DD`（支持 `2026/8/19`、
  `2026年8月19日` 等写法）。
- **翻页与去重**：从来源首页按 `pagination.next` 逐页读取，最多 `max_pages`
  页；同一详情链接只保留首次出现的记录（同页与跨页重复均适用）。
- **详情页校验**：详情页用于确认条目可用，必需字段为标题和发布日期。
  详情页不可读、无响应、缺标题、日期缺失或非法 → 条目不入清单，在报告
  `excluded` 中按链接与原因类别列出。
- **来源状态**（报告 `status`）：
  - `complete`：目录页全部读取，且所有条目均成功采集；
  - `partial`：目录页读完，但存在被排除的条目；
  - `incomplete`：目录页未能读完（不可读 / 超时 / 分页异常 / 达到页数上限）。
  `partial` 与 `incomplete` 都归入 `incomplete_sources`，**不会**出现在
  `complete_sources` 中；其 `records` 只是已成功采集条数，不代表完整数据。
- **时限**：单页请求受 `timeout_seconds` 约束，整次运行受
  `deadline_seconds` 约束；超时条目与来源都会明确记录，已采集条目照常输出。
- **确定性**：排序为发布日期降序、同日期按标题升序（再按来源 ID、链接），
  同一快照连续运行产物逐字节一致。

## 4. 样例

`scripts/generate_sample_site.py` 生成三个来源的镜像快照：

| 来源 | 目录页 | 详情页 | 设计的问题 |
| --- | --- | --- | --- |
| 公告 | 2 | 5 | 第 2 页**不可读**；第 1 页内有重复链接；跨页重复 `a3` |
| 新闻 | 2 | 4 | 跨页重复 `b1`；`b4` 目录日期为 `2026/8/19` |
| 招标 | 2 | 8 | 缺标题 `c7`、日期非法 `c6`、详情不可读 `c8`、无响应 `hang`、目录行缺日期 `c4`、缺链接（第 6 行） |

故障由 `sample-site/.mirror.json` 声明，由采集器的本地回环服务复现：

```json
{
  "unreadable": ["/announcements/page-2.html", "/tenders/detail/c8.html"],
  "hang": ["/tenders/detail/hang.html"],
  "hang_seconds": 60
}
```

## 5. 产物示例

`output/announcements.csv`（节选）：

```csv
标题,发布日期,详情链接
关于2026年度设备采购计划的通知,2026-09-12,/announcements/detail/a1.html
中心开展技术交流活动,2026-09-11,/news/detail/b1.html
办公设备采购项目招标公告,2026-09-10,/tenders/detail/c1.html
```

`output/source-report.json` 结构：

```
complete_sources[]   来源完整：id, name, url, pages_read, records, excluded, status
incomplete_sources[] 来源不完整：额外含 reason, reason_label, reason_detail
totals               来源/记录计数
notes                口径说明
```

## 6. 验证

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

7 项用例覆盖 9 条验收要求，逐条记录见 `docs/verification.md`。

## 7. 目录

```
collector/         采集器实现（config / fetch / extract / crawl / output / cli）
config/            样例配置与缺字段配置
scripts/           确定性样例站点生成器
sample-site/       生成的站点快照（可删除后重建，已 gitignore）
output/            运行产物（已 gitignore）
tests/             端到端验收测试
docs/              验证记录
run_collector.cmd  Windows 命令行入口
```
