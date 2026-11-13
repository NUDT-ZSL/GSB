# 本地弱口令基线自查工具

对本地自建授权服务的 JSON 账户配置做弱口令基线自查。**纯本地、确定性执行：不联网、不读写真实凭据**，
仅处理命令行指定的样本文件；口令在报告中一律脱敏。

## 1. 环境要求

- Python 3.10 及以上（实测 Python 3.14.7，Windows 11）
- 无第三方依赖，仅使用标准库
- 不使用 Docker 或任何容器环境

## 2. 目录结构

```
project19/
├── password_audit.py              # 主程序（CLI）
├── samples/
│   ├── config.json                # 主样本：5 条合法配置
│   └── config_corrupted.json      # 损坏样本：含缺 name/role、类型错误、非对象等坏条目
├── tests/
│   └── test_audit.py              # 对应 9 条验收点的测试
├── reports/                       # 运行后生成的报告（自动创建）
└── README.md
```

## 3. 判定规则

| 风险等级 | risk_category | 触发条件 |
| --- | --- | --- |
| 高风险 | `weak_password` | 命中内置常见弱口令清单（20 个模拟值） |
| 中风险 | `simple_or_short_password` | 纯数字、纯字母，或长度不足 6 位 |
| 中风险 | `missing_password` | `password` 为 `null` 或字段缺失（**不算空口令、不算通过**） |
| 低风险 | `compliant` | 不满足上述任一条件 |

- `role` 为 `admin` 且命中高/中风险时，结果额外带 `admin_account_risk: true` 与 `admin_risk_note` 管理员账户风险标注。
- 报告排序：先高风险、后中风险、再低风险；同级按 `name` 升序；缺失口令归入中风险参与排序。
- 口令脱敏：仅保留首尾字符，中间以**等长星号**替换，如 `123456` → `1****6`；缺失口令显示 `***`。
- 损坏条目（缺 `name`/`role`、字段类型非法、元素不是对象等）：写一条警告（stderr + 报告 `warnings` 数组，可追溯），跳过该条，其余条目照常检测。

## 4. 启动与业务操作样例

### 4.1 指定输入与输出路径

```powershell
python -I password_audit.py samples/config.json -o reports/report_main.json
```

### 4.2 缺省输出（在当前工作目录生成 report.json）

```powershell
python -I password_audit.py samples/config.json
# 当前目录即出现 report.json
```

### 4.3 含损坏条目的输入（验证容错与警告）

```powershell
python -I password_audit.py samples/config_corrupted.json -o reports/report_corrupted.json
```

stderr 示例（UTF-8）：

```
[WARN] 条目#1 缺少必填字段 name 或 name 非法，已跳过该条目
[WARN] 条目#4 缺少必填字段 role 或 role 非法（仅支持 ('admin', 'user')），已跳过该条目
[WARN] 条目#7 password 字段类型非法（应为字符串或 null），已跳过该条目
[WARN] 条目#8 不是 JSON 对象，已跳过该条目
[INFO] 检测完成：输入 10 条，有效 6 条，跳过 4 条，高风险 2，中风险 3，低风险 1，报告已写入 reports/report_corrupted.json
```

> 说明：`-I` 为隔离模式，忽略环境变量与当前目录下的注入模块（防止恶意 `json.py` 等被导入）。
> 工具启动时会自行把标准流重配置为 UTF-8，因此中文 Windows 的 GBK 代码页下输出仍为合法 UTF-8。

### 4.4 运行测试

```powershell
python -I tests/test_audit.py
```

## 5. 报告结构（摘要）

```json
{
  "report_meta": { "tool": "local-password-baseline-audit", "mode": "local-deterministic", "weak_password_list_size": 20 },
  "summary": { "total_entries_in_input": 5, "entries_audited": 5, "entries_skipped": 0,
               "high_risk": 2, "medium_risk": 3, "low_risk": 0,
               "missing_password": 1, "admin_account_risks": 2 },
  "warnings": [],
  "results": [
    { "name": "alice", "role": "admin", "risk_level": "high", "risk_category": "weak_password",
      "reasons": ["命中内置常见弱口令清单"], "masked_password": "1****6",
      "admin_account_risk": true, "admin_risk_note": "管理员账户风险：管理员账户口令存在高风险" }
  ]
}
```

结果中**不存在任何原始口令字段**，只有 `masked_password` 脱敏值。

## 6. 实际验证结果（2026-11-14，Python 3.14.7 / Windows 11）

| # | 验收点 | 验证方式 | 结果 |
| --- | --- | --- | --- |
| 1 | 5 条合法配置 → 5 条结果，含 name 与风险分类 | `test_01` + 主样本运行 | ✅ audited=5 |
| 2 | `123456` 判高风险（弱口令清单 20 个） | `test_02` | ✅ alice=high |
| 3 | `abcdef`、`12345678` 判中风险 | `test_03` | ✅ 均 medium |
| 4 | admin + `admin123` 含管理员账户风险标注 | `test_04` | ✅ bob 带标注 |
| 5 | null 口令判缺失口令、脱敏且不泄露 | `test_05` | ✅ category=missing_password，显示 `***` |
| 6 | 高风险在前、同级 name 升序、缺失归中风险 | `test_06` | ✅ 顺序 alice,bob → carol,dave,eve |
| 7 | 缺 name/role 等坏条目警告并跳过，其余正常 | `test_07` + 损坏样本 | ✅ 输入10/有效6/跳过4，4 条警告 |
| 8 | CLI 指定路径与缺省 report.json | `test_08` | ✅ 两种路径均生成文件 |
| 9 | 脱敏 `123456`→`1****6`、null→`***` | `test_09` | ✅ |

测试输出：`Ran 9 tests ... OK`（9/9 通过）。

## 7. 输入文件格式约定

顶层为 JSON 数组，每个元素为对象：

```json
[
  { "name": "alice", "role": "admin", "password": "123456" },
  { "name": "eve",   "role": "user",  "password": null }
]
```

- `name`：非空字符串，必填
- `role`：`admin` 或 `user`，必填
- `password`：字符串、`null` 或省略；省略与 `null` 同等视为缺失口令
