# 依赖供应链完整性诊断工作台

这是一个从零实现、可离线重复执行的 Windows/Python 依赖供应链诊断工具。输入是一批本地 JSON 清单；输出为规范 JSON 诊断报告、依赖传播图数据和原子检查点。核心计算只使用 Python 标准库，`numpy` 与 `pytest` 是环境预检/测试依赖。

## 快速开始

要求 Python 3.10+。在 Windows 空白工作区中：

```powershell
py -3 -m venv .venv
py -3 -m pip install --no-index --find-links wheels numpy pytest
.\.venv\Scripts\python.exe -m supply_diag doctor --json

.\.venv\Scripts\python.exe -m supply_diag generate --output samples
.\.venv\Scripts\python.exe -m supply_diag diagnose samples\manifests `
  --report artifacts\diagnostic_report.json `
  --graph artifacts\dependency_graph.json `
  --checkpoint artifacts\checkpoint.json
```

Linux/macOS 将 `.\.venv\Scripts\python.exe` 替换为 `.venv/bin/python` 即可。也可以先执行 `python -m pip install -e .` 后直接使用 `supply-diag` 命令。

退出码：

- `0`：本批全部清单成功。
- `1`：部分或全部清单存在拒绝条目、环、哈希/签名/约束问题。
- `2`：命令行参数、输入发现或输出写入错误。

单份清单失败不会中断其他清单；结构性 JSON 错误也会形成 `failed` 清单结果。

## 输入清单

推荐结构：

```json
{
  "project_id": "alpha-suite",
  "manifest_version": "1.0.0",
  "components": [
    {
      "component_id": "app",
      "version": "3.0.0",
      "hash_algorithm": "sha256",
      "content_hash": "64位十六进制摘要",
      "signature_status": "signed",
      "content": {"artifact": "app.dll"},
      "dependencies": [
        {"target": "core", "constraint": "[1.2.0,2.0.0)"}
      ]
    }
  ]
}
```

兼容别名：`project`/`name`、`artifacts`/`entries`、`id`、`hash_algo`/`algorithm`、`digest`/`hash`、`signature`、`version_expression`/`version_range`/`range`。签名状态接受 `signed`、`unsigned`、`unknown` 及“已签/已签名、未签/未签名、未知”。

版本表达式支持：

- 精确版本：`1.2.3`、`=1.2.3`
- 比较器组合：`>=1.2.3 <2.0.0`、`>1.0.0, <=2.0.0`
- 闭开区间：`[1.2.3,2.0.0)`、`(1.2.3,2.0.0]`
- 连字符区间：`1.2.3 - 2.0.0`（双闭）
- 对象：`{"lower":"1.2.3","upper":"2.0.0","lower_inclusive":true,"upper_inclusive":false}`
- 全区间：省略约束、`null`、`*`

`content` 存在时会计算规范化摘要。字符串按 UTF-8 原样处理；其他 JSON 值按 `sort_keys=True`、无空白、UTF-8 的规范 JSON 序列化。`sha256` 直接计算 SHA-256；`hmac-sha256` 使用 `hmac_key`，也支持 `hmac_key_hex`。未提供 `content` 时只做摘要格式/位宽校验。

## 精度与误差模型

1. 版本被规范化为非负整数分量向量，例如 `1.2.3 -> (1,2,3)`。
2. 同一清单内取最大分量深度 `D` 作为统一分辨率。较短版本右侧补零。
3. 将向量映射为整数时使用 `base = max(10, 最大分量 + 1)`，保证数值比较仍等价于字典序比较。
4. 一个量子 `q` 是深度 `D` 下最小分量步长，即 1。
5. 每个有限端点：

   ```text
   量化误差 = 1/2 q
   包含误差 = 0 q（闭端点）或 1 q（开端点）
   端点总误差 = 量化误差 + 包含误差
   ```

6. 区间交集选择最紧的下界与上界。同值端点并列时，任一端点为开则交集端点必开；并列误差取最大值。交集宽度误差为下界误差与上界误差之和。
7. 判定规则：

   - 原始交集非空：`satisfiable`
   - 原始交集为空，但向外扩张误差界后重叠：`uncertain`（原因 `ERROR_BOUND_OVERLAP`）
   - 原始与扩张交集均空：`unsatisfiable`
   - `[v,v)` 等显式形式空区间直接判为不可满足，不以误差重叠推翻闭开标记。
8. 有向依赖边为 `source -> target`。累计版本误差从根沿有向边传播；存在多条根路径时选择累计宽度最大者，并列时选择最短且字典序最小的路径。
9. 每个哈希链路的舍入上界为 `2^-256`，累计值为路径节点数乘以 `2^-256`。报告中同时给出分子/分母与十进制表示；十进制可终止时标记 `decimal_exact: true`。

所有误差运算使用 `fractions.Fraction`，不经过二进制浮点。报告中的 `version_resolution` 给出深度、基数和量子定义，便于人工复核。

## 图、完整性与传播

- 自引用和任意长度有向环均输出 `E_CYCLE`。环从字典序最小节点开始，并在末尾重复起点，例如 `["a","b","c","a"]`。
- 同一构件被多条入边约束时，将所有入边约束与构件自身版本表达式一起求交。
- 依赖图边方向是 `source -> target`；签名缺失的传播方向相反：未签/未知依赖会沿反向边影响下游消费者。这与供应链中“依赖缺陷传播给使用方”的方向一致。
- 每个受影响构件列出传播来源、来源状态与规范最短路径，例如 `["leaf","mid","root"]`。
- 构件满足以下全部条件才是 `complete`：版本可满足、哈希有效或仅格式校验、自身已签、没有任何上游缺失签名传播、不处于环中且不存在未解析依赖。
- 版本不确定但无其他确定失败时为 `indeterminate`；哈希不匹配、版本不可满足或签名缺失链为 `incomplete`。

## 同步标记真值对照

生成器在每个构件写入：

```json
"truth": {
  "raw_version_components": [3, 0, 0],
  "declared_algorithm": "sha256",
  "expected_error_bound": {
    "lower": "1",
    "upper": "1",
    "unit": "quantum",
    "scope": "cumulative_version_width"
  },
  "sync_marker": "SHA-256(supply-diag-truth-v1\\n + 规范 JSON)"
}
```

验证端只重算标记，并比较规范化版本分量、规范化算法名和规范化累计误差区间。它不使用生成器的诊断状态或中间图结果，不把生成器计算结论直接当作业真值。报告给出 `checked_components`、`consistent_count`、`inconsistent_count`、`mismatch_count`，不一致项定位到 `source`、`component_id` 和 `field`。

## 检查点与顺序无关

检查点文件是可解析 JSON：

```text
payload:
  schema_version
  hash_algorithm_whitelist_version
  complete
  sequence
  watermark                <- 已应用清单的最大版本水位
  aggregate                <- 聚合计数
  entries[]                <- 成功清单的规范结果
