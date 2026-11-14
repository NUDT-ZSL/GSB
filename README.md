# 本地授权业务 HTTP 服务样本 —— 敏感信息暴露自查工具

面向本地自建授权业务的 HTTP 服务配置与响应样本，在 Windows 上离线完成敏感信息暴露自查，
输出确定性的 JSON 检测报告，并支持修复前后样本重新比对以确认暴露已消除。

- 纯 Python 标准库实现，无需安装第三方依赖；
- 全程只读写本地文件，**不发起任何网络请求、不访问任何外部目标**；
- 不使用 Docker 或其他容器环境；
- 同一输入多次运行输出**逐字节一致**（固定排序、固定 UTF-8/LF 编码、无时间戳/随机数）。

## 1. 环境要求

- Windows 10/11，Python 3.10+（本机实测 Python 3.14.7）；
- 仅使用标准库：`argparse / json / re / pathlib / dataclasses / unittest`。

## 2. 目录结构

```
project48/
├─ scanner/
│  ├─ __init__.py          # 版本常量
│  ├─ __main__.py          # python -m scanner 入口
│  ├─ cli.py               # generate / scan / compare 三个子命令
│  ├─ models.py            # 统一字段、严重级别与结论枚举、数据模型
│  ├─ detector.py          # 确定性检测规则（响应头 / 正文凭据 / 可疑线索）
│  ├─ engine.py            # 清单加载、逐样本扫描、排序编号、报告组装
│  ├─ compare.py           # 修复前后报告比对
│  ├─ report_io.py         # 确定性 JSON 读写（UTF-8、LF、sort_keys）
│  └─ sample_generator.py  # 确定性样本/损坏样本/清单生成器
├─ tests/
│  └─ test_scanner.py      # 对应 12 项验收要求的 15 个测试
├─ run_scan.py             # python -I 隔离模式运行入口
├─ run_tests.py            # python -I 隔离模式测试入口
├─ demo.ps1                # 一键端到端演示（生成→扫描→复扫→比对→哈希校验）
├─ README.md
└─ VERIFICATION.md         # 实际验证结果记录（含验收逐条映射）
```

## 3. 可复现的本地启动方式

所有命令均在仓库根目录 `project48/` 下执行。

### 3.1 一键演示（推荐）

```powershell
./demo.ps1
```

脚本依次完成：确定性生成样本 → 首次扫描 → 同输入二次扫描并比对 SHA256 →
修复版复扫 → 修复前后比对。产物落在 `demo_output/`。

### 3.2 分步操作（业务操作样例）

```powershell
# 步骤 1：生成确定性样本工作目录（含正常、损坏、缺失、排除样本）
python -m scanner.cli generate demo_workdir

# 步骤 2：按清单扫描修复前样本，输出 JSON 报告
python -m scanner.cli scan demo_workdir -o demo_output/report_before.json

# 步骤 3：对声称已修复的样本目录重新扫描
python -m scanner.cli scan demo_workdir/fixed_samples -o demo_output/report_after.json

# 步骤 4：比对两份报告，确认每条暴露是“已消除 / 未消除 / 新增”
python -m scanner.cli compare `
    demo_output/report_before.json `
    demo_output/report_after.json `
    -o demo_output/comparison.json
