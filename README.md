# 上传服务两类缺陷：缺陷版 / 修复版 / 检测脚本 / 回归测试

纯 Python 标准库实现（无需网络、无需第三方包），所有样本与请求均由代码
**确定性生成**，只监听本机回环地址 `127.0.0.1`，不使用容器。

## 1. 文件说明

| 文件 | 作用 |
| --- | --- |
| `common.py` | 确定性样本字节、纯标准库的 multipart 构造/解析、JSON 响应、绝对路径泄露检查、扩展名与图片魔数规则 |
| `vuln_server.py` | **故意留缺陷**的上传服务（默认 8001）。缺陷一：文件名直接 `os.path.join` 拼接，`../` 可越出 `uploads`；缺陷二：仅凭结尾扩展名判断图片，不检查内容，`shell.php.jpg` 原样落盘 |
| `fixed_server.py` | **修复版**服务（默认 8002）。文件名净化并拒绝路径片段、全扩展名段检查可执行类型、结尾扩展名白名单、图片魔数校验、落盘路径包含校验（containment） |
| `detect.py` | 检测脚本，`--target vuln|fixed` 选择目标，自动发出两类恶意请求并输出 JSON；不可达时输出 `error` 且两项 `status_code` 均为 `-1` |
| `test_detect.py` | `unittest` 回归测试，覆盖缺陷目标、修复目标、服务不可达三类情形（共 11 个用例） |
| `demo.ps1` / `demo.sh` | 业务演示脚本（PowerShell / Bash） |

两类固定请求：

1. **路径遍历**：文件名 `../escape.txt`，内容为固定文本字节。
2. **伪装可执行文件**：文件名 `shell.php.jpg`，内容为固定的伪 PHP 脚本字节。

## 2. 本地启动与演示（可复现）

需要 Python 3.10+（本机实测 Python 3.14.7 64 位）。打开三个终端，均在本目录下：

### 终端 A —— 启动缺陷服务

```powershell
python vuln_server.py --port 8001 --dir vuln_workdir
```

### 终端 B —— 启动修复服务

```powershell
python fixed_server.py --port 8002 --dir fixed_workdir
```

### 终端 C —— 检测与对比

```powershell
# 对缺陷目标：两项 detected 均为 true
python detect.py --target vuln  --url http://127.0.0.1:8001 --base-dir vuln_workdir

# 对修复目标：两项 detected 均为 false（均返回 400）
python detect.py --target fixed --url http://127.0.0.1:8002 --base-dir fixed_workdir

# 服务不可达（先停掉所有服务）：输出 error，两项 status_code 均为 -1
python detect.py --target vuln
```

或一键演示（含磁盘落点展示）：

```powershell
./demo.ps1 vuln      # Git Bash 用： ./demo.sh vuln
./demo.ps1 fixed
```

### 回归测试

```powershell
python -m unittest test_detect -v
```

## 3. 实测结果记录（2026-11-14，Windows 11 + Python 3.14.7）

### 缺陷服务（vuln_server.py，端口 8001）

- `../escape.txt` 上传返回 **200**，文件实际写入
  `vuln_workdir/escape.txt`（即 `uploads` 的**上级目录**），`uploads/` 内无此文件。
- `shell.php.jpg` 上传返回 **200**，原样保存于
  `vuln_workdir/uploads/shell.php.jpg`，落盘字节与提交字节一致（60 字节）。
- `detect.py --target vuln` 输出两项 `"detected": true`、`status_code: 200`。
- 畸形 multipart 请求返回 **400**，`DELETE /upload` 返回 **405**，响应体均无盘符绝对路径。

### 修复服务（fixed_server.py，端口 8002）

- 重放 `../escape.txt` → **400** `path separators or traversal segments not allowed`，
  工作目录下只有空的 `uploads/`，无任何文件越出。
- 重放 `shell.php.jpg` → **400** `executable extension is not allowed`，
  `uploads/` 内无可执行扩展名文件。
- `detect.py --target fixed` 输出两项 `"detected": false`、`status_code: 400`。
- 正常 1×1 PNG 上传 → **200** 且字节一致；把脚本字节改名为 `evil.png` 上传
  → **400** `content is not a recognised image`（魔数校验拦截，证明不只看扩展名）。
- 所有 4xx/5xx 响应体只含通用错误信息，经正则检查不含 `X:\` 形式的绝对路径。

### 服务不可达

- 未启动任何服务时运行 `detect.py`：JSON 顶层含 `error` 字段，
  两项检查 `status_code` 均为 `-1`、`detected` 均为 `false`，进程退出码 0，不伪造结论。

### 回归测试

```
Ran 11 tests in ~9s

OK
```

覆盖：缺陷服务遍历越界、伪装文件字节一致、检测器对两目标的结论、修复服务
拒绝两类恶意请求、合法 PNG 放行、畸形请求 400、错误方法 405、响应无绝对路径、
不可达 JSON 结构、CLI 子进程黑盒运行。

## 4. 检测脚本 JSON 结构（业务人员阅读）

```json
{
  "target": "vuln",
  "url": "http://127.0.0.1:8001",
  "checks": [
    {
      "check": "path_traversal",
      "description": "路径遍历检测：……（中文说明）",
      "request": "POST /upload, filename='../escape.txt'",
      "status_code": 200,
      "detected": true,
      "evidence": "在 uploads 上级目录发现 escape.txt"
    }
  ]
}
```

每项均含 `detected`、`status_code` 与请求简述（`request` + `description`）；
检测结论优先采用服务端磁盘证据（通过 `--base-dir` 指向服务工作目录），
无法访问磁盘时回退为 HTTP 状态码判断，并在 `evidence` 中注明。

## 5. 安全边界

- 仅绑定 `127.0.0.1`，仅限本机回环验证，请勿暴露到网络。
- 缺陷服务仅用于对照验证，建议放在一次性临时工作目录中运行，验证后直接删除
  `vuln_workdir/`（已在 `.gitignore` 忽略）。
- 全部代码仅使用标准库（`http.server`、`http.client`、`urllib.parse`、
  `json`、`zlib`、`struct`、`re`、`unittest` 等），在仅安装 Python 的
  环境即可运行。
