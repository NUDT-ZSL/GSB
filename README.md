# macroanalyzer — 离线 Office 宏载荷静态分析工具

在隔离 Windows 环境中运行的离线 OOXML / OLE 文档宏载荷静态分析与漏洞复现验证工具。
仅依赖 Python 标准库与 `olefile==0.47`；所有样本由内置生成器按字节模板确定性构造，
不依赖外部恶意样本或网络访问。

## 环境

- Python 3.11+（本仓库在 Python 3.14.7 上验证）
- `pip install olefile==0.47`

## 使用方法

```powershell
# 生成确定性样本（OOXML 含/无宏、OLE 含宏、损坏文件、截断模块、
# 超时样本、超限样本、重复载荷、合法/损坏规则集、旧版报告）
python -m macroanalyzer gensamples --out samples

# 分析（单文档或批量，语义一致）
python -m macroanalyzer analyze <docs...> --out report.json `
    [--rules rules.json] [--jobs 4] [--max-bytes 16777216] [--timeout-ms 5000]

# 合并旧版本报告（缺失字段以默认值填充并写入 warnings）
python -m macroanalyzer merge legacy_report.json --out merged.json

# 完整性校验（重算 input_sha256 与规则集指纹并比对）
python -m macroanalyzer verify --report report.json [--rules rules.json]

# 运行验收测试（覆盖任务验收项 1-10，共 55 项断言）
python tests\run_acceptance.py
```

## 退出码约定

| 退出码 | 含义 |
|---|---|
| 0 | 全部成功（模块级 decode_error 不影响） |
| 2 | 部分失败（存在 parse_error / timeout / size_exceeded 文档），或 verify 发现不一致 |
| 3 | 规则集加载失败（已回退内置规则集，报告仍正常生成） |
| 4 | 参数错误（stderr 输出说明，不写出报告文件） |

## 架构

```
macroanalyzer/
  cli.py          命令行、退出码约定（argparse 错误 -> 4）
  analyzer.py     格式识别（调度前完成）、有界并发、子进程超时中断、去重聚合
  deepworker.py   子进程深解析：逐模块流式回传，超时被杀后保留部分结果
  vbaproject.py   vbaProject.bin 解析：dir 流记录、模块流抽取（olefile 读取 CFB）
  vbacompress.py  MS-OVBA 压缩容器解压（生成器用原始块/字面量块压缩）
  normalize.py    续行合并、注释剥离、字符串遮蔽视图（防伪关键字干扰）
  features.py     行为特征提取 + 字符串拼接常量折叠
  rules.py        规则集加载/校验/匹配/置信度归一化，内置默认规则集
  report.py       报告序列化、旧版报告合并、verify 完整性校验
  cfbwrite.py     最小 CFB(OLE2) 写入器（含 MiniFAT），供样本生成器使用
  generator.py    确定性样本/规则集/旧版报告生成器
tests/run_acceptance.py   端到端验收（55 项断言）
```

### 关键设计

- **格式识别先于调度**：主线程先做 zip/CFB 嗅探与宏流定位；无法识别的容器
  → `format=unknown, status=parse_error`；识别但无宏流 → `status=no_macro`（非错误）。
- **真实超时中断**：每个文档的深度解析在独立子进程中运行（multiprocessing spawn），
  超时即 `terminate()`；子进程逐模块回传消息，父进程保留已收到的模块与特征并标记
  `truncated=true`。计时从子进程发出 `started`（开始解析）起算，不计解释器启动开销。
- **资源上限**：工作线程数可配置并硬性钳制到 ≤8；内置并发计数器记录峰值写入报告
  `concurrency.peak`；超过 `--max-bytes` 的文档标记 `size_exceeded` 并跳过深解析，
  不阻塞其他文档；批量报告顺序与输入一致。
- **特征与证据**：关键词匹配在字符串遮蔽视图上进行（字符串内伪关键字不触发）；
  `obfuscation_string_concat` 仅对可常量折叠的拼接（`Chr(65)+Chr(66)`、`"a"&"b"`）
  标记，普通字符串不误报；每个特征携带模块名与语句序号。
- **置信度**：`confidence = 命中规则权重和 / 规则集总权重`，截断到 [0,1]；
  命中列表按严重级别降序、同极按 id 排序。规则文件损坏时报告 `rules_load_error`
  并回退内置规则集（`used_ruleset=builtin`）。
- **去重聚合**：对规范化后模块源码计算 SHA-256 载荷指纹；同组首个文档为基准，
  其余 `duplicate_of` 指向它；`groups`/`aggregate.by_fingerprint` 按指纹字典序排列。
- **可复核性**：每文档记录 `input_sha256`、`analysis_ms`（非负数值）、报告级
  `used_ruleset_fingerprint`；`verify` 子命令重算并比对，不一致列出字段与文档 id。
- **版本兼容**：报告含字符串 `schema_version`；`merge` 读取缺字段的旧报告，
  以默认值填充并将缺失字段名写入 `warnings`，退出码 0。

## 实际验证结果（2026-11-12，Windows 11，Python 3.14.7 + olefile 0.47）

- `python tests\run_acceptance.py`：**55 项检查全部 PASS，0 失败**（覆盖验收项 1-10）。
- 超时真实性：40 个长模块样本在 200ms 预算下仅完成 5 个模块即被终止，
  `status=timeout, truncated=true`，部分模块写入报告，同批其他文档不受影响。
- 确定性：两次 `gensamples` 输出逐字节一致（SHA-256 全等）。
