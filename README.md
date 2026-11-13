# 本机回环反射型脚本注入探测工具

仅面向**本机授权演示站点**的反射型脚本注入（Reflected XSS）离线探测工具。
纯 Python 3.10+ 标准库实现，无第三方依赖、无容器；通过“唯一标记 + 转义状态 +
HTML 上下文”三重判定输出确定性 JSON 报告，支持修复前后复核对比。

## 1. 安全边界

- 工具只允许对回环地址执行：主机名经 `getaddrinfo` 解析后，**每一个** IP 都必须
  属于 `127.0.0.0/8` 或 IPv6 `::1`，否则直接拒绝，退出码 `3`，且不发送任何请求、
  不生成报告。
- 演示站点只绑定 `127.0.0.1`，不监听外部网卡；尝试绑定非回环地址会自行退出。
- 用例中的 `url` 覆盖字段同样逐条做回环校验，无法借由用例清单绕过限制。
- 全部样本由代码确定性生成，不访问任何未授权目标。

## 2. 目录结构

```
project33/
├─ xss_probe/              探测工具包（标准库）
│  ├─ models.py            结论/入口/上下文常量与用例数据结构
│  ├─ netutil.py           回环校验、URL 构造、HTTP 请求
│  ├─ parserutil.py        HTML 上下文分词判定（脚本/属性/文本）与转义形式
│  ├─ scanner.py           逐条探测与风险判定（单条失败隔离）
│  ├─ report.py            用例加载、报告生成、历史比对
│  ├─ samples.py           确定性用例清单与历史报告样本生成
│  ├─ cli.py / __main__.py 命令行入口
├─ demo_site/server.py     脆弱版/修复版同构演示站点（--mode 切换）
├─ tests/test_end_to_end.py  15 项端到端/单元测试（unittest）
├─ scripts/
│  ├─ demo.ps1 / demo.sh   一键演示
│  └─ verify_escape.py     转义回显核验小工具
├─ samples/                生成的用例清单与历史报告（运行后出现）
└─ reports/                JSON 报告输出目录（运行后出现）
```

## 3. 本地启动方式

### 3.1 启动演示站点（开两个终端）

```powershell
# 终端 A：脆弱版（未转义直接拼接）
python demo_site/server.py --port 8033 --mode vulnerable
# 终端 B：修复版（统一 html.escape 转义）
python demo_site/server.py --port 8034 --mode fixed
```

站点页面：

| 路由 | 方法 | 入口 | 回显上下文 |
|---|---|---|---|
| `/reflect?q=` | GET | 查询参数 | 文本 |
| `/attr?q=` | GET | 查询参数 | 属性（input value） |
| `/script?q=` | GET | 查询参数 | 脚本（`<script>` 内） |
| `/path/<片段>` | GET | 路径片段 | 文本 |
| `/form`（字段 msg） | POST | 表单回显 | 文本 |
| `/empty?q=` | GET | 查询参数 | 空响应体（验证无效响应判定） |
| `/safe?q=` | GET | 查询参数 | 始终转义的对照页 |

### 3.2 生成确定性样本

```powershell
python -m xss_probe gen-cases  --output samples/cases.json
python -m xss_probe gen-history --output samples/history_report.json
```

用例标记形如 `z1qf03eb163x<svc>"1`：由用例标识哈希派生，全局唯一，并携带
`<`、`>`、`"` 等 HTML 特殊字符，使“转义/未转义”可被明确区分。

### 3.3 执行探测

```powershell
# 脆弱版（带历史比对）
python -m xss_probe scan --base-url http://127.0.0.1:8033 `
  --cases samples/cases.json --report reports/vuln_run1.json `
  --fix-status vulnerable-2026-11-14 --history samples/history_report.json

# 修复版复核
python -m xss_probe scan --base-url http://127.0.0.1:8034 `
  --cases samples/cases.json --report reports/fixed_run.json `
  --fix-status fixed-2026-11-14 --history samples/history_report.json
```

### 3.4 一键演示

```powershell
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1
# 或 Git Bash：
bash scripts/demo.sh
```

## 4. 判定规则（不看状态码、不只看“出现”）

对每条用例发送携带唯一标记的请求后：

1. 请求失败（连接拒绝/超时/DNS 失败）或**响应体缺失/为空** →
   `未获得有效响应`（上下文记“不适用”，说明字段注明原因）；
2. 标记以**未转义原样**出现在响应体 → `存在风险`，并用确定性分词器标注
   `脚本` / `属性` / `文本` 上下文；
3. 标记仅以 HTML 转义形式（`&lt;`、`&quot;` 等，兼容引号转义与否两种写法）
   出现 → `无风险`，同时仍标注其回显上下文；
4. 标记完全未出现 → `无风险`，上下文记“无回显”。

上下文分词规则：`<script>...</script>` 区间内为脚本；区间外、落在某标签
`<...>` 内部为属性；其余为文本。同一标记多次出现时报告保留全部上下文。

## 5. 报告格式（JSON）

- 顶层：报告版本、确定性运行标识（对目标与全部结论取 SHA-256，无时间/随机量）、
  目标基址、**修复状态标识**、汇总计数、用例记录、结论取值约定、可选历史比对块。
- 每条用例记录字段：`入口类型`、`用例标识`、`标记`、`结论`、`上下文类型`、
  `全部上下文`、`HTTP状态码`、`请求URL`、`证据片段`、`说明`。
- 结论取值统一为三种：`存在风险` / `未获得有效响应` / `无风险`。
- 排序：存在风险 → 未获得有效响应 → 无风险；同级按用例标识升序。
- 历史比对按组合键 **(入口类型, 用例标识, 标记)** 匹配，分别列出
  `新增风险` 与 `已消除风险`。

## 6. 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 扫描完成且不存在风险（或样本生成类命令成功） |
| 1 | 扫描完成但存在风险 |
| 2 | 参数/用例文件错误 |
| 3 | 目标非本机回环地址等运行前校验失败 |

运行结束始终打印总用例数、存在风险数、未获得有效响应数，计数与报告汇总一致。

## 7. 实际验证结果（2026-11-14，Windows 11 + Python 3.14.7）

- `python -m unittest tests.test_end_to_end -v`：**15 项测试全部通过**。
- 脆弱版 8 条用例：存在风险 5（文本/属性/脚本/路径/表单各覆盖）、
  未获得有效响应 2（空体 TC-006、不可达 TC-008）、无风险 1，退出码 1。
- 修复版同 8 条：存在风险 0、未获得有效响应 2、无风险 6，退出码 0；
  响应体确认标记被转义为 `&lt;svc&gt;&quot;` 等形式。
- 外部地址 `http://example.com/` 与内网地址 `http://192.168.1.20/`：
  均退出码 3 并提示“仅允许对本机回环地址执行”，不生成报告。
- 同参数连续两次报告字节级一致；`PYTHONHASHSEED=1` 与 `=42` 下报告仍一致，
  运行标识不变。
- 历史比对：脆弱版相对历史基线列出新增 TC-005、已消除 TC-099；
  修复版列出已消除风险 5 项、新增 0。

## 8. 已知边界（工具定位内的取舍）

- 分词器为确定性轻量规则实现，不解析注释/CDATA/模板语法等复杂场景；
  适用于常规 HTML 回显判定，复杂前端渲染页面需要更重的浏览器引擎方案。
- 脚本上下文的“安全”以输出编码（HTML 实体编码）为判定依据；严格的 JS 上下文
  防护还需配合 JS 字符串编码（如 JSON 编码），修复时应按上下文选择编码方式。
- 仅支持同步单线程逐条探测，定位为小规模本机复核工具，不做并发压测。
