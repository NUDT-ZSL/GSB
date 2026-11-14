# 内部工单系统 · 附件预览接口越权读取核验（本地双实例）

被测场景：附件预览接口把请求参数 `name` 直接拼接到附件目录后读取文件。
本仓库提供 **漏洞版（5000）** 与 **修复版（5001）** 两个本地 Flask 实例、
一个检测脚本和一份可复现的业务样例，全部只绑定 `127.0.0.1`，不使用容器、不访问外部对象。

## 1. 环境与安装

已验证环境：Windows 11 + Python 3.10.20 + Flask 2.3.0 + Werkzeug 2.3.7。

```powershell
python --version              # 需要 >= 3.10
python -m pip install flask==2.3.0 werkzeug==2.3.7
```

> Flask 2.3.0 需要 Werkzeug 2.3.x；若环境中已有 Werkzeug 3.x，其 `__version__` 缺失会导致
> Flask 测试客户端报错，检测脚本本身用标准库 `urllib` 不依赖该属性，但服务端仍建议按上面版本对齐。

## 2. 目录结构

```
attachments/                 附件目录（正常附件）
  ├─ 员工手册-2026.txt
  └─ notice-2026-11.txt
outside_attachments/         附件目录之外（仅用于核验越界读取）
  └─ secret_marker.txt       含 MARKER_TOKEN=P66-LOCAL-MARKER-A7F3C9
preview_server/app.py        两个实例共用的服务代码（--variant vulnerable|fixed）
detect.py                    检测脚本：发送三类参数、输出 JSON 报告与 detect.log
samples/curl_examples.py     可复现的业务样例（人工核对用）
start_servers.bat / .sh      一键启动两个实例
reports/                     检测报告输出目录
logs/                        detect.log 输出目录
```

## 3. 启动说明

方式一（Windows 批处理，会开两个新窗口）：

```powershell
.\start_servers.bat
```

方式二（Git Bash / Linux / macOS）：

```bash
./start_servers.sh
```

方式三（手动分别启动）：

```powershell
python preview_server/app.py --variant vulnerable --port 5000
python preview_server/app.py --variant fixed      --port 5001
```

启动成功时控制台输出：

```
variant=vulnerable attachment_dir=E:\Company\demo\projects\project66\attachments listening=http://127.0.0.1:5000
variant=fixed attachment_dir=E:\Company\demo\projects\project66\attachments listening=http://127.0.0.1:5001
```

服务端仅允许回环地址：`--host` 非 `127.0.0.1/localhost/::1` 时直接退出。

## 4. 运行检测

```powershell
python detect.py
# 可选：自定义实例、报告与日志路径
python detect.py --targets http://127.0.0.1:5000:vulnerable http://127.0.0.1:5001:fixed `
                 --out reports/report.json --log logs/detect.log
```

脚本先用正则解析 `http://host:port:variant`，再校验主机名必须在允许列表内，
任何非本地地址会以“参数错误”退出（退出码 2），不会发出请求。

### 三类请求参数（共 5 条）

| 类别 `param_class` | 参数 `name` | 说明 |
| --- | --- | --- |
| `normal` | `员工手册-2026.txt` | 附件目录内的正常附件（基线） |
| `traversal` | `../outside_attachments/secret_marker.txt` | `../` 跳出附件目录 |
| `traversal` | `..\outside_attachments\secret_marker.txt` | Windows 反斜杠变体 |
| `absolute` | `E:\...\outside_attachments\secret_marker.txt` | Windows 绝对路径 |
| `absolute` | `E:/.../outside_attachments/secret_marker.txt` | 正斜杠绝对路径 |

### 判定规则

- 越界参数（traversal / absolute）返回 **200 且响应体含 `MARKER_TOKEN`** → 该实例 `风险`，证据记录状态码与标记文件内容片段；
- 越界参数返回 **400/403/404**，或 200 但响应体不含标记 → 该实例 `已修复`；
- 实例连接失败/超时 → 该实例及其每条参数记录均为 `未完成`，并写入 `unreachable_instances`；
- 单个实例不可达不中断其余实例的检测，脚本始终退出码 0（参数错误除外）。

## 5. 实际运行结果（2026-11-14 本地实测）

### 5.1 两个实例均在线

```
[+] 报告已写入：E:\Company\demo\projects\project66\reports\report.json
[+] 日志已写入：E:\Company\demo\projects\project66\logs\detect.log
    - http://127.0.0.1:5000 (vulnerable): 风险
    - http://127.0.0.1:5001 (fixed): 已修复
```

漏洞版（5000）关键条目（摘自 `reports/report.json`）：

```json
{
  "instance": "http://127.0.0.1:5000",
  "variant": "vulnerable",
  "param_class": "traversal",
  "parameter": "../outside_attachments/secret_marker.txt",
  "request_url": "http://127.0.0.1:5000/api/attachment/preview?name=..%2Foutside_attachments%2Fsecret_marker.txt",
  "status_code": 200,
  "response_snippet": "=== OUTSIDE-ATTACHMENTS MARKER === / MARKER_TOKEN=P66-LOCAL-MARKER-A7F3C9 / 说明：本文件位于附件目录之外…",
  "marker_leaked": true,
  "evidence": "状态码 200，响应体包含目录外标记文件内容片段：“=== OUTSIDE-ATTACHMENTS MARKER === / MARKER_TOKEN=P66-LOCAL-MARKER-A7F3C9 / 说明：本…”",
  "verdict": "风险"
}
```

修复版（5001）关键条目：

