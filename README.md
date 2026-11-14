# LFI 授权批量体检工具（纯 Python 标准库）

对**授权范围内、仅在本机运行**的内部自建 Web 应用做本地文件包含（LFI）批量
体检。输入应用根目录与 URL 清单，输出 JSON 报告，将每个 URL 区分为：

| 结论 | 含义 |
|---|---|
| 疑似LFI | 某一文件包含请求的响应中命中敏感文件特征串 |
| 确认安全 | 服务可正常响应，但全部响应均未命中特征串 |
| 未完成 | 连接失败 / 响应超时 / 重试后仍不可用 |

> 合规边界：仅可用于已获授权的安全测试。内置样本服务只读取仓库内
> `samples/sensitive/` 自制夹具，不触碰真实系统文件；全程仅访问
> `127.0.0.1`，不联网、不使用容器、不访问任何未授权目标。

## 1. 环境要求

- Windows 10/11，Python **3.10 或更高**（开发验证环境为 Python 3.14.7）
- 仅使用标准库（`urllib`、`http.server`、`json`、`socket` 等），无需 pip 安装
- 建议始终以 `python -I`（隔离模式）运行，避免当前目录恶意模块被导入

## 2. 文件说明

```
project54/
├── lfi_scanner.py          # 主检测工具（CLI，生成 JSON 报告）
├── sample_server.py        # 确定性本地样本服务：vulnerable / safe / slow
├── run_demo.py             # 覆盖全部 11 条验收点的自动化演示
├── run_demo.bat            # Windows cmd 一键演示
├── samples/sensitive/      # 模拟敏感文件夹具（win.ini、passwd，自制无害内容）
├── url_lists/demo_urls.txt # 手动演示用 URL 清单
└── demo_work/              # 演示自动生成的清单与 JSON 报告（运行后出现）
```

## 3. 业务操作样例（手动演示，可复现）

打开 **命令提示符（cmd）**，切换到本目录，分四个窗口/步骤操作：

```bat
REM 步骤1：启动三个固定端口的确定性样本服务（各开一个 cmd 窗口）
python -I sample_server.py vulnerable --port 8011
python -I sample_server.py safe       --port 8012
python -I sample_server.py slow       --port 8013 --delay 3
```

- `8011/vuln`：模拟含 LFI 缺陷的页面（参数值被拼进文件读取路径）
- `8012/safe`：模拟安全页面（忽略参数，固定返回无关 HTML）
- `8013/slow`：每个请求睡眠 3 秒，用于制造超时
- `8099`：**故意不监听**，用于制造服务不可用

```bat
REM 步骤2：执行批量体检（清单见 url_lists\demo_urls.txt）
python -I lfi_scanner.py . url_lists\demo_urls.txt --output report.json --timeout 1
```

参数说明：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `app_root`（位置参数1） | 被测应用根目录，必须是已存在的本地目录，否则非零退出 | 必填 |
| `url_list`（位置参数2） | URL 清单，UTF-8 文本，每行一个 URL，`#` 为注释 | 必填 |
| `-o/--output` | JSON 报告输出路径 | `lfi_report.json` |
| `-t/--timeout` | 单请求超时秒数 | `5` |
| `-r/--retries` | 传输失败后的重试次数（总尝试 = 重试+1） | `2` |
| `-d/--retry-delay` | 重试间隔秒数 | `0.5` |

输入校验示例（均以退出码 2 失败并打印中文原因）：

```bat
python -I lfi_scanner.py C:\no_such_dir url_lists\demo_urls.txt
REM -> 错误: 应用根目录不存在或不是目录: ...

REM 清单中放入 javascript:alert(1) 等非法 URL
python -I lfi_scanner.py . bad_urls.txt
REM -> 错误: 清单第 2 行 URL 非法 —— 仅支持 http/https 协议: ...
```

## 4. 一键自动化验收（推荐演示方式）

无需手动开服务，脚本会自动启停所有本地服务并制造故障场景：

```bat
run_demo.bat
REM 或
python -I run_demo.py
```

约 15 秒完成，逐条打印 `[通过]`，全部通过时退出码为 0。

## 5. JSON 报告结构

