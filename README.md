# 本地供应商报价快照采集系统

纯 Python 标准库实现（Python 3.10+，无第三方包、无容器），通过本机回环地址读取
由确定性样本生成的模拟供应商页面，提取商品名与报价，输出比价 CSV、JSON 采集报告
与纯文本采集日志。单来源失败不影响其他来源，成功数据始终可用、未完成来源可明确辨认。

## 1. 目录结构

```
project14/
├── collector.py              # 采集器主程序（配置校验/抓取/解析/输出/退出码）
├── supplier_server.py        # 回环样本服务器（仅绑定 127.0.0.1/::1，支持故障注入）
├── run_acceptance_tests.py   # 14 项验收点的端到端自动化测试（15 个断言）
├── config.json               # 默认配置（3 个正常供应商）
├── samples/                  # 确定性 HTML 样本
│   ├── hongfa.html           # 宏发五金，3 个商品
│   ├── xinyuan.html          # 鑫源建材，含 1 个 N/A 报价商品
│   ├── boyuan.html           # 博远机电，2 个商品
│   └── broken.html           # 结构异常页面（无 ul.products 结构）
├── demo_configs/             # 手动演示用配置（部分失败 / 缺字段）
├── output/                   # 默认配置的采集产物（运行后生成）
└── tests_workspace/          # 自动化测试的临时工作区（运行后生成，可删）
```

## 2. 配置文件说明（config.json）

| 字段 | 含义 | 校验规则 |
| --- | --- | --- |
| `server.host` | 样本服务主机 | 仅允许 `127.0.0.1` / `::1` / `localhost`，拒绝非回环地址 |
| `server.port` | 默认端口 | 1–65535 整数 |
| `output.dir` | 输出目录（相对路径锚定**配置文件所在目录**） | 非空字符串，不存在时自动创建（含多级父目录） |
| `output.csv_file` / `report_file` / `log_file` | 三个产物文件名 | 非空字符串 |
| `http.timeout_seconds` | 单次请求超时 | 正数，默认 10 |
| `http.retry_attempts` | 每来源总尝试次数（含首次） | ≥1 整数，默认 3 |
| `http.backoff_seconds` | 重试间隔 | 非负数，默认 0.5 |
| `suppliers[].name` | 来源名称 | 非空、不可重名 |
| `suppliers[].page_id` | 页面标识（URL 路径） | 非空、不可重复 |
| `suppliers[].port` | 可选，覆盖默认端口 | 1–65535 整数，用于模拟不可达来源 |

任何字段缺失或非法时，程序打印**具体字段路径**（如 `缺少必填字段: suppliers[0].page_id`），
不产生采集产物，退出码 `2`。

## 3. 可复现的本地启动方式（PowerShell）

需要两个终端（或后台运行服务端）。

### 终端 A：启动回环样本服务器

```powershell
cd D:\demo_GSB\demo\projects\project14
python -I supplier_server.py --samples-dir samples --host 127.0.0.1 --port 8000
# 看到 "[supplier_server] 样本服务已启动: http://127.0.0.1:8000/" 即就绪
```

### 终端 B：运行采集

```powershell
cd D:\demo_GSB\demo\projects\project14
python -I collector.py --config config.json
echo "exit=$LASTEXITCODE"   # 全部成功为 0，存在未完成来源为 1，配置错误为 2
```

产物（`output/` 目录，自动创建）：

- `quotes.csv`：表头固定为 `来源,商品名,报价`，报价统一两位小数（如 `0.10`），
  数据行按 `(来源, 商品名)` 排序，重复运行字节级一致；
- `report.json`：`collected_at`（带时区的 ISO 8601 时间，可被
  `datetime.fromisoformat` 解析）、`completed_sources`、`incomplete_sources`，
  两个列表均按名称字母序（Unicode 码位序）排列；
- `collector.log`：纯文本，每个来源一行 `来源结果: 名称 | 状态 | 耗时: x.xxx 秒 | ...`。

### 业务操作样例：故障与容错

```powershell
# 1) 部分来源未完成（结构异常 + 端口不可达），退出码 1，成功来源数据不丢失
python -I collector.py --config demo_configs\config_partial.json

# 2) 缺字段配置，打印具体缺失项并拒绝启动，退出码 2
python -I collector.py --config demo_configs\config_missing_field.json

# 3) 短暂不可用后恢复：让 boyuan 页面前 2 次请求返回 503，第 3 次起恢复。
#    采集器 retry_attempts=4，重试后该来源仍进入完整列表
python -I supplier_server.py --samples-dir samples --host 127.0.0.1 --port 8001 `
    --flaky-page boyuan --flaky-fail 2
