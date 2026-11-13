# 实际验证记录（VERIFICATION）

- 环境：Windows 11，Python 3.14.7（要求 ≥ 3.10），纯标准库、无容器、无网络访问
- 样本：`tools/build_samples.py` 确定性生成，原始样本 7 个条目
  （4 个合法数据文件、2 个损坏文件、1 个不支持扩展名的 txt）
- 测试命令：`python -I -m unittest discover -s tests -v`
- 结果：**9 个测试全部通过（Ran 9 tests … OK）**

## 样本构成与预期

| 文件 | 性质 | 预期行为 |
|---|---|---|
| `users.json` | 含 DROP TABLE / 永真式 / UNION SELECT | 3 条高危 SQL 注入 |
| `orders.csv` | 含 `; cat /etc/passwd` | 1 条高危命令注入 |
| `profile.json` | 含 `&& rm`、`SELECT SLEEP(`（自定义规则） | 1 低 + 1 中 |
| `departments.json` | 普通文本 | 无命中 |
| `broken_encoding.csv` | 非 UTF-8 字节（0xff…） | parse_failed |
| `broken_syntax.json` | JSON 语法损坏 | parse_failed |
| `notes.txt` | 合法但扩展名不允许 | skipped_unsupported_extension |

## 验收点逐项实测结果

### 1. 路径遍历名称拒绝、正常文件解析（test_01 通过）

- `validate_entry_name("../evil.json")`、`("sub\\dir.json")` 返回拒绝；
  `safe_resolve` 对 `../evil.json`、`sub\dir.json` 抛 `PathSecurityError`
- 基线报告 `parsed_files` 恰为
  `["departments.json", "orders.csv", "profile.json", "users.json"]`，
  越界名称不出现在任何解析结果中
- 说明：NTFS 不允许文件名含 `/ \ : * ? " < > |` 与控制字符，含 `../` 的
  条目无法在真实目录中创建，因此该路径由单元测试直接驱动安全函数验证

### 2. 注入特征标记、普通文本不标记（test_02 通过）

基线报告实际命中（片段）：

```
users.json  users[1].remark   sql_drop_table    命中 "DROP TABLE"
users.json  users[2].login_hint sql_or_tautology 命中 "' OR '1'='1"
users.json  users[2].note     sql_union_select  命中 "UNION SELECT"
orders.csv  row[2].note       cmd_shell_sep     命中 "; cat"
```

每条记录含 `file_name / field_path / matched_feature / field_value`；
`departments.json`、`orders.csv row[1]`、`users[0].remark` 等普通文本零命中。

### 3. 报告字段与排序（test_03 通过）

基线（含合法自定义规则）共 6 项：高 4、中 1、低 1。输出顺序实测为
high（orders.csv → users.json×3）→ medium（profile.json）→ low（profile.json），
即风险降序、同级按文件名升序；每项均含文件名、漏洞类型、风险等级、修复建议。

### 4. 修复副本对比与原始数据只读（test_04 通过）

`report_fixed.json` 的对比摘要：

```json
{"baseline_total": 6, "current_total": 1, "fixed": 5, "still_open": 1, "new": 0}
```

- `users.json users[1].remark sql_drop_table` 等 5 项状态为 `fixed`
- 刻意保留的 `orders.csv row[2].note cmd_shell_sep` 状态为 `open`
- 只读证据：扫描前后对 `samples/raw` 全部文件计算 SHA-256，哈希完全一致
  （`811a7b4c…`、`8c0133ee…` 等 7 个文件前后相同）

### 5. 损坏文件跳过且标记（test_05 通过）

`parse_failures` 实测包含：

```
broken_encoding.csv -> 非 UTF-8 编码文件: 'utf-8' codec can't decode byte 0xff...
broken_syntax.json  -> JSON 格式损坏: Expecting ',' delimiter (第 2 行)
```

同时 `parsed_files=4`、命中数不受影响（内置 5 项 / 含自定义 6 项）。

### 6. 自定义规则合并、坏配置降级（test_06 通过）

- 合法配置：临时样本 `{"html":"<script>alert(1)</script>"}` 被
  `custom_xss_script`（high）命中，同文件普通字段零命中；
  raw 样本中 `SELECT SLEEP(` 被 `custom_sql_sleep` 命中，内置规则同时生效
- 损坏配置 `rules_broken.json`：报告
  `配置文件 JSON 语法错误: Illegal trailing comma...`，
  内置 5 条规则照常全部命中
- 部分损坏 `rules_partial.json`：非法条目写入 `config_issues` 并跳过，
  合法的 `<script>` 规则正常加载

### 7. 越界访问拒绝并写安全日志（test_07 通过）

对 `check_external_path` 传入 `../outside.json` 与绝对路径均抛
`PathSecurityError`；`out/security.log.jsonl` 实测记录：

```json
{"event": "path_rejected", "requested": "../escape_probe.txt",
 "root": "...\\samples\\raw", "reason": "请求路径越出授权根目录（绝对路径或父目录引用）",
 "result": "DENIED"}
```

CLI 每次启动还会对父目录引用做一次边界自检，拒绝同样落日志。

### 8. CLI 指定路径与缺省输出（test_08 通过）

- `python -m vuln_scanner -i samples/raw -o out/report_raw.json`
  在指定位置生成报告，退出码 0
- 不带 `-o` 运行时在当前工作目录生成 `report.json`（实测于
  `out/default_demo/` 目录验证）

## 复跑命令清单

```powershell
python -I tools/build_samples.py
python -I -m unittest discover -s tests -v
python -m vuln_scanner -i samples/raw -c config/rules.json -o out/report_raw.json
python -m vuln_scanner -i samples/fixed -c config/rules.json -b out/report_raw.json -o out/report_fixed.json
python -m vuln_scanner -i samples/raw -c config/rules_broken.json -o out/report_badcfg.json
```

## 已知边界与说明

1. 扫描为非递归单层目录，符合“用户上传目录”场景；子目录/联接作为
   非常规文件跳过，符号链接指向根目录外会被真实路径校验拒绝。
2. Windows 隔离模式 `python -I` 会屏蔽 cwd，导致 `-m vuln_scanner`
   无法定位本项目包；CLI 演示从可信项目根目录以常规模式运行，
   样本目录与脚本目录分离，样本仅以路径参数传入、从不作为代码加载。
3. 规则为确定性特征匹配，定位已知危险模式；参数化查询等根因修复建议
   已写入每条命中的 `advice` 字段。
