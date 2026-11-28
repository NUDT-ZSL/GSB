# 电网计量块存储演示

这是一个只使用 Python 标准库的本地演示项目，实现了：

- 固定种子生成台区、电表、逐条计量记录及写入/重传/删除交错事件流。
- 对 JSONL 记录字节流进行基于齿轮滚动哈希的内容定义分块（CDC）。
- 以 SHA-256 块摘要为内容地址建立 SQLite 块索引和引用计数。
- 文件到块、块到文件的双向逻辑映射。
- 按台区编号前缀与毫秒采集区间检索有效记录。
- 归档删除时逐块递减引用计数、归零回收及回收账目。
- 运行时切换分块策略，并比较不同策略的块命中率与去重率。
- 读取同一 SQLite 文件的 tkinter 诊断视图。

## 环境准备

要求 Linux（WSL Ubuntu）和 Python 3.10 以上版本。项目不依赖第三方 Python 包。

图形视图需要系统包 `python3-tk`：

```bash
sudo apt-get update
sudo apt-get install -y python3-tk
```

WSL 中还需要可用的图形会话（WSLg 或配置好的 X Server）。没有图形库或显示时，命令行功能不受影响。

## 快速开始

在项目根目录执行：

```bash
python3 -m metering_store demo --seed 42
python3 -m metering_store verify
python3 -m metering_store gui
```

`demo` 默认会重建数据库并完成整条事件流，生成以下本地文件：

| 路径 | 内容 |
| --- | --- |
| `data/chunk_index.sqlite3` | 块索引、逻辑映射、事件与回收账目 |
| `data/samples/manifest.json` | 台区配置、文件摘要、交错事件清单 |
| `data/samples/files/*.jsonl` | 确定性记录流样本文件 |
| `data/samples/records.jsonl` | 基础记录流拼接样本 |
| `data/reports/demo_report.json` | 全事件明细、块边界、摘要、引用变更与指标 |
| `data/reports/reclaim_ledger.json` | 删除事件的逐块回收清单 |

再次以相同参数运行会得到逐字节相同的样本文件。命令行入口和 tkinter 视图使用同一个数据库。

## 命令行

```bash
# 仅生成确定性样本
python3 -m metering_store generate --seed 42 --output data/samples

# 完整生成并回放事件流
python3 -m metering_store demo \
  --seed 42 --target-size 384 --mask-bits 7 \
  --new-target-size 768 --new-mask-bits 9

# 独立复算所有有效文件的块边界与摘要，并重放范围检索
python3 -m metering_store verify --db data/chunk_index.sqlite3

# 检查引用计数不变量
python3 -m metering_store check --db data/chunk_index.sqlite3

# 按台区前缀和毫秒区间检索
python3 -m metering_store search --area-prefix A01 \
  --start-ms 1700000000000 --end-ms 1700000020000

# 文件追溯与摘要反查
python3 -m metering_store file --file-id f-0001
python3 -m metering_store refs --digest <sha256>

# 查看策略指标、块索引、事件和回收清单
python3 -m metering_store metrics
python3 -m metering_store blocks
python3 -m metering_store events
python3 -m metering_store ledger

# 打开 tkinter 诊断视图
python3 -m metering_store gui --db data/chunk_index.sqlite3
```

手工写入与删除：

```bash
python3 -m metering_store write \
  --file-id f-manual --path data/samples/files/f-0001.jsonl
python3 -m metering_store delete --file-id f-manual
python3 -m metering_store switch-policy --target-size 1024 --mask-bits 9
```

多数查询命令支持 `--json`，便于脚本比对。

## 确定性数据格式

每条记录包含：

```text
area_id, meter_id, ts_ms,
active_energy_wh, reactive_energy_varh,
voltage_mv, current_ma, quality_flag
```

序列化为 UTF-8 JSONL：键排序、分隔符固定为 `,` 和 `:`、每条记录以单个换行结束。相同种子与相同分块参数下，文件字节完全一致。

默认样本包含 3 个台区（`A01` 至 `A03`）、每台区 4 块电表、1000 ms 采集间隔、每批 4 个采集时刻，共 14 个文件、21 个交错事件。事件涵盖：

- 基础写入
- 完全相同的重复上报
- 只有单个字段不同的近似重复上报
- 分块策略切换
- 交错归档删除

## CDC 分块规则

实现位于 `metering_store/chunker.py`：

1. 使用由固定 SHA-256 材料生成的 256 项齿轮表。
2. 每字节更新 `h = ((h << 1) | (h >> 63)) ^ gear[byte]`（64 位回绕）。
3. 最小块大小为 `max(8, target_size // 4)`，最大块大小为 `max(min+1, target_size * 4)`。
4. 达到最小大小后，若 `h & ((1 << mask_bits) - 1) == 0`，在该字节后闭合块。
5. 每块起始时滚动状态重置；文件尾部强制闭合最后一块。

因此，相同记录前缀在分歧点之前会产生完全相同的边界和摘要。块摘要采用 SHA-256。

## 存储与账目语义

- `blocks.digest` 是主键和内容地址。
- 新块写入时 `ref_count = 1`；已存在的存活块只递增引用计数。
- 每个文件的每个块都有一条 `file_chunks` 逻辑映射，映射记录创建时使用的策略和是否命中。
- 删除文件不会删除审计映射，而是把 `file_chunks.active` 置为 0，并逐块递减引用计数。
- 引用计数归零时，块标记 `reclaimable = 1`、清空块负载并记录回收序号和字节数。
- 关键不变量：任意时刻 `SUM(blocks.ref_count)` 等于有效 `file_chunks` 映射数。
- 检索只连接有效文件及有效映射，因此不会返回已删除文件中的块。
- 策略切换只改变之后新写入文件使用的参数。旧文件、旧映射和旧块索引不会被重分块。

策略指标按文件创建时的策略累计：

- 块命中率 = 命中的块引用数 / 总块引用数
- 去重率 = 去重节省字节 / 总逻辑块字节

## tkinter 诊断视图

界面包含四个页签：

- **文件与块边界**：选择文件后查看每个块的偏移范围、长度、摘要、引用数、命中标记和有效状态。
- **块索引**：查看全部块的长度、引用数、是否可回收、是否留存及创建策略。
- **台区/时间检索**：输入台区前缀和毫秒区间，查看命中记录对应的文件、块偏移和摘要。
- **账目核对**：查看去重前后占用、引用总和、归零块和分块策略指标。

视图每 2 秒自动刷新，也可用“刷新视图”按钮立即读取命令行刚推进的写入或删除事件。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖确定性生成、记录前缀稳定性、独立参考分块器复算、完全重复与近似重复、双向引用、交错删除回收、范围检索、策略切换及完整事件流重放。

无 `python3-tk` 时仍可运行全部命令行与测试；此时 `gui` 会给出安装提示并退出。