```

### 一键自动化验收

```powershell
python -I run_acceptance_tests.py
# 末行输出：共 15 项，通过 15 项，失败 0 项；进程退出码 0
```

测试自行在临时端口启动/关闭样本服务（含 flaky 故障注入），在 `tests_workspace/`
下为每个场景生成独立配置与产物，互不污染，可重复执行。

## 4. 关键行为约定

- **回环限制**：服务器拒绝绑定非回环地址；采集器同样校验 `server.host`，
  不会访问回环之外的任何目标。
- **结构异常判定**：页面没有 `ul.products > li.product` 结构，或全部商品都无法
  解析出有效名称+报价时，整个来源记为未完成，不写入该来源任何商品。
- **单品容错**：单个商品缺少名称/报价，或报价为非数值（如 `N/A`）时只跳过该商品，
  同页其他商品正常写入（跳过数量写入日志）。
- **重试语义**：连接失败、超时、HTTP 错误均按 `retry_attempts` 重试，
  固定间隔 `backoff_seconds`；持续不可达的来源最终进入未完成列表。
- **数据安全**：CSV 只写成功来源的记录，不产生空行/空值；未完成来源仅出现在
  报告与日志中。
- **排序口径**：使用 Python 字符串默认 Unicode 码位序（标准库不含中文拼音排序），
  它是确定性全序，同一份样本重复运行的数据行内容与顺序完全一致，便于人工比对。
- **编码**：所有文件 UTF-8；程序启动时将标准流重配为 UTF-8，避免 Windows GBK
  控制台导致的中文/符号输出错误。

## 5. 验收点对照与实测结果

实测环境：Windows 11 + Python 3.14.7，`python -I run_acceptance_tests.py` 全绿
（15 个断言覆盖 14 项验收点，验收 11 拆成全成功/部分失败两个断言）。

| 验收点 | 验证方式 | 实测结果 |
| --- | --- | --- |
| 1 配置校验 | 合法配置正常启动；删除 `suppliers[0].page_id` | 打印 `缺少必填字段: suppliers[0].page_id`，退出码 2 |
| 2 回环样本 | 抓取 `http://127.0.0.1:<port>/hongfa` 与样本文件比对 | HTTP 200，内容与 `samples/hongfa.html` 字节一致 |
| 3 提取与两位小数 | 校验 CSV 报价列全部可转 float 且恰两位小数 | 7 条记录全部通过（如 `0.85`、`145.00`） |
| 4 CSV 结构 | 表头与记录数 | 表头 `来源,商品名,报价`，7 条数据行 |
| 5 报告列表排序 | 检查两个列表及与实际结果一致性 | 均为字母序，完整 3 个、未完成 0 个 |
| 6 单来源不可达 | 一个来源指向无监听端口 | 其余 5 条记录完整，该来源在未完成列表 |
| 7 结构异常 | 接入 `broken.html` | 该来源 0 条记录入 CSV，列为未完成，退出码 1 |
| 8 N/A 单品跳过 | 解析 `xinyuan.html` | “红砖 千块”缺失，其余 2 条正常入 CSV |
| 9 短暂故障恢复 | flaky 服务前 2 次 503 后恢复 | 第 3 次尝试成功，来源进入完整列表，退出码 0 |
| 10 持续不可达 | 端口始终无监听，重试耗尽 | 其他来源数据正确无空值，未完成列表含该来源 |
| 11 退出码 | 全成功 / 部分失败分别运行 | 分别为 0 和 1（配置错误另为 2） |
| 12 目录与时间格式 | 指向不存在的嵌套输出目录 | 目录自动创建、三文件就位，时间可被 `fromisoformat` 解析 |
| 13 日志 | 检查日志文件 | 纯文本，每来源一行结果且含 `耗时: x.xxx 秒` |
| 14 确定性 | 连续两次运行后哈希比对 | 两份 CSV 数据行完全一致 |

## 6. 退出码约定

| 退出码 | 含义 |
| --- | --- |
| 0 | 全部来源完整采集 |
| 1 | 采集流程完成，但存在未完成来源（报告/日志可查） |
| 2 | 配置错误或配置文件不可读，未执行采集 |
