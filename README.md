# 离线访问控制复核工具（access_auditor）

对内网自研 Web 应用的请求记录做**离线**访问控制复核：基于角色-路径策略逐条判定授权结果，
识别**越权访问**与**策略冲突**，并把**畸形输入**与**整体不可用来源**分开呈现，
输出可复核的 `report.json` 与 `malformed.log`。

- 仅依赖 Python 标准库，Python ≥ 3.10（实测 3.14.7）；
- 不使用 Docker / 容器，不访问网络，全部使用本地确定性样本；
- 建议始终以隔离模式运行：`python -I ...`（屏蔽当前目录植入模块的注入风险）。

## 1. 目录结构

```
project41/
├── audit_tool/
│   ├── __init__.py
│   └── auditor.py            # 工具主程序（含 CLI 入口）
├── tests/
│   └── test_auditor.py       # 19 个 unittest 用例，覆盖 13 条验收点
├── samples/                  # 确定性样本（脚本不与样本同目录运行）
│   ├── normal/               # 主样本：3 个 access_*.json 共 5 条记录
│   ├── conflict/             # 同角色同模式 allow/deny 冲突
│   ├── malformed/            # 畸形记录 + 整体不可用 JSON 来源
│   ├── methods/              # PUT / 缺 method / DELETE 方法过滤
│   └── empty_input/          # 目录存在但无匹配文件
└── demo_output/              # 各样本的实际运行产物（report.json / malformed.log）
```

## 2. 输入文件格式

### 请求记录（文件名必须匹配 `access_*.json`，顶层为数组）

```json
[
  {"method": "GET", "path": "/admin/settings", "role": "admin"},
  {"method": "POST", "path": "/admin/edit", "role": "user"},
  {"method": "GET", "path": "/admin/../user/x", "role": "user"},
  {"method": "GET", "path": "/static/main.js"},
  {"method": "GET", "path": "/public/notice", "role": null}
]
```

- `role` 缺失、为 `null` 或空字符串 → 按匿名角色 `anonymous` 处理，不中断；
- `method` 仅接受 `GET`/`POST`；其他方法或缺失 method 的记录**不参与授权判定**（计入总数）；
- `path` 缺失、非字符串、为空或不以 `/` 开头 → **畸形记录**，跳过判定并写 `malformed.log`。

### 策略文件（默认 `policy.json`）

```json
{
  "roles": {
    "admin": ["/admin/*", "/user/*", "/"],
    "user": ["/user/*", "/public/*"],
    "anonymous": ["/public/*"]
  },
  "denials": [
    {"role": "user", "pattern": "/admin/*"}
  ]
}
```

- `roles`：角色 → 允许路径模式数组；星号**仅允许出现在末尾**（如 `/admin/*`），
  无星号则为精确匹配；
- `denials`（可选）：拒绝型策略。同一角色同一模式同时出现在 `roles` 与 `denials` 中
  → 记为**策略冲突**，冲突请求单列，不计入越权；
- 非法通配位置、JSON 损坏、结构不符 → 直接报错并以退出码 1 结束，不生成残缺报告。

## 3. 判定规则（按顺序）

1. **畸形校验**：非对象、缺/非法 `path` → 畸形，写日志；
2. **角色补全**：缺省/null/空 → `anonymous`；
3. **方法过滤**：非 GET/POST（含缺 method）→ 不授权判定，计入总数；
4. **路径规范化**：剥离 `?query`/`#fragment`，折叠 `//`，按 POSIX 规则解析 `.`/`..`，
   如 `/admin/../user/x` → `/user/x`；
5. **静态资源过滤**：`.js/.css/.png/.jpg/.jpeg/.gif/.svg/.ico/.woff/.woff2`
   → 不判越权，计入总数；
6. **授权判定**：
   - 命中本角色冲突模式 → `policy_conflict`（单列）；
   - 命中本角色 allow → `allowed`；
   - 命中本角色 deny → `unauthorized`（附命中模式）；
   - 本角色无允许规则，但路径命中策略中**任一**模式（含其他角色的模式）
     → `unauthorized`，附 `hit_pattern`；
   - 未命中任何模式 → `no_policy_match`（不计越权，单独列示供审计参考）。

模式匹配语义：`/admin/*` 匹配 `/admin/settings`，**不**匹配 `/user/x`，也不匹配 `/admin` 本身。

## 4. 启动方式（可复现）

在仓库根目录 `D:\demo_GSB\demo\projects\project41` 下执行。

### 4.1 完整参数（推荐演示方式）

```powershell
python -I audit_tool\auditor.py `
  --input-dir samples\normal `
  --policy samples\normal\policy.json `
  --output-dir demo_output\normal