```json
{
  "报告生成时间": "2026-11-14T10:00:00",
  "应用根目录": "D:\\...\\project54",
  "检测参数": { "超时秒": 1.0, "重试次数": 1, "包含参数": ["file", "..."],
                "敏感文件特征串": ["[fonts]", "root:x:0:0:", "..."] },
  "汇总": {
    "总数": 4, "确认安全数": 1, "疑似LFI数": 1, "未完成数": 2,
    "疑似LFI_URL清单": ["http://127.0.0.1:8011/vuln"],
    "确认安全_URL清单": ["http://127.0.0.1:8012/safe"],
    "未完成_URL清单": ["http://127.0.0.1:8013/slow", "http://127.0.0.1:8099/vuln"]
  },
  "逐条结果": [
    {
      "url": "http://127.0.0.1:8011/vuln",
      "结论": "疑似LFI",
      "检测时间": "2026-11-14T10:00:00",
      "证据": "参数 file、path ... 命中敏感文件特征串 [fonts]、root:x:0:0: ...；证据片段: ...",
      "错误说明": "",
      "命中特征串": ["[fonts]", "for 16-bit app support", "root:x:0:0:", "/bin/bash"],
      "尝试次数": 1,
      "request_evidence": [
        { "param": "file", "payload": "../../../../windows/win.ini",
          "probe_url": "http://...?file=...", "http_status": 200,
          "matched_signatures": ["[fonts]"],
          "response_snippet": "; for 16-bit app support [fonts] ..." }
      ]
    }
  ]
}
```

要点：

- 每条结果必含 `url / 结论 / 检测时间 / 证据 / 错误说明`，**证据字段保证非空**；
- 未完成条目在 `错误说明` 中给出可读原因（如 `请求超时(超过 1.0 秒): timed out`、
  `连接失败，服务不可用: ...`），异常不被吞掉；
- `汇总.未完成_URL清单` 单独列出始终未完成的 URL，供业务人员跟进；
- 汇总四个计数与逐条结论严格一致，列表长度等于清单 URL 数。

## 6. 判定原理与内置检测字典

- 包含参数（10 个）：`file, filename, path, page, include, doc, document,
  template, view, load`
- 遍历/敏感文件载荷（4 个）：Windows `win.ini` 的正斜杠/反斜杠两种遍历写法、
  `/etc/passwd` 绝对路径与遍历路径
- 特征串（命中才允许判疑似）：`[fonts]`、`for 16-bit app support`、
  `root:x:0:0:`、`/bin/bash`
- **无特征串内容绝不判疑似**：安全页面返回无关 HTML，结论为确认安全。
- 单 URL 失败只影响自身：异常在该条目内捕获，其余 URL 照常检测，
  已成功结果完整保留。

## 7. 验收点对照（实测结果）

`run_demo.py` 全部断言实测通过（Windows 11 + Python 3.14.7，退出码 0）：

| 验收点 | 演示场景 | 实测结论 |
|---|---|---|
| 1 参数校验 | S0：不存在目录、`ftp://`/非法 URL | 退出码 2，stderr 打印明确中文原因 |
| 2 两类服务判定 | S1：vulnerable / safe | 疑似LFI / 确认安全 |
| 3 条目字段与非空证据 | S1/S2 全条目结构断言 | URL、结论、时间戳、证据齐全且非空 |
| 4 不可用不混入其他类 | S2：8099 无监听 | 未完成，且不在安全/疑似清单 |
| 5 恢复后重试成功 | S3：2 秒后启动服务，4 次重试 | 尝试 2 次后结论=确认安全 |
| 6 故障隔离 | S4：好/坏 URL 混合清单 | 好 URL 结论与单独运行完全一致 |
| 7 合法 JSON 与汇总一致 | 全部报告 | 计数一致、列表长度=清单 URL 数 |
| 8 特征串依赖判定 | 安全 HTML vs 含特征串响应 | 无特征=安全，有特征=疑似 |
| 9 异常不吞、可读错误 | S2：slow 服务超时 | 错误说明非空且含“超时” |
| 10 未完成清单 | S2 汇总区 | 准确列出 2 个未完成 URL |
| 11 Windows cmd 纯标准库 | `run_demo.bat` | 退出码 0，报告为合法 UTF-8 JSON |

> 实测备注：Windows 本机网络栈对刚释放端口的 `connect` 可能表现为超时
> （类 Unix 上通常是 connection refused）。两种情况都归入“未完成”并在
> `错误说明` 中保留原始原因，不影响分类正确性。

## 8. 对真实应用使用时

1. 确认已取得书面授权，且目标仅监听本机；
2. 准备 URL 清单（可先只放一个页面入口，工具会自动变换参数与载荷）；
3. 按需调整 `--timeout / --retries`，服务偶发卡顿可加大重试次数；
4. 报告中“未完成_URL清单”交由业务方确认服务状态后重新运行；
5. “疑似LFI”条目附带的 `probe_url` 与 `response_snippet` 可直接用于复核。
