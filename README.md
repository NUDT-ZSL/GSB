# authcfg-scan — .authcfg 授权配置样本漏洞检测工具

对本地安全团队的自定义授权配置文件（`.authcfg`，JSON 格式）做静态检测，
产出 UTF-8 JSON 漏洞检测报告，区分 **可触发漏洞 / 格式畸形 / 安全 / 跳过** 四类样本，
并支持修复开关用于修复前后对比。仅使用 Python 3.10+ 标准库，检测全程只读样本。

## 文件

| 文件 | 作用 |
|---|---|
| `authcfg_scanner.py` | 检测工具本体（CLI） |
| `generate_samples.py` | 确定性样本生成器（无随机、无时间戳，重复运行逐字节一致） |
| `run_checks.py` | 13 条验收要求的自动化验证脚本 |

## .authcfg 格式约定

```json
{
  "version": 2,                 // int, 允许范围 [1, 3]
  "subject": "alice",           // str, 非空
  "resource_path": "data/q3.txt", // str, 解析后不得越出样本目录
  "max_connections": 64,        // int, 允许范围 [0, 1024]
  "timeout_ms": 5000            // int, 允许范围 [0, 60000]
}
```

判定规则：

- **vulnerable**：`resource_path` 解析（realpath）后越出样本目录（路径上跳/绝对路径逃逸）；
  或数值字段超出允许范围（负数、超上限）。`reason` 给出具体依据。
- **malformed**：非法 JSON、顶层非对象、必需字段缺失或类型不符、空字符串字段。
- **safe**：字段合法且无越界风险，`reason` 为空字符串。
- **skipped**：单文件超过 1MB，不解析，计入 summary。
- 修复开关 `--fix`：路径越界收拢进样本目录、数值钳制到允许区间，
  原 vulnerable 样本转为 safe（附 `fixed` 字段说明修复动作）；malformed/skipped 分类不变。

## 快速开始（可复现）

```powershell
# 1. 确定性生成样本集（8 个样本 + 1 个非样本文件）
python generate_samples.py --dir demo_run\samples

# 2. 修复前扫描
python authcfg_scanner.py --samples demo_run\samples --output demo_run\out\report.json
#    -> scanned 8 sample(s): vulnerable=3 malformed=3 safe=1 skipped=1

# 3. 修复后对比扫描
python authcfg_scanner.py --samples demo_run\samples --output demo_run\out\report_fixed.json --fix
#    -> vulnerable=0, safe=4, malformed=3, skipped=1

# 4. 一键跑全部 13 条验收
python run_checks.py
```

退出码：`0` 扫描完成；`2` 样本目录不存在或无 `.authcfg` 样本（stderr 有明确提示）。

## 业务操作样例

```powershell
# 空目录 -> 非零退出码 + 提示
python authcfg_scanner.py --samples demo_run\empty_samples --output demo_run\out\x.json
# error: no .authcfg samples found in: ...   (exit code 2)

# 查看报告（PowerShell）
Get-Content demo_run\out\report.json -Encoding UTF8 | ConvertFrom-Json |
  Select-Object -ExpandProperty results | Format-Table file, status
```

## 实际验证结果（2026-11-14 本机运行）

`python run_checks.py` 输出 **13/13 项验收通过**，覆盖：

1. 3 个命名样本报告条目严格按文件名字典序（alpha/mid/zeta）
2. 含畸形样本时 8 个样本全部出现在报告中
3. `../../outside/secret.txt` 上跳路径判 vulnerable 并给出解析路径依据
4. `max_connections=-1`、`timeout_ms=999999999999` 判 vulnerable
5. 缺字段 / 类型不符 / 非法 JSON 均判 malformed
6. 合法样本判 safe 无误报
7. summary 四项之和 = 样本总数（3+3+1+1=8）
8. `--fix` 后 vulnerable=0、safe 1→4、malformed/skipped 不变
9. vulnerable 条目 reason 非空、safe 条目 reason 为空
10. 目录不存在 / 空目录退出码 2 且有提示
11. 1.5MB 样本判 skipped 且计入 summary
12. 报告可解析，所有 status ∈ {safe, vulnerable, malformed, skipped}
13. 运行前后样本目录 9 个文件 SHA-256 哈希一致（全程只读）