checksum                   <- SHA-256(规范 JSON(payload))
```

- 只有 `complete=true`、校验和正确、结构版本和白名单版本均匹配的检查点才会增量复用。
- 任意损坏、缺字段、校验和错误、结构版本变化或白名单变化都触发整体重建，并在报告 `checkpoint.mode/reason` 中说明。
- 已形成规范结果的 `success`/`partial` 清单会写入检查点；无法形成规范结果的 `failed` 清单不写入。同一 `project_id + manifest_version` 的旧结果会被当前结果替换。
- 清单先按稳定身份排序，构件按 `component_id` 排序，边、拒绝记录、环、传播路径和不确定项均做规范排序。
- 同一组清单的任意文件导入顺序产生同一 `run_id`、同一构件判定、同一误差界和同一签名传播集合。依赖声明顺序不影响稳定边号。

## 输出

诊断报告主要字段：

- `schema_version`、`run_id`、`conclusion`、`exit_code`
- `hash_algorithm_whitelist`
- `checkpoint`
- `summary`
- `uncertain_items`（单列）
- `manifests[]`：清单级结论、拒绝记录、环、构件结果、真值对照

每个构件结果包括规范化版本、哈希/签名状态、所有入边约束、交集状态、闭开端点、量化/包含/宽度误差、累计路径误差、哈希舍入上界、传播路径、完整性状态和业务错误码。

传播图数据主要字段：

- `graphs[].nodes[]`：版本、约束判定、误差界、哈希、签名、完整性
- `graphs[].edges[]`：源、目标、规范约束、目标约束状态
- `graphs[].cycles[]`、`roots[]`、`traversal`

报告和图均先写入目标目录临时文件，`flush + fsync` 后用 `os.replace` 整体替换；检查点同样原子写入。

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `E_INVALID_JSON` | 清单不是合法 UTF-8 JSON |
| `E_SCHEMA` | 顶层或条目结构错误 |
| `E_PROJECT_ID` | 项目标识非法 |
| `E_DUPLICATE_PROJECT` | 批内项目标识重复 |
| `E_MANIFEST_VERSION` | 清单版本格式非法 |
| `E_COMPONENT_ID` | 构件标识非法 |
| `E_DUPLICATE_COMPONENT` | 同一清单内构件标识重复；所有同 ID 条目均拒绝 |
| `E_VERSION_EXPR` | 版本/区间表达式非法 |
| `E_INVALID_CONSTRAINT` | 依赖约束条目非法 |
| `E_HASH_ALGO` | 算法不在白名单、摘要位宽错误或 HMAC 密钥缺失 |
| `E_HASH_MISMATCH` | 规范化内容摘要不匹配 |
| `E_SIGNATURE_STATUS` | 签名状态不在三值集合 |
| `E_UNKNOWN_DEPENDENCY` | 依赖目标不存在或被拒绝 |
| `E_CYCLE` | 自引用或任意长度环 |
| `E_CONSTRAINT_UNSAT` | 版本约束不可满足 |
| `E_TRUTH_MISMATCH` | 同步标记/规范化区间对照不一致 |
| `E_IO` | 清单读取失败 |

警告：`W_BOUND_UNCERTAIN`、`W_SIGNATURE_MISSING`。

## 项目结构

```text
supply_diag/
  cli.py          命令行、批处理、退出码、重复项目检查
  manifest.py     清单解析、条目拒绝、字段规范化
  versioning.py   版本向量、表达式、交集、精确误差
  graph.py        图构建、稳定环枚举、路径
  diagnostics.py  哈希/签名/约束判定、误差传播
  truth.py        同步标记真值对照
  report.py       规范 JSON、原子替换、报告/图导出
  checkpoint.py   校验和检查点、水位、失效重建
  generator.py    确定性离线样本生成器
