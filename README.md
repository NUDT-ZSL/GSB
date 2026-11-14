# Windows 目录文件权限基线核查工具

递归扫描指定目录下的**普通文件**，读取每个文件的 Windows 访问控制项（DACL/ACE），
对照 JSON 基线规则输出风险清单；无法完成核查的文件单独列入“未能核查”清单。
**仅使用 Python 标准库**（`ctypes` 调用 Win32 安全 API），无任何第三方依赖，
不使用容器。

## 1. 文件清单

| 文件 | 作用 |
| --- | --- |
| `file_audit.py` | 核查工具本体（命令行入口） |
| `tests/build_fixture.py` | 构建确定性本地测试夹具（目录树 + 7 个规则文件） |
| `tests/lock_holder.py` | 占锁辅助程序，制造共享冲突（WinError 32） |
| `tests/run_demo.py` | 端到端验证编排器，覆盖全部 11 条验收点（33 项断言） |
| `tests/test_file_audit.py` | 标准库 `unittest` 单元测试（16 项） |

## 2. 环境与启动

要求 Windows + Python 3.10 及以上（实测环境 Windows 11、Python 3.14.7、非管理员）。

```powershell
# 一键复现：构建夹具、自动占锁、执行 11 项验收并打印真实 JSON 报告
python -I tests/run_demo.py

# 仅单元测试
python -I -m unittest discover -s tests -p "test_*.py" -v

# 手工运行（先构建夹具）
python -I tests/build_fixture.py
python -I file_audit.py tests\workspace\sample_tree tests\workspace\rules_valid.json
```

> 统一加 `-I`（隔离模式），避免从被扫描目录加载到同名恶意模块（如伪造的 `json.py`）。
> 工具在启动时把 stdout/stderr 固定为 UTF-8 并尝试切换控制台到 65001 代码页，
> 因此报告重定向到文件时字节确定为 UTF-8，中文提示在交互终端也可正常显示。

### 命令行参数与退出码

```
python file_audit.py <目录路径> <规则文件.json>
```

| 退出码 | 含义 |
| --- | --- |
| `0` | 扫描完成（**即使存在未能核查的文件**也是 0） |
| `2` | 前置输入错误：目录不存在/不可访问、规则文件非法等；此时**不产生报告**（stdout 为空，错误走 stderr） |

## 3. 规则文件格式

JSON 数组，每项**必须且只能**含 `identity` 与 `access` 两个字段；
`access` 仅允许字符串 `"read"` 或 `"write"`，任何其他取值、缺字段、多字段、
顶层非数组、JSON 语法错误都会导致**整体拒绝**（退出码 2，不输出报告）。

```json
[
  { "identity": "S-1-5-32-545", "access": "write" },
  { "identity": "S-1-1-0", "access": "read" }
]
```

- `identity` 推荐填 **SID 字符串**（语言中立，中文/英文系统均可复现）；
  也支持账户名，匹配时大小写不敏感，兼容 `DOMAIN\name` 与裸 `name` 两种写法。
- 规则按字面逐条执行：同一身份/访问类型写两条就会产生两条记录（不去重），
  空数组 `[]` 是合法规则，表示不产生任何风险记录。

## 4. 报告结构（stdout，UTF-8 JSON）

```json
{
  "scan_root": "D:\\...\\sample_tree",
  "risks": [
    { "file": "D:\\...\\acct_writable.txt", "identity": "BUILTIN\\Users", "access": "write" }
  ],
  "unchecked": [
    { "file": "D:\\...\\locked.txt",
      "reason": "[WinError 32] 共享冲突：文件正被其他进程独占占用，暂时无法读取访问控制项" }
  ]
}
```

- 风险记录三字段 `file` / `identity` / `access` 均为字符串，`file` 一律为绝对路径。
- 排序：`risks` 按 `file` 升序；同一文件再按 `identity`、再按 `access` 升序。
  `unchecked` 按 `file` 升序，与风险清单**分开展示**。

## 5. 关键技术设计与实测结论

### 5.1 ACE 读取：句柄式而非路径式

最初按常规做法使用 `GetNamedSecurityInfoW`（按路径取安全描述符）。实测发现：
**NTFS 的安全描述符独立于数据流，按路径读取完全不受文件独占锁影响**，
无法捕获需求 8 的共享冲突。最终实现改为：

1. `CreateFileW` 先以 `GENERIC_READ` + 全共享位打开文件 → 独占占用时真实返回
   `WinError 32/33`，归入未能核查；
2. 若返回 `WinError 5`（账号无数据读权限），回退为仅请求
   `READ_CONTROL | SYNCHRONIZE` 重新打开——读安全描述符只需要该权限，
   保证“能读元数据但不能读数据”的文件仍可完成核查；
3. `GetSecurityInfo`（按句柄）取 DACL，本地内存由 `LocalFree` 释放。

### 5.2 访问位如何折算 read / write

只统计**数据面**权限，不含 `READ_CONTROL`/`SYNCHRONIZE`/`WRITE_DAC`/
`WRITE_OWNER` 等管理位：

