# 实际验证记录

验证机器：Windows 11（10.0.26300），PowerShell 7+，Python 3.14.7（满足 3.10+ 要求）。
验证方式：纯本地离线运行，无 Docker、无容器、无任何网络访问；样本由
`scanner/sample_generator.py` 确定性生成。

## 1. 测试套件结果

命令：`python -m unittest discover -s tests -v`（隔离模式 `python -I run_tests.py` 结果相同）

```
Ran 15 tests in 0.035s

OK
```

15 个测试全部通过，其中 12 个端到端测试与任务书验收条目一一对应：

| 验收条目 | 对应测试 | 实测结果 |
|---|---|---|
| 1 逐条覆盖不遗漏 | test_01_all_declared_samples_covered | 报告含 S01–S06 共 6 条，排除项 S07 不出现 |
| 2 服务标识/版本头并给出头名 | test_02_server_version_header_flagged_with_header_name | S01 的 Server（中危）与 X-Powered-By（低危）被标记，field_name 给出头名 |
| 3 明文口令高危并指出字段 | test_03_plaintext_password_high_risk_with_field | S02 password 条目 severity=高，field_name=password |
| 4 修复后确认暴露消失 | test_04_fixed_sample_marks_exposure_removed | 比对结果 7 条全部“已消除”，修复版 S01/S02/S04 结论为合规 |
| 5 缺失/损坏列为未完成 | test_05_corrupt_and_missing_samples_incomplete | S05（不可解析）、S06（文件缺失）均 status=未完成、findings=[]、error 有值 |
| 6 坏样本不影响其余结论 | test_06_other_samples_still_evaluated_with_bad_one | S01/S02/S03/S04 四条结论完整可用（2 确认、1 可疑、1 合规） |
| 7 仅高中低且按级别排序 | test_07_severity_values_and_ordering | 级别取值集合 ⊆ {高,中,低}，每条样本内严格高→中→低 |
| 8 重复暴露合并并计数 | test_08_duplicate_exposure_merged_with_count | S02 两处 token 合并 1 条，occurrences=2，位置列出 nested.token 与 backup.token |
| 9 确认与可疑不混类 | test_09_confirmed_vs_suspected_separated | S04 样本结论=可疑且条目全部为可疑；S01/S02 全部为已确认暴露 |
| 10 处置说明无代码片段 | test_10_remediation_has_no_code_snippet | 全部 remediation 为自然语言（无反引号/花括号/语句分隔），均含移除或替换动作 |
| 11 确定性逐字节一致 | test_11_byte_for_byte_deterministic | 两次扫描输出字节相同 |
| 12 缺失值用 null | test_12_null_instead_of_empty_string | 文本线索 field_name=null、正常样本 error=null；全报告无空字符串 |

另有 3 个检测器单元测试：PEM 私钥块判定为高危已确认、无版本 Server 头判定为低危、
干净输入返回零条目，均通过。

## 2. CLI 端到端实跑结果

命令：`./demo.ps1`

```
[1/5] 已生成 12 个文件到 demo_workdir
[2/5] 扫描完成：声明 6 个，完成 4 个，未完成 2 个；
      已确认 2 个，可疑 1 个，合规 1 个；暴露条目共 7 条。
[3/5] 确定性校验通过：两次输出 SHA256 均为
      3A05268E8A37292E9656381CF0D48E101C9B41249E3CA8B3AD30EDC88653A008
[4/5] 修复版扫描：声明 4 个，完成 4 个，未完成 0 个；
      已确认 0 个，可疑 0 个，合规 4 个；暴露条目共 0 条。
[5/5] 比对完成：已消除 7 条，未消除 0 条，新增 0 条；未完成样本 0 个。
```

产物（`demo_output/`）：

| 文件 | 大小（字节） | 内容 |
|---|---|---|
| report_run1.json | 5908 | 修复前完整报告 |
| report_run2.json | 5908 | 同输入第二次扫描，与 run1 逐字节相同 |
| report_fixed.json | 1065 | 修复版复扫报告，4 个样本全部合规 |
| comparison.json | 3306 | 逐条比对结果，7 条均为“已消除” |

## 3. 隔离模式验证

`python -I` 会剥离当前目录隐式导入路径，直接执行 `-m scanner` 不可用，
因此提供 `run_scan.py` / `run_tests.py` 两个隔离入口：

```
python -I run_scan.py scan demo_workdir -o demo_output/report_isolated.json
python -I run_tests.py   → Ran 15 tests ... OK
```

隔离模式产出的 `report_isolated.json` 与常规模式 `report_run1.json` 的 SHA256
同为 `3A05268E8A37292E9656381CF0D48E101C9B41249E3CA8B3AD30EDC88653A008`，
证明隔离运行不影响检测结论与确定性。

## 4. 报告内容抽查（修复前 report_run1.json）

- S01-F01：category=响应头服务版本暴露，severity=中，field_name=Server，
  证据脱敏为 `ngin***（已脱敏，原始长度 12 字符）`；
- S02-F01：category=响应正文凭据字段泄漏，severity=高，field_name=password，
  location=`响应正文 JSON 字段 password`；
- S02-F02：token 字段两处重复合并，occurrences=2，location 同时给出
  `backup.token` 与 `nested.token`；
- S04 两条可疑线索定位到具体行列（第 2 行第 40 列、第 3 行第 30 列），
  conclusion 均为“可疑”，field_name 均为 null；
- S05 error=`样本内容不可解析：JSONDecodeError: ...`，S06 error=`样本文件缺失：...`，
  两者 conclusion=未完成、findings=[]，未被误判为合规。

## 5. 复现步骤

```powershell
python -m unittest discover -s tests -v   # 15 项测试
./demo.ps1                                # 端到端演示与哈希校验
```

删除 `demo_workdir/` 与 `demo_output/` 后重新执行，产物与上述哈希完全一致。