```

### 3.3 隔离模式（处理来源不可信的样本目录时推荐）

`python -I` 关闭用户站点包与环境变量注入、跳过当前目录隐式导入，
避免样本目录中被植入的同名模块（如 `json.py`）被解释器加载：

```powershell
python -I run_scan.py generate demo_workdir
python -I run_scan.py scan demo_workdir -o demo_output/report.json
python -I run_tests.py
```

### 3.4 运行测试

```powershell
python -m unittest discover -s tests -v
# 或隔离模式：
python -I run_tests.py
```

## 4. 样本与清单约定

样本为 JSON 文件，结构固定：

```json
{
  "sample_version": 1,
  "response": {
    "headers": {"Server": "nginx/1.22.1"},
    "body": {"password": "..."}
  }
}
```

同目录下的 `manifest.json` 声明纳入检测的样本：

```json
{
  "manifest_version": 1,
  "samples": [
    {"sample_id": "S01", "sample_file": "samples/S01_server_version.json", "declaration": "included"},
    {"sample_id": "S07", "sample_file": "samples/S07_excluded.json", "declaration": "excluded"}
  ]
}
```

- 仅 `declaration = "included"` 的条目纳入扫描，排除项不出现在报告中；
- 样本文件缺失、不可读、JSON 不可解析或结构无效 → 该样本标记为 **未完成**，
  给出 `error` 原因、`findings` 为空，绝不视为“无问题”；
- 生成器同时产出 `fixed_samples/` 及独立清单，用于修复后复扫。

内置确定性样本集：

| 样本 | 场景 | 预期结论 |
|---|---|---|
| S01 | Server 带版本号 + X-Powered-By 标识头 | 已确认暴露（中、低各 1 条） |
| S02 | 明文 password 字段 + token 字段重复两处 + 无版本 Server 头 | 已确认暴露（高 1、中 1、低 1） |
| S03 | 干净健康检查响应 | 合规 |
| S04 | 非结构化文本中的 AWS Key / Bearer 形态线索 | 可疑（低，2 条） |
| S05 | 故意写入的损坏 JSON | 未完成 |
| S06 | 清单声明但文件缺失 | 未完成 |
| S07 | 含口令但声明 excluded | 不扫描、不出现 |

## 5. 检测规则与分级

严重级别仅允许 **高 / 中 / 低**，结论仅允许 **已确认暴露 / 可疑 / 未完成 / 合规**。

| 规则组 | 检测内容 | 结论 | 级别 |
|---|---|---|---|
| 响应头 | `Server` 含可识别版本号 | 已确认暴露 | 中 |
| 响应头 | `Server` 仅有服务标识、无版本 | 已确认暴露 | 低 |
| 响应头 | `X-Powered-By` / `X-AspNet-Version` / `X-Runtime` | 已确认暴露 | 低 |
| 正文 JSON | `password`/`secret`/`private_key` 等明文凭据字段 | 已确认暴露 | 高 |
| 正文 JSON | `token`/`api_key`/`access_token` 等可用令牌字段 | 已确认暴露 | 中 |
| 正文文本 | PEM 私钥块起始标记 | 已确认暴露 | 高 |
| 正文文本 | AWS Access Key 形态、Bearer 令牌形态（无法证实真伪） | 可疑 | 低 |

- 同一暴露在同一样本重复出现 → 合并为一条，`occurrences` 注明次数，位置逐一列出；
- 每个样本内条目按 高 → 中 → 低、已确认优先于可疑、类别/位置字典序固定排列；
- 证据一律脱敏（仅保留前 4 个字符与原始长度），报告本身不二次泄漏敏感值；
- `remediation` 只描述应移除或替换的暴露内容，为自然语言、不含代码片段；
  无适用建议时字段为 `null`，全报告不允许空字符串占位。

## 6. 报告字段说明

报告顶层：`scanner_version`、`scan_root`、`summary`、`samples`。

样本对象统一字段：

| 字段 | 含义 |
|---|---|
| `sample_id` / `sample_file` | 样本标识与清单声明路径 |
| `status` | `完成` 或 `未完成` |
| `conclusion` | `已确认暴露` / `可疑` / `合规` / `未完成` |
| `error` | 未完成原因，正常样本为 `null` |
| `findings[]` | 暴露条目数组，未完成样本为空数组 |

暴露条目统一字段：`finding_id`、`category`、`severity`、`conclusion`、
`location`（头名称或正文行列/字段路径）、`field_name`、`evidence`（脱敏）、
`occurrences`、`remediation`。

比对报告 `comparison.json` 中每条目增加 `status`：

- `已消除`：修复前存在、修复后消失；
- `未消除`：两份报告均存在；
- `新增`：修复前不存在、修复后新出现；
- 未完成样本单列于 `incomplete_samples`，不参与条目比对。

## 7. 确定性保证

- 报告不含时间戳、主机名、随机数、哈希顺序依赖；
- JSON 固定 `sort_keys=True`、`ensure_ascii=False`、两空格缩进、LF 行尾、无 BOM；
- 样本与清单由代码固定生成；清单条目额外按 `sample_id` 排序兜底；
- 连续两次运行的输出文件 SHA256 相同（演示脚本内置该校验）。
