# 内网文档归档服务 路径逃逸检测与修复验证

在本地授权目录内，对归档服务的文件名清单做路径逃逸检测，并验证修复后组件：
合法文件名（含中文、空格、多级扩展名）正常归档，越界文件名被拒绝并给出可读原因。
仅使用 Python 3.10+ 标准库，无容器、无外部数据、不访问未授权目标。

## 目录结构

```
src/archive_service.py   归档服务：VulnerableArchiver（修复前，仅对照演示）/ FixedArchiver（修复后）
src/detector.py          检测入口：读清单逐条判定，输出 UTF-8 JSON 报告（只读清单，不写被测目标）
src/generate_samples.py  确定性样本生成：清单 + 归档目录 + 归档目录外诱饵文件
src/submit.py            业务操作样例：运维提交清单，修复后组件逐条归档或拒绝
verify.py                一键验证入口：16 项检查覆盖全部验收点
testroot/                生成的授权测试根目录（清单、归档目录、诱饵、报告、沙箱）
```

## 环境要求

Windows + Python 3.10 及以上，仅标准库。

## 一键验证（推荐入口）

```powershell
python verify.py
```

自动完成：生成确定性样本 → 检测混合/全安全清单 → 校验诱饵未变 → 删除一份清单复测 →
修复后组件归档与拒绝 → 沙箱内实证修复前组件的路径逃逸缺陷。
全部通过退出码为 0，任一失败退出码为 1。

## 可复现的分步启动方式

```powershell
# 1. 生成确定性样本（每次运行结果一致）
python src/generate_samples.py --root testroot

# 2. 检测混合清单，输出 JSON 报告；存在风险时退出码为 1
python src/detector.py --archive-root testroot\archive --manifest testroot\manifests\mixed.json --out testroot\reports\report_mixed.json

# 3. 检测全安全清单；全部安全时退出码为 0
python src/detector.py --archive-root testroot\archive --manifest testroot\manifests\all_safe.json --out testroot\reports\report_safe.json

# 4. 多来源检测；某份清单不可读时其余条目照常出结果，缺失来源列入 missing_sources
python src/detector.py --archive-root testroot\archive --manifest testroot\manifests\mixed.json --manifest testroot\manifests\extra.json --out testroot\reports\report_multi.json
```

## 业务操作样例（运维提交清单归档）

```powershell
# 合法清单：全部归档成功，退出码 0
python src/submit.py --archive-root testroot\archive --manifest testroot\manifests\all_safe.json

# 混合清单：越界条目被拒绝并打印可读原因，退出码 1，归档目录外无新文件
python src/submit.py --archive-root testroot\archive --manifest testroot\manifests\mixed.json
```

## 报告字段（固定语义，UTF-8 JSON）

| 字段 | 含义 |
| --- | --- |
| `report_type` | 报告类型标识 |
| `archive_root` | 归档目录绝对路径 |
| `summary` | `total/risk/invalid/safe/sources_total/sources_missing/complete` 汇总 |
| `missing_sources` | 不可读来源 `[{source, error}]`，完整时为空数组 |
| `entries[].index` | 条目在全部输入中的原始序号，可对应回原样本 |
| `entries[].level` | `risk` / `invalid` / `safe`；报告按此排序，同级保持原始顺序 |
| `entries[].reason` | 风险/无效原因；安全条目固定为 `null` |
| `entries[].before` | 修复前判定 `{verdict: escape/inside/invalid, would_write_outside, resolved_path}` |
| `entries[].after` | 修复后判定 `{verdict: accept/reject, reason, archived_path}` |

## 实际验证结果（2026-11-14，Python 3.14.7，Windows 11）

- `python verify.py`：16 项检查全部 PASS，退出码 0。
- 混合清单检测：风险 4（`..\escape.txt`、`../../etc/pwned.txt`、`C:\Windows\Temp\evil.txt`、`/etc/shadow`）、
  无效 2（空文件名、缺失 content）、安全 4；报告顺序 index=[1,3,5,8,4,7,0,2,6,9]，同级保持输入顺序；退出码 1。
- 全安全清单检测退出码 0；`报表 2026-11-14.final.csv`、`sub/dir/日志 备份.tar.gz`、`季度 报告.draft.v2.md`
  归档成功且落盘内容与提交内容逐字节一致。
- 检测前后归档目录外诱饵文件（含逃逸目标金丝雀 `testroot/escape.txt`）内容与修改时间不变。
- 删除 `extra.json` 后复测：其余 10 条正常出结果，`missing_sources` 明确列出缺失来源，`complete=false`。
- 沙箱实证：修复前组件把 `../pwned.txt` 写到了归档目录之外（`testroot/sandbox/pwned.txt`），
  同一样本被修复后组件拒绝，原因为“文件名包含父目录跳转 '..'”。
