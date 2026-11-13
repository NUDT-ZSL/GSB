# 文件上传服务路径穿越（Path Traversal）离线检测工具

在**完全离线、无容器**环境中，对本地 Python 文件上传服务源码做静态污点分析，并在隔离沙箱中
构造含 `../` 的恶意上传文件名做模拟利用，输出 JSON 检测报告、可利用性结论与修复前后对比摘要。

仅依赖 **Python 3.10+ 标准库**（实测 Python 3.14.7），不访问网络、不访问未授权目标。

---

## 1. 目录结构

```
project29/
├── scanner/                    # 工具本体（纯标准库）
│   ├── rules.py                # 污点源 / 净化函数 / 危险汇聚点 / 严重级别规则
│   ├── detector.py             # AST 函数内污点分析扫描器
│   ├── simulator.py            # 隔离沙箱模拟利用器（冲突保留/只读/逐字节校验）
│   ├── reporter.py             # JSON 报告、文本摘要、修复前后指纹对比
│   ├── cli.py                  # 命令行入口
│   └── __main__.py             # 支持 python -m scanner
├── tests/test_acceptance.py    # 覆盖任务 1~12 的 12 个验收测试
├── make_samples.py             # 确定性样本生成器（含漏洞/已修复/语法错误）
├── pt_scan.py                  # 项目根入口（任意工作目录可运行）
├── run_demo.py                 # 一键端到端演示
├── examples/sim_config.example.json
└── samples/                    # 由 make_samples.py 生成
    ├── vulnerable/             # 7 个含漏洞服务 + 1 个语法错误文件
    └── fixed/                  # 同名 7 个已修复服务
```

## 2. 本地启动方式

```powershell
# 0) 环境确认（Python 3.10+，标准库可用）
python --version

# 1) 生成确定性样本（任务环境准备）
python make_samples.py

# 2) 扫描 + 模拟利用（指定目录、报告路径、上传内容）
python -m scanner --dir samples/vulnerable `
                  --report out/vuln_report.json `
                  --content "PWNED-CONTENT-BYTE-CHECK"
# 等价入口（任意工作目录均可）：
python pt_scan.py --dir samples/vulnerable --report out/vuln_report.json

# 3) 无参运行：扫描当前目录，报告默认写到 ./out/report.json 与 .txt
python -m scanner

# 4) 一键全流程演示
python run_demo.py

# 5) 验收测试（12 项）
python -m unittest tests.test_acceptance -v
```

### 命令行参数（任务9）

| 参数 | 说明 | 缺省值 |
| --- | --- | --- |
| `--dir/-d` | 待扫描源码目录 | 当前目录 `.` |
| `--report/-o` | JSON 报告输出路径 | `out/report.json` |
| `--text` | 文本摘要输出路径 | 与报告同名 `.txt` |
| `--sim-config` | 模拟配置 JSON 文件 | 内置默认配置 |
| `--payload` | 恶意文件名（含 `../`） | `../pwned_marker.txt` |
| `--marker-name` | 逃逸标记文件名 | `pwned_marker.txt` |
| `--content/--content-file` | 指定上传内容（字符串/二进制文件） | 内置标记内容 |
| `--conflict` | 预置同名文件，验证保留原文件（任务4） | 关 |
| `--read-only` | 只读目录模拟（任务10，失败退出码 2） | 关 |
| `--baseline` | 修复前基线报告，输出 fixed/remaining 对比 | 无 |
| `--no-sim` | 仅静态扫描 | 关 |
| `--sandbox-base` | 沙箱根目录 | 系统临时目录 |

## 3. 检测原理

1. **静态污点分析（AST）**：逐文件 `ast.parse`，在每个函数内跟踪
   `request.FILES/form/args`、函数文件名形参、`input()`、`os.getenv()` 等污点源，
   经字符串拼接、f-string、`os.path.join`、`Path(/)` 等传播到
   `open`/`write_text`/`write_bytes`/`shutil.copy*`/`save`（写入类）与
   `read_bytes`/`remove`/`unlink`（读删类）等汇聚点。
2. **净化分级**：`os.path.basename`、`PurePath.name`、`secure_filename`、`uuid` 重命名
   为强净化（消除污点）；`str.replace` 黑名单、`strip`、纯扩展名 `endswith` 校验为弱净化
   （降为中危，提示可绕过）。
3. **边界守卫识别**：函数内同时存在 `resolve/realpath/commonpath` 与
   `startswith/is_relative_to/commonpath(...)==root` 约束时抑制告警（已修复样例即如此）。
4. **隔离模拟利用**：每条漏洞在独立临时沙箱中调用样例自身的 `save_upload(upload_dir,
   filename, content)` 入口，按漏洞形态自适应 payload：
   - 原始拼接：`../pwned_marker.txt`
   - 弱净化黑名单：`....//pwned_marker.txt`（一次 `replace("../","")` 还原为 `../`）
   - 扩展名白名单：`../pwned_marker.txt.png`
   - 读取类：在逃逸目标预置标记文件，校验越界读取返回内容
   标记文件出现在上传根之外且内容逐字节一致 → `exploitable=true`。