```json
{
  "instance": "http://127.0.0.1:5001",
  "variant": "fixed",
  "param_class": "traversal",
  "parameter": "../outside_attachments/secret_marker.txt",
  "request_url": "http://127.0.0.1:5001/api/attachment/preview?name=..%2Foutside_attachments%2Fsecret_marker.txt",
  "status_code": 403,
  "response_snippet": "{\"error\":\"forbidden\",\"message\":\"\\u9884\\u89c8\\u8def\\u5f84\\u5fc5\\u987b\\u4f4d\\u4e8e\\u9644\\u4ef6\\u76ee\\u5f55\\u5185\",\"variant\":\"fixed\"}",
  "marker_leaked": false,
  "evidence": "状态码 403，响应体未包含标记文件内容 MARKER_TOKEN；片段：“{\"error\":\"forbidden\",\"message\":\"\\u9884\\u89c8\\u8def\\u5f84\\u5fc5\\u987b\\u4f4d\\u4e8e…”",
  "verdict": "已修复"
}
```

汇总：漏洞版 5 条请求中 4 条越界请求均返回 200 且含标记内容；修复版 4 条越界请求全部 403；
正常附件在两个实例上均为 200，基线可用。

### 5.2 关闭 5001（修复版）后

```
    - http://127.0.0.1:5000 (vulnerable): 风险
    - http://127.0.0.1:5001 (fixed): 未完成（不可达：连接被拒绝（ConnectionRefusedError））
[!] 不可达实例：
    - http://127.0.0.1:5001（fixed）：连接被拒绝（ConnectionRefusedError）
```

报告文件：`reports/report-fixed-down.json`。漏洞版结论仍为 `风险`，不可达的修复版及其 5 条参数记录均为 `未完成`。

### 5.3 仅关闭 5000（漏洞版）后

```
    - http://127.0.0.1:5000 (vulnerable): 未完成（不可达：连接被拒绝（ConnectionRefusedError））
    - http://127.0.0.1:5001 (fixed): 已修复
[!] 不可达实例：
    - http://127.0.0.1:5000（vulnerable）：连接被拒绝（ConnectionRefusedError）
```

报告文件：`reports/report-vuln-unreachable.json`。不可达项没有被记为“已修复/安全”，而是 `未完成`。

## 6. 日志（detect.log）

`logs/detect.log` 每行一条请求记录，字段为：
`时间 | instance | variant | param_class | name | status | response`。

```
2026-11-14T13:21:13 | instance=http://127.0.0.1:5000 | variant=vulnerable | param_class=traversal | name=../outside_attachments/secret_marker.txt | status=200 | response: === OUTSIDE-ATTACHMENTS MARKER === / MARKER_TOKEN=P66-LOCAL-MARKER-A7F3C9 / ...
2026-11-14T13:21:13 | instance=http://127.0.0.1:5001 | variant=fixed | param_class=traversal | name=../outside_attachments/secret_marker.txt | status=403 | response: {"error":"forbidden","message":"\u9884\u89c8\u8def\u5f84\u5fc5\u987b\u4f4d\u4e8e\u9644\u4ef6\u76ee\u5f55\u5185","variant":"fixed"}
```

## 7. 业务样例（可复现）

```powershell
python samples/curl_examples.py
```

一次实际输出（节选）：

```
=== 漏洞版 http://127.0.0.1:5000 ===
[正常附件名] ...name=%E5%91%98%E5%B7%A5%E6%89%8B%E5%86%8C-2026.txt
  status=200 body=员工手册（2026 版） 内部工单系统 · 附件预览示例文件 ...
[目录穿越] ...name=..%2Foutside_attachments%2Fsecret_marker.txt
  status=200 body==== OUTSIDE-ATTACHMENTS MARKER === MARKER_TOKEN=P66-LOCAL-MARKER-A7F3C9 ...
[绝对路径] ...name=E%3A%5CCompany%5Cdemo%5Cprojects%5Cproject66%5Coutside_attachments%5Csecret_marker.txt
  status=200 body==== OUTSIDE-ATTACHMENTS MARKER === MARKER_TOKEN=P66-LOCAL-MARKER-A7F3C9 ...

=== 修复版 http://127.0.0.1:5001 ===
[正常附件名] status=200 body=员工手册（2026 版） ...
[目录穿越]   status=403 body={"error":"forbidden","message":"\u9884\u89c8\u8def\u5f84\u5fc5\u987b\u4f4d\u4e8e\u9644\u4ef6\u76ee\u5f55\u5185",...}
[绝对路径]   status=403 body={"error":"forbidden","message":"\u9884\u89c8\u8def\u5f84\u5fc5\u987b\u4f4d\u4e8e\u9644\u4ef6\u76ee\u5f55\u5185",...}
```

## 8. 修复建议

报告中的 `repair_recommendation` 字段给出了具体做法，核心是：

1. 不对客户端参数做字符串拼接后直接读文件；
2. 先规范化路径（`os.path.realpath` / `pathlib.Path.resolve`），得到真实绝对路径；
3. 校验规范化后的路径确实位于附件目录内（如 `candidate.relative_to(attachment_root)`），否则返回 403；
4. 拒绝绝对路径、`../` 与反斜杠变体、空字节等输入；
5. 更稳妥的做法是改为附件 ID → 存储路径的映射，客户端不参与路径寻址。

## 9. 边界说明

- 仅在本机回环地址运行与探测，不扫描、不访问其他主机；
- `outside_attachments/secret_marker.txt` 是本项目自建的核验用标记文件，不含真实敏感数据；
- 服务端为 Flask 开发服务器，仅用于本地复现，不可用于生产部署。
