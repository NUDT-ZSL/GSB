# 过场动画音画同步核对工具（project87）

音频组的**音频提示点表**与**引擎事件表**在导入前需要核对音画同步。本工具读取两份 JSON
标记表与一份配置 JSON，按名称比对毫秒时间，输出：

- `corrections.json` —— 可执行的**校正清单**（偏移超容差的标记）
- `sync_report.txt` —— 人类可读的**同步报告**（校正清单 / 未匹配 / 不可核对 / 顺序异常）
- `result.json` —— 完整结构化结果（供自动化流水线消费）

仅依赖 Python 3.10+ 标准库，**无第三方依赖**，不使用容器。

## 目录结构

```
project87/
├── audio_sync/
│   ├── sync_check.py          # 核对工具主程序
│   └── generate_samples.py    # 确定性样本/配置生成器（无随机数）
├── config/
│   └── config.sample.json     # 配置：容差 80ms、总时长 60000ms
├── samples/
│   ├── main/                  # 主样本（每表 5 条，覆盖多数规则）
│   ├── boundary/              # 边界：偏移恰好等于容差、负时间/超总时长越界
│   ├── unmatched/             # 单侧出现标记
│   └── sort_ties/             # 等偏移排序
├── tests/
│   └── test_sync_check.py     # 15 个 unittest 用例（标准库）
└── output/                    # 默认输出目录
```

## 一、本地启动步骤

前置：已安装 Python 3.10 或以上（本机验证版本 3.10.20 / 3.13.14 均可）。

```powershell
# 1. （可选）确定性重建全部样本与配置——输出与仓库内文件逐字节一致
python -I audio_sync/generate_samples.py

# 2. 运行核对（默认读取 samples/main 与 config/config.sample.json，写入 output/）
python -I audio_sync/sync_check.py

# 3. 也可以显式指定任意路径
python -I audio_sync/sync_check.py `
  --audio  samples/main/audio_cues.json `
  --engine samples/main/engine_events.json `
  --config config/config.sample.json `
  --outdir output

# 4. 运行测试
python -I tests/test_sync_check.py
```

说明：`-I` 为隔离模式（忽略当前目录与 `PYTHONPATH`，防止同名 `json.py` 之类被误导入），
是处理来源不明 JSON 时的推荐用法；本工具只用标准库，去掉 `-I` 也能运行。
所有路径均通过命令行参数传入，工具**只读输入文件**，不会修改或删除它们。

## 二、输入/输出约定

### 输入标记表（两份结构相同）

```json
[
  {"name": "BGM_Start", "time": 1200},
  {"name": "Footstep", "time": 2000}
]
```

- `name`：非空字符串；`time`：**整数毫秒**（布尔值、浮点一律判类型错误）。
- 顶层必须是数组；单条记录缺字段或类型错误时跳过该条并在结果中说明，其余照常处理。

### 配置文件

```json
{ "tolerance_ms": 80, "total_duration_ms": 60000 }
```

- `tolerance_ms`：容差阈值，缺省 80 毫秒；偏移绝对值**严格超过**它才进入校正清单。
- `total_duration_ms`：总时长，越界判定区间为 `[0, total_duration_ms]`；
  不配置则只检查负时间。配置缺失时自动回退缺省值并在报告中注明。

### 输出校正清单 `corrections.json`

按偏移绝对值降序、同值按名称升序排列，每项含名称、音频时间、引擎时间、偏移量：

```json
[
  { "名称": "Camera_Shake", "音频时间": 4000, "引擎时间": 3800, "偏移量": -200 },
  { "名称": "BGM_Start",    "音频时间": 1200, "引擎时间": 1350, "偏移量": 150 }
]
```

偏移量 = 引擎时间 − 音频时间；正值表示引擎偏晚，负值表示引擎偏早。

## 三、业务规则与实现对照

| # | 规则 | 实现位置（`sync_check.py`） |
|---|------|------------------------------|
| 1 | 时间统一按整数毫秒解释 | `_is_plain_int` 排除 bool/float |
| 2 | 文件内时间须单调不减，颠倒单独指出 | `process_file` 顺序异常段 → 报告第四节 |
| 3 | 同名保留首个，其余列不可核对 | 去重段，原因“名称重复…” |
| 4 | 按名称合并，偏移=引擎−音频 | `merge_and_compare` |
| 5 | 偏移绝对值超容差进校正清单，缺省 80 | `abs(offset) > tolerance` |
| 6 | 单侧出现不参与计算，未匹配注明来源 | 集合差集，来源“音频/引擎” |
| 7 | `time<0` 或 `>总时长` 越界，不影响他人 | 越界段先于去重执行 |
| 8 | 缺字段/类型错误跳过并说明 | 逐条结构校验段 |
| 9 | 校正清单按 |偏移| 降序、名称升序 | `corrections.sort((-abs, 名称))` |
| 10 | 报告含三（实为四）个分类，空分类写“无” | `build_report` |
| 11 | 两份表都不可读仍出报告、退出码 0 | `process_file` 读取异常分支 + `main` 恒返回 0 |
| 12 | 重复运行逐字节一致、不改输入 | 无时间戳、UTF-8/LF 固定序列化；测试校验哈希 |

判定流水线（单文件内）：**结构校验（规则8）→ 顺序检查（规则2，仅提示）→
越界检查（规则7）→ 同名去重（规则3）→ 参与名称合并（规则4~6）**。
即一条记录若既越界又与他者同名，会先按越界列入不可核对。

## 四、业务样例讲解（samples/main）

音频表 5 条：`BGM_Start@1200`、`Camera_Shake@4000`、`Footstep@2000`、
`Door_Open@3000`、`Door_Open@2800`；
引擎表 5 条：`BGM_Start@1350`、`Footstep@2050`、`Light_Flash@4100`、
`Camera_Shake@3800`、以及 1 条缺少 `name` 的记录。

- **校正清单**：`Camera_Shake` 偏移 −200（引擎偏早 200ms）、`BGM_Start` 偏移 +150
  （引擎偏晚 150ms），均超 80ms 容差，按绝对值降序输出；`Footstep` 偏移 +50 在容差内，不校正。
- **未匹配**：`Door_Open` 只在音频表、`Light_Flash` 只在引擎表，分别注明来源。
- **不可核对**：音频第二条 `Door_Open` 名称重复（首个保留）；引擎第 5 条缺 `name`。
- **顺序异常**：音频 `Footstep(2000)<Camera_Shake(4000)`、重复的 `Door_Open(2800)<3000`、
  引擎 `Camera_Shake(3800)<Light_Flash(4100)`，仅提示，不剥夺比对资格。

## 五、确定性与安全说明

- 样本由 `generate_samples.py` 纯字面量生成，无随机数、无时间戳，可随时逐字节重建。
- 输出 JSON/文本统一 UTF-8、LF 行尾、固定键顺序与缩进，重复运行结果逐字节相同（有测试守护）。
- 工具不写任何输入路径；仅在 `--outdir`（自动创建）下产出 3 个文件。
- 测试只针对本地样本与临时目录，不访问网络、不使用容器。