```

### 4.2 默认参数：输入目录=当前目录，策略=当前目录 policy.json

```powershell
Push-Location samples\normal
python -I ..\..\audit_tool\auditor.py --output-dir ..\..\demo_output\defaults
Pop-Location
```

### 4.3 专项样本

```powershell
# 策略冲突（越权与冲突分列）
python -I audit_tool\auditor.py --input-dir samples\conflict --policy samples\conflict\policy.json --output-dir demo_output\conflict
# 畸形记录 + 整体不可用来源
python -I audit_tool\auditor.py --input-dir samples\malformed --policy samples\malformed\policy.json --output-dir demo_output\malformed
# 方法过滤（PUT/DELETE/缺 method）
python -I audit_tool\auditor.py --input-dir samples\methods --policy samples\methods\policy.json --output-dir demo_output\methods
# 空输入目录（报告仍生成、计数全 0、带 notice）
python -I audit_tool\auditor.py --input-dir samples\empty_input --policy samples\empty_input\policy.json --output-dir demo_output\empty
```

### 4.4 失败场景（退出码 1，且无 report.json）

```powershell
python -I audit_tool\auditor.py --input-dir samples\empty_input --policy samples\empty_input\missing-policy.json --output-dir demo_output\should_not_exist
# 控制台：错误: 策略文件不存在: ...；退出码 1；demo_output\should_not_exist\report.json 不存在
```

### 4.5 运行测试

```powershell
python -I tests\test_auditor.py -v
```

## 5. 输出说明

- `report.json`
  - `summary`：`processed_total`（已处理总数，含畸形/过滤项）、`unauthorized_count`、
    `conflict_count`、`malformed_count`、`source_failure_count`、`ignored_method_count`、
    `static_skipped_count`、`allowed_count`、`no_policy_match_count`、
    `roles_involved`（**字典序**）、`count_cross_check`（与总数恒等，供交叉复核）；
  - `unauthorized_requests`：越权条目，均带 `hit_pattern` 命中模式与来源文件/序号；
  - `policy_conflicts`：请求级冲突条目；`policy_definition_conflicts`：策略定义级冲突；
  - `malformed_records`：畸形条目（来源文件、记录序号、原因）；
  - `unavailable_sources`：整体不可用来源（如 JSON 解析失败），与畸形分开；
  - `ignored_by_method` / `static_resources_skipped` / `allowed_requests` /
    `no_policy_match_requests`：全量分流，保证每条请求都可追溯；
  - 无匹配文件时带顶层 `notice`，`summary.notice = "no_matching_input_files"`。
- `malformed.log`：每行一条，形如
  `access_bad.json #1 畸形记录，原因: 缺少 path 字段`；来源级错误同样单列一行。
- 报告先写 `.tmp` 再 `os.replace` 原子替换，任何前置校验失败都不会留下残缺报告。

## 6. 验收点与实测结果对照（2026-11-14 本机实测）

| # | 验收要求 | 实测结果 |
|---|---|---|
| 1 | 3 文件 5 记录全覆盖、按文件名字典序 | `input_files_found=3`，顺序 `access_001/002/003.json`，`processed_total=5` ✅ |
| 2 | `/admin/*` 匹配 `/admin/settings`、不匹配 `/user/x` | 单测 `test_pattern_trailing_wildcard` 通过；admin 放行、user 越权 ✅ |
| 3 | 缺/null 角色按匿名不中断 | `role=null` 记录以 `anonymous` 放行 `/public/notice`，报告仍含该请求 ✅ |
| 4 | PUT/缺 method 不产生越权 | methods 样本 `ignored_method_count=3`、`unauthorized_count=0` ✅ |
| 5 | `/admin/../user/x` 等价 `/user/x` | 报告中规范化为 `/user/x` 并按 user 放行 ✅ |
| 6 | 静态资源不判越权但计入总数 | `/static/main.js` 进 `static_resources_skipped`，总数仍为 5 ✅ |
| 7 | user 访问 `/admin/edit` 输出越权并附 `/admin/*` | `unauthorized_requests[0].hit_pattern = "/admin/*"` ✅ |
| 8 | 冲突单列、不计越权 | conflict 样本 `conflict_count=1`、`unauthorized_count=1`（另一条越权），互不混计 ✅ |
| 9 | 缺 path 写 malformed.log 含来源/序号/原因 | 日志含 `access_bad.json #1 畸形记录，原因: 缺少 path 字段` ✅ |
| 10 | 汇总与列表长度一致、角色字典序 | `count_cross_check == processed_total`；角色 `admin, anonymous, user` 字典序 ✅ |
| 11 | 空目录仍出报告、计数 0、注明情况 | 报告存在，全计数 0，带 `notice` 说明无匹配文件 ✅ |
| 12 | 输入/策略缺失不可读：报错 + 退出码 1 + 无残缺报告 | 实测退出码 1，stderr 可读错误，`report.json` 不存在 ✅ |
| 13 | CLI 支持 `--input-dir` / `--policy`（默认当前目录 / policy.json） | 自定义参数与默认参数两种方式均实测通过 ✅ |

测试结果：`Ran 19 tests ... OK`（全部通过）。