## 4. JSON 报告字段

```json
{
  "summary": {"total": 7, "high": 5, "medium": 1, "low": 1,
              "exploitable": 7, "errors": 1},
  "vulnerabilities": [{
    "id": "V002", "file": "vuln02_os_path_join.py", "line": 7,
    "severity": "高危",
    "function": "save_upload", "sink": "open",
    "taint_source": "用户可控输入", "taint_level": "原始用户输入未净化",
    "code": "with open(target, \"wb\") as fh:",
    "suggestion": "…os.path.basename…Path.resolve…os.path.commonpath…is_relative_to…uuid…",
    "fingerprint": "FP-xxxxxxxxxxxx",
    "simulation": {
      "payload": "../pwned_marker.txt",
      "target_path": "<沙箱>/pwned_marker.txt",
      "outside_upload_root": true, "exploitable": true,
      "content_matches": true, "conflict": null, "error": null, "mode": "write"
    }
  }],
  "errors": [{"file": "vuln08_broken_syntax.py", "error": "SyntaxError",
              "message": "expected ':' (line 5)"}],
  "comparison": {"fixed_count": 7, "remaining_count": 0, "new_count": 0,
                 "per_vulnerability": [{"id": "V001", "status": "已修复", "...": "..."}]}
}
```

`severity` 仅取 **高危 / 中危 / 低危** 三值；条目按 severity 降序、同级按 file 字母序、
再按行号排列，多次运行顺序完全一致（稳定指纹 `fingerprint` 不随行号变化）。

## 5. 任务验收对照（实际运行结果）

| # | 验收项 | 实现位置 | 实测结果 |
| --- | --- | --- | --- |
| 1 | 列出用户输入拼入写入路径的文件与行号 | detector.py | 7 条漏洞均含 file/line，如 `vuln02_os_path_join.py:7` |
| 2 | JSON 含 file/line/severity，severity 三值枚举 | reporter.py | 校验通过，仅 高危/中危/低危 |
| 3 | `../` 模拟后隔离目录外出现标记文件 | simulator.py | 7/7 可利用，uploads 外生成标记文件 |
| 4 | 同名文件保留原内容并记录冲突 | simulator.py | 6 条写入类 `overwritten=false`、`content_preserved=true` |
| 5 | suggestion 含具体函数/校验 | rules.py | 含 basename/commonpath/is_relative_to/secure_filename/uuid |
| 6 | 修复后重扫 fixed_count/remaining_count | reporter.py | fixed_count=7、remaining_count=0、new_count=0 |
| 7 | 语法错误跳过入 errors 不中断 | detector.py | `vuln08` 进 errors，其余 7 文件正常检出 |
| 8 | 指定内容逐字节一致 | simulator.py | 含 `\\x00` 二进制与中文的 content_matches=true |
| 9 | CLI 指定目录/配置/输出，缺省用默认值 | cli.py | 无参写 `./out/report.json`，带参按指定路径 |
| 10 | 只读目录权限提示+非零退出+不崩溃 | simulator.py/cli.py | 明确权限提示，退出码 2 |
| 11 | severity 降序、同级 file 字母序、确定性 | reporter.py | 两次运行条目顺序完全一致 |
| 12 | 修复前后文本摘要逐条编号与状态 | reporter.py | V001..V007 逐条“已修复/未修复”，与对比数据一致 |

## 6. 业务操作样例

```powershell
# A. 常规离线检测（自动模拟利用）
python -m scanner --dir samples/vulnerable --report out/a.json

# B. 指定自定义二进制上传内容，验证逐字节写入
python -m scanner --dir samples/vulnerable -o out/b.json `
                  --payload "../evil.bin" --content-file some.bin

# C. 验证同名文件冲突保护
python -m scanner --dir samples/vulnerable -o out/c.json --conflict

# D. 只读目标目录的失败处理（退出码 2）
python -m scanner --dir samples/vulnerable -o out/d.json --read-only

# E. 修复前后对比
python -m scanner --dir samples/vulnerable --no-sim -o out/baseline.json
python -m scanner --dir samples/fixed -o out/fixed.json --baseline out/baseline.json
```

## 7. 修复建议要点（报告中逐条给出）

1. `os.path.basename(filename)`（或 `PurePath(filename).name`、`secure_filename`）剥离目录分量；
2. `os.path.join(root, safe)` 后用 `os.path.realpath()` / `Path.resolve()` 解析绝对路径；
3. `os.path.commonpath([root, resolved]) == root` 或 `Path.is_relative_to(root)` 做边界校验，失败即拒绝；
4. 服务端用 `uuid.uuid4().hex` 重命名存储，避免信任客户端文件名。

## 8. 安全与约束说明

- 所有模拟均在 `tempfile.mkdtemp` 隔离沙箱内进行，仅写入本地临时目录，进程结束后可手动清理；
- 冲突模式下**先检测同名文件并跳过写入**，绝不覆盖既有文件；
- 不使用 Docker/容器，不发起任何网络请求，样本与 payload 均为本地确定性数据。