tests/            43 项 pytest 测试
samples/          生成器输出（三份异常场景 + 一份全成功清单）
artifacts/        实测报告、传播图、检查点
```

## 实际验证

验证环境：Linux/WSL2 上的 Python 3.12.13，`pytest 9.2.0`；`numpy 2.5.5` 安装在 `.venv` 中。核心代码也用 `/usr/bin/python3.10` 做了语法编译检查，未使用 3.11+ 专属运行时 API。

执行：

```text
python -m pytest
43 passed

python -m supply_diag generate --output samples
python -m supply_diag diagnose samples/manifests \
  --report artifacts/diagnostic_report.json \
  --graph artifacts/dependency_graph.json \
  --checkpoint artifacts/checkpoint.json
```

生成样本覆盖：多级依赖、闭开端点混用、多入边交集、误差重叠不确定、不可满足区间、SHA-256/HMAC-SHA256、哈希不匹配、未签/未知签名传播、自环、三节点环、重复条目、非法算法、非法签名、未知依赖、完全成功清单。实测真值对照无差异，检查点第二次运行复用成功清单；损坏检查点会以 `full_rebuild` 重建。

最近一次全量重建结果：4 份清单、20 个保留构件；8 个完整、11 个不完整、1 个不确定；18 个版本可满足、1 个不可满足、1 个误差重叠不确定；检出 2 个环、4 个拒绝条目。同步标记对照 `20/20` 一致、`0` 不一致。第二次带检查点运行复用全部 4 份已诊断清单（水位 `3.2.1`）；反向文件参数运行得到相同 `run_id` 和逐清单结果。

