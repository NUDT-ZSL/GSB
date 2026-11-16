# 技术设计

## 1. 数据流

```text
WAV (16 kHz / mono / PCM_16)
  ├─ v1 JSON ── parser ─┐
  └─ v2 SRT  ── parser ─┴─> unified IR (utterance/token, int ms)
                               ├─ edit operations
                               ├─ v1 writer + v2 writer + sidecar writer
                               ├─ complete atomic snapshot
                               └─ diagnostics
```

统一 IR 不依赖输出格式。`Transcript`、`Utterance`、`Token` 的当前时间均为整数毫秒；每个 utterance/token 另存 `Origin`，记录源版本、原始起止、原文、源文件和行号。v2 的独立回退切分使用“最大非空白片段”；若旁路元数据可用则以旁路的词级时间与切分为准。

## 2. 格式与时间

v1 输出示例：

```json
{
  "version": 1,
  "language": "zh-CN",
  "utterances": [
    {
      "id": "utt-001",
      "start": 0.000,
      "end": 1.250,
      "text": "今天 天气 很好",
      "tokens": [{"text": "今天", "start": 0.000, "end": 0.300}]
    }
  ]
}
```

SRT 使用标准 `HH:MM:SS,mmm --> HH:MM:SS,mmm`。v1 秒值经 `Decimal` 转毫秒并使用 half-up 取整，避免二进制浮点累加；输出秒值固定三位小数。跨格式偏差只做整数毫秒相减。

字符权重分配和拆分边界使用 10 ms 网格。拆分严格保留原 token 时间，仅把新 utterance 首 token 起点取整到 10 ms，且不允许越过左侧末 token 终点。词级时间推断按字符数累计权重分配，保留句级首尾端点。

## 3. 失败隔离

解析器逐 utterance/cue/token 捕获异常。失败记录字段为：

```json
{
  "stage": "parse",
  "source_file": "path/to/file",
  "item_index": 1,
  "line_number": 5,
  "code": "E_FORMAT_INVALID",
  "exception_type": "ReversedTimestampError",
  "message": "token 0 end precedes start"
}
```

UTF-8 以 `surrogateescape` 读入，再逐个用户可见字符串检查代理字符；单个坏 token/cue 不会丢弃同文件的合法邻项。JSON 语法错误或整文件不可读会记录文件级错误。

## 4. 编辑语义

- 合并：只允许相邻 utterance；token 顺序和时间保持，新句界取首 token 起点与末 token 终点。
- 拆分：边界取右组首 token 起点并归一到 10 ms；原 token 时间不拉伸，原句间静音可以保留。
- 平移：选择与 `[start_ms, end_ms)` 相交的 token，统一加整数 `delta_ms`；任一结果小于 0 则整个操作失败并产生 `E_TIME_OVERFLOW`，不发生部分修改。
- 文本替换：替换指定 token 文本；token 时间不变，v1/v2 同步重写。

每次成功编辑后同时重写 v1、v2、旁路元数据。v2 句级边界由当前首/末 token 决定。

## 5. 旁路元数据与哈希

`<stem>.tokens.meta.json` 包含：

- `v2_content_hash`：当前 SRT 句级时间与规范化文本的内容哈希。
- `token_metadata_hash`：词文本、起止毫秒和 `inferred` 标记的哈希。
- 每个 utterance 的 id、句级边界、文本与 token 时间。

升级时只有 `v2_content_hash` 与当前 SRT 完全一致，且旁路 token 文本可重构当前句文本，才恢复词级时间。文件损坏、哈希不匹配或文本不一致都会回退到字符权重推断，标记所有 token 为 `inferred=true`，并产生 `E_TOKEN_TIME_INFERRED`。v2 原始句级时间仍保存在 `Origin`，不会静默丢失。

## 6. 快照与恢复

每次成功编辑：

1. 构造包含完整 IR、旁路元数据、全局版本、IR 指纹、旁路哈希、操作日志摘要和精度统计的快照。
2. 依次原子写 `snapshots/snapshot-NNNNNN.json` 和 `snapshots/snapshot.json`。
3. 原子写 v1、v2、旁路文件和 `workspace.json`。
4. 原子写 `progress.json`，记录最后完成操作序号和快照版本。

启动时选择版本最高的可校验完整快照。以下任一情况触发整体回滚：工作区版本高于快照、状态缺失或版本不一致、当前 v1 语义指纹不匹配、当前 v2 句级内容不匹配、旁路文件缺失/损坏/哈希不匹配。回滚直接由快照重建全部输出，不依赖进程内存。

`resume_after_operation` 来自校验过的进度记录；只有计划摘要一致才续跑，否则产生 `E_PLAN_MISMATCH`。这也覆盖“快照已写、latest 尚未替换”的强杀窗口：加载器会选中更高版本的完整快照。

## 7. 信号与强杀

CLI 把 SIGINT、SIGTERM、Windows SIGBREAK 转成安全退出标志。信号处理器不中断正在执行的纯整数编辑；操作完成并原子写快照/进度后，命令以 `0` 退出。随后同计划重启会从 `last_completed_operation + 1` 继续。

无法处理的强制终止不会运行清理代码，也不会承诺生成退出报告。重启只采用完整替换过的快照；未持久化的内存状态、临时文件或半写输出不会被信任。

## 8. 诊断与精度观测

诊断报告对逐句输出 `start_deviation_ms`、`end_deviation_ms`、token 数差、文本一致性、推断标记数和业务码。任一偏差超过 20 ms、token 数不同或文本不同即标注 `E_CROSS_FORMAT_MISMATCH`。

`precision` 段输出：

- `internal_unit = integer_millisecond`
- 解析和 10 ms 网格各自的舍入次数与最大误差
- 输出小数位 `3`
- 最大误差是否不超过 5 ms 半网格，用于验证 10 ms 粒度约束

## 9. 平台注意事项

项目在 Windows 上使用 `.venv\Scripts\python.exe`。所有路径通过 `pathlib.Path` 组合，文本固定 UTF-8，JSON/旁路输出使用临时文件加 `os.replace`。信号测试在 POSIX 上实际验证 SIGINT/SIGKILL；Windows 逻辑使用标准库 `signal` 的 SIGINT/SIGTERM/SIGBREAK 分支和同一原子写路径。
