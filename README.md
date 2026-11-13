# 文件导入组件本地漏洞检测程序（vuln_scanner）

对自研文件导入组件做**本地授权环境**下的静态检测：只扫描指定目录内的
`.json` / `.csv` 文件，识别历史版本出现过的**路径遍历、SQL 注入、命令注入**
类问题，输出确定性 JSON 报告，并支持对修复副本做前后对比。

- 纯 Python 标准库实现，Python ≥ 3.10（实测 3.14.7），无第三方依赖
- 不使用 Docker / 容器；不访问任何网络或未授权目标
- 样本由 `tools/build_samples.py` 确定性生成，每次重建字节一致
- 扫描器对样本目录**只读**，不会改写任何原始字段值

## 目录结构

```
project43/
├── vuln_scanner/            # 检测程序包
│   ├── __main__.py          # CLI 入口（python -m vuln_scanner）
│   ├── security.py          # 路径安全校验 + 本地安全日志（JSON Lines）
│   ├── rules.py             # 内置 SQL/命令注入规则、规则合并
│   ├── config.py            # 本地自定义规则配置加载（坏配置降级）
│   ├── parsers.py           # 严格 UTF-8 的 JSON/CSV 解析与字段遍历
│   ├── scanner.py           # 扫描编排、命中记录、排序
│   └── report.py            # JSON 报告、修复基线对比
├── tools/build_samples.py   # 确定性样本/配置生成
├── tests/test_scanner.py    # 覆盖 8 个验收点的端到端测试
├── config/                  # 自定义规则（合法/损坏/部分损坏）
├── samples/raw/             # 原始样本（含损坏文件、非数据文件）
├── samples/fixed/           # 修复副本（刻意保留 1 处未修复项）
└── out/                     # 报告与安全日志输出目录
```

## 可复现的本地启动方式

```powershell
# 1) 生成确定性样本（幂等，可重复执行）
python -I tools/build_samples.py

# 2) 基线扫描：原始样本 + 合法自定义规则
python -m vuln_scanner -i samples/raw -c config/rules.json -o out/report_raw.json

# 3) 修复副本扫描：与基线对比，标识 fixed / open / new
python -m vuln_scanner -i samples/fixed -c config/rules.json `
    -b out/report_raw.json -o out/report_fixed.json

# 4) 坏配置降级演示：报告配置问题，内置规则照常生效
python -m vuln_scanner -i samples/raw -c config/rules_broken.json -o out/report_badcfg.json

# 5) 缺省输出：不指定 -o 时在当前目录生成 report.json
python -m vuln_scanner -i samples/raw

# 6) 运行全部验收测试
python -I -m unittest discover -s tests -v
```

CLI 参数：

| 参数 | 说明 |
|---|---|
| `-i/--input` | 必填，授权扫描目录（只解析其内 `.json`/`.csv`） |
| `-o/--output` | 报告路径，缺省 `./report.json` |
| `-c/--config` | 自定义规则 JSON，与内置规则合并（同名覆盖） |
| `-b/--baseline` | 基线报告，传入后输出修复状态对比段 |
| `-l/--security-log` | 安全日志路径，缺省 `<输出目录>/security.log.jsonl` |

## 自定义规则配置格式

```json
{
  "version": 1,
  "patterns": [
    {"name": "custom_sql_sleep", "type": "sql_injection", "risk": "medium",
     "pattern": "\\bSELECT\\s+SLEEP\\s*\\(", "advice": "改用参数化查询。"},
    {"name": "custom_xss_script", "type": "xss", "risk": "high",
     "pattern": "<script>", "literal": true}
  ]
}
```

- `type`：`sql_injection` / `command_injection` / `xss`
- `risk`：`high` / `medium` / `low`
- `literal: true` 表示大小写不敏感的字面量子串匹配，否则按正则匹配
- 配置缺失、编码错误、JSON 语法错误或单条规则非法：报告 `config_issues`，
  其余规则与内置规则继续生效

## 报告结构（节选）

```json
{
  "summary": {"total_findings": 6, "high": 4, "medium": 1, "low": 1,
              "parsed_files": 4, "parse_failed": 2, "rejected_or_skipped": 1},
  "findings": [
    {"file_name": "users.json", "vuln_type": "SQL注入", "risk_level": "high",
     "rule": "sql_drop_table", "field_path": "users[1].remark",
     "matched_feature": "DROP TABLE",
     "field_value": "admin'; DROP TABLE users; -- 高危 SQL 注入",
     "advice": "使用参数化查询/预编译语句……"}
  ],
  "parsed_files": ["departments.json", "orders.csv", "profile.json", "users.json"],
  "parse_failures": [{"file_name": "broken_encoding.csv", "status": "parse_failed", "...": "..."}],
  "rejected_entries": [{"file_name": "notes.txt", "status": "skipped_unsupported_extension"}],
  "config_issues": []
}
```

排序规则：风险等级 high → medium → low；同级按文件名升序，再按字段路径、
规则名保证完全确定。

## 安全设计要点

1. **文件名白名单**：`validate_entry_name` 拒绝含 `/`、`\`、`..`、盘符、
   绝对路径、Windows 非法字符的名称；仅放行 `.json`/`.csv` 扩展名。
2. **真实路径约束**：`safe_resolve` 对 `resolve()` 后的真实路径做根目录包含
   校验，符号链接/联接指向目录外同样拒绝。
3. **越界审计**：任何拒绝都以 JSON Lines 写入本地安全日志
   （时间、请求路径、根目录、原因、`DENIED` 结果）。
4. **严格 UTF-8**：非 UTF-8 或 JSON/CSV 损坏文件标记 `parse_failed` 并跳过，
   不影响同目录其他合法文件。
5. **只读保证**：扫描流程不持有样本文件的写句柄；测试以扫描前后
   SHA-256 哈希一致作为回归断言。

## 验收点对照

| # | 验收要求 | 对应测试 |
|---|---|---|
| 1 | `../` 等条目拒绝且不进解析结果，正常文件被解析 | `test_01_*` |
| 2 | `DROP TABLE` 等被标记并记录文件/字段/特征串，普通文本不标记 | `test_02_*` |
| 3 | 报告含规定字段，按风险降序、同级文件名升序 | `test_03_*` |
| 4 | 修复副本标识 fixed/open，原始字段值不改写 | `test_04_*` |
| 5 | 损坏文件标解析失败，合法文件结果完整 | `test_05_*` |
| 6 | 自定义规则合并生效；坏配置不影响其余检测 | `test_06_*` |
| 7 | 绝对路径/父目录引用被拒并写安全日志 | `test_07_*` |
| 8 | CLI 指定路径与缺省 `report.json` 均可用 | `test_08_*` |

实际运行结果见 `VERIFICATION.md`。