- read：`FILE_READ_DATA | FILE_READ_EA | FILE_READ_ATTRIBUTES` 或 `GENERIC_READ`
- write：`FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA |
  FILE_WRITE_ATTRIBUTES` 或 `GENERIC_WRITE`
- 完全控制（`:F`）同时命中 read 与 write；只读（`:R`）不命中 write。

**Deny ACE 不参与命中**——它是限制性条目，不授予任何访问。普通文件上极罕见的
对象/回调 ACE 类型无法可靠解析时，文件直接转入未能核查，避免静默漏报。
NULL DACL（语义为“所有人完全控制”）无法归因到具体身份，本工具不生成风险记录，
需人工关注。

### 5.3 符号链接与 junction

`DirEntry.is_symlink()` 在 Windows 上只识别 `IO_REPARSE_TAG_SYMLINK`，
对目录联接点（junction）返回 `False`（Python 3.14 实测）。工具额外检查
`FILE_ATTRIBUTE_REPARSE_POINT` 属性位，因此**符号链接与 junction 都被跳过**，
但被链接指向的真实目录仍通过其真实路径正常核查。当前环境非管理员且未开启
开发者模式，无法创建符号链接，夹具自动回退创建 junction（同属 reparse point，
走同一条跳过路径），验证等价。

### 5.4 故障隔离

- 单文件读取 ACL 的任何异常都被捕获并写入 `unchecked`，扫描继续；
- 子目录本身无法枚举（权限等）时，该目录作为一条 `unchecked` 记录，其余目录继续；
- 风险匹配与未能核查互不影响。

### 5.5 不存在的身份

规则中的身份若在任何 ACE 中都匹配不到（如夹具里的
`S-1-5-21-0-0-0-9999`），自然不产生任何记录，同文件中的其他规则照常命中。

## 6. 验收点与实测结果对照

`tests/run_demo.py` 实测输出 **33 项断言全部 PASS**；单元测试 **16 项全部 OK**。

| # | 验收点 | 验证方式与结果 |
| --- | --- | --- |
| 1 | 目录不可用非零退出 | 不存在目录 → 退出 2、stderr“目录不可用”、stdout 空；另测无权限目录 → WinError 5、退出 2 |
| 2 | 非法 access 整体拒绝 | 4 类坏规则（非法 access/缺字段/非数组/JSON 语法错）均退出 2、零报告、stderr 有提示 |
| 3 | 跳过链接、只查普通文件 | junction 与（有权限时的）符号链接均不出现；真实目录文件正常核查 |
| 4 | write 命中 | `acct_writable.txt` 对 Users 可写 → 1 条 write 记录 |
| 5 | 同文件多规则逐条列出 | `multi_rw.txt` 对同一身份输出独立的 read、write 两条 |
| 6 | 三字段字符串+绝对路径 | 逐条断言键集合、字符串类型、非空、`os.path.isabs(file)` 成立 |
| 7 | 未能核查清单独立 | 占锁文件进入 `unchecked` 且带原因；风险清单不受影响 |
| 8 | 共享冲突不中断扫描 | `lock_holder.py` 独占占锁 → WinError 32 入清单，其余 7 个文件结果完整 |
| 9 | 两层排序 | 实际键序列与 `sorted()` 完全一致，含同文件多身份样本 |
| 10 | 幽灵身份无记录 | 不存在 SID 零记录，同规则文件内有效 write 规则仍命中 |
| 11 | 退出码语义 | 正常扫描（含 unchecked）退出 0；坏规则退出 2 且无报告 |

## 7. 本地业务操作样例

```powershell
$ws = "tests\workspace"

# 准备样本（含普通文件、子目录、junction、占锁文件及 7 个规则文件）
python -I tests\build_fixture.py

# 正常核查，报告落盘（cmd 原生重定向或管道均可，字节为 UTF-8）
python -I file_audit.py $ws\sample_tree $ws\rules_valid.json | Out-File report.json -Encoding utf8

# 模拟共享冲突：一个终端占锁，另一个终端核查
python -I tests\lock_holder.py $ws\sample_tree\locked\locked.txt 60
python -I file_audit.py $ws\sample_tree $ws\rules_valid.json

# 错误示例（均为退出码 2，不产生报告）
python -I file_audit.py C:\不存在的目录 $ws\rules_valid.json
python -I file_audit.py $ws\sample_tree $ws\rules_bad_access.json
```

## 8. 范围与限制

- 仅核查**普通文件**的 DACL；目录本身、reparse point、设备等不作为被核查对象。
- 身份匹配基于 ACE 中的 SID 与展示名；本地账户/域账户/内置组均可，
  建议规则统一使用 SID 以避免语言差异。
- 未启用特权模式：读取 SACL（审计策略）需要 `SeSecurityPrivilege`，
  本工具只核查 DACL，不需要管理员权限。
- 测试全部使用本地确定性样本，不访问任何未授权目标，不使用 Docker 或容器。
