# 验证记录

环境：Windows 11，Python 3.10.20，requests 2.34.2，beautifulsoup4 4.15.0。
数据：`scripts/generate_sample_site.py` 确定性生成的本地授权站点快照
（3 个来源、6 个目录页、17 个详情页、若干故障路径），仅通过 127.0.0.1
回环读取，不访问公网。

复现命令：

```powershell
python scripts\generate_sample_site.py --out sample-site
python -m pytest -q
run_collector.cmd --config config\sample.json --out output
```

`pytest` 结果：**7 passed**。

| # | 要求 | 验证方式 | 结果 |
| --- | --- | --- | --- |
| 1 | 字段缺失时明确提示，不产出清单或报告 | `config/missing-pagination.json` 删除 `pagination` 段后运行 | 退出码 2，stderr 输出 `缺少必需字段：pagination.next`、`缺少必需字段：pagination.max_pages`；输出目录未创建，无 CSV / JSON |
| 2 | 目录页抽取标题/日期/链接，UTF-8 CSV，固定字段顺序，日期 YYYY-MM-DD | 解析产物表头与全部日期 | 表头为 `标题,发布日期,详情链接`；11 行数据全部匹配 `\d{4}-\d{2}-\d{2}`（含目录页写作 `2026/8/19` 的一条，已规范为 `2026-08-19`） |
| 3 | 合并分页、按详情链接去重、保留首次出现 | 统计链接唯一性与行数 | 11 行、11 个唯一链接；公告 `a3`（第 1 页与第 2 页重复）、新闻 `b1`（第 1 页与第 2 页重复）、公告第 1 页内的 `a1` 均只保留一行 |
| 4 | 详情页不可读或缺字段的条目不入清单，报告按链接与原因单列 | 核对清单与报告 `excluded` | `c7`（缺标题）→ `detail_title`；`c6`（日期 `2026-13-32` 非法）→ `detail_date`；`c8`（HTTP 503）→ `detail_unreadable`；`hang`（无响应）→ `detail_timeout`；`c4`（目录行缺日期）→ `entry_date`；第 6 行（缺链接）→ `entry_link`。以上链接均不在 CSV 中 |
| 5 | 目录页不可读时来源记为未完成，已采集条目照常写入，不影响其他来源 | 检查公告来源与其余来源 | 公告第 2 页返回 503：`a1/a2/a3` 3 条仍在清单；`a5`、`a14` 未采集；新闻 4 条、招标 4 条不受影响；报告将公告列为 `incomplete` / `list_unreadable` |
| 6 | 报告区分完整与未完成来源并写明原因，未完成来源不得显示为完整 | 校验两个列表是否互斥、原因是否为空 | `complete_sources` 仅含新闻（`status=complete`）；公告（`incomplete`）与招标（`partial`，有 6 条被排除）均在 `incomplete_sources`，且 `reason` / `reason_label` / `reason_detail` 均非空；两列表 id 集合无交集 |
| 7 | 按发布日期降序、同日期标题升序，重复运行逐字节一致 | 排序断言 + 两次运行 `cmp` | 排序断言通过；两次运行的 `announcements.csv` 与 `source-report.json` 均逐字节相同 |
| 8 | 存在无响应路径时在限定时限内结束，其余条目完整 | 计时运行（`timeout_seconds=2`，`deadline_seconds=15`） | 总耗时 2.78 秒（含无响应路径的 2 秒等待），远低于 15 秒；`hang` 被记为 `detail_timeout`，其余 11 条完整写入 |
| 9 | 命令行入口可指定输出路径，打印条数与未完成来源数且与产物一致 | 运行 `run_collector.cmd --config ... --out output` 并与产物比对 | 打印 `清单条数：11` = CSV 数据行数 11；`未完成来源数：2` = 报告 `incomplete_sources` 条数 2；退出码 4（存在不完整来源） |

附加校验：

- **样例可确定性重建**：两次独立生成站点快照后 `diff -r` 无差异。
- **输出目录不可写**：以文件占位目标目录时退出码 3，提示「输出目录不可写」，不产出文件。
- **不访问公网**：来源 URL 若含协议/主机名在配置校验阶段即被拒绝；目录页中的跨站链接
  记为 `entry_link` 原因并跳过；所有请求目标均为 `127.0.0.1` 随机端口。

已知边界：

- 同一快照下产物逐字节一致；不同 Python/bs4 次版本对 HTML 解析结果的差异不在保证范围内。
- 「无响应」通过本地回环服务延迟复现，未对真实远端站点做任何请求。
