# 本地确定性图像修复与风格迁移领域编辑器 — 技术说明

## 1. 目标与边界

系统在纯本地运行，仅依赖 Python 3.10+、NumPy、Pillow，不访问网络、不加载
预训练权重、不调用外部服务。所有渲染均为确定性算法：

- 修复：由外向内分层 + 定点数距离平方反比邻域加权填充。
- 风格迁移：固定量化档位的三维查表（LUT）颜色映射。
- 同一工程、参考图与配置在任意机器/任意运行次数下输出逐像素一致。

## 2. 目录结构

```
ieditor/                  核心包
  errors.py               业务码与结构化失败清单
  models.py               统一工程模型（掩码/风格参数/损失清单/编辑日志）
  fingerprint.py          结构指纹与损失清单哈希（SHA-256，规范化 JSON）
  config.py               渲染配置/版本映射表/量化规则加载与校验
  parsers.py              v1 二进制 RLE 与 v2 JSON/base64 解析器、写出器
  converter.py            v1<->v2 双向转换与降级损失记录
  inpaint.py              定点分层修复填充
  style.py                参考图统计 + 调色板 + LUT 风格迁移
  editor.py               掩码矩形/权重/强度编辑（追加式编辑日志）
  pipeline.py             修复+风格渲染管线与确定性媒体路径解析
  diagnostics.py          跨版本一致性诊断报告
  snapshot.py             原子快照、进度记录与陈旧快照拒绝
  replay.py               回放引擎、五阶段异常隔离、可处理退出
  cli.py                  交互式命令行与非交互回放入口
scripts/
  generate_samples.py     固定种子样本与异常注入工程生成
  run_demo.py             一键演示并产出 verification.json
config/
  render_config.json      邻域半径/定点 scale/量化档位/告警阈值
  version_map.json        v1 强度档位 -> 分通道权重、调色板桶数
  quant_rules.json        v2->v1 折叠档位、量化小数位、位宽规则
  replay_sequence.json    可重复回放的编辑序列（${WORK_DIR} 占位符）
samples/                  固定种子 PNG、v1/v2 工程、corrupt/ 异常样本
tests/                    unittest 测试（20 用例）
run_demo.bat / run_demo.sh  Windows / POSIX 演示入口
```

## 3. 交换格式

### v1（二进制，magic `IED1`，小端）

`4s magic | H version=1 | I width | I height | i intensity |
H image_path_len | image_path bytes | H ref_len | ref_path bytes |
I run_count | RLE payload...`

- 掩码为 1 位位图，行程压缩：每行程 2 字节 `(value∈{0,1}, length 1..255)`，
  行程总数必须恰好等于 `width*height`，否则 `E_FORMAT_INVALID`。
- 风格参数只有一个整型强度档位 `intensity`。

### v2（文本 JSON，magic `IED2`）

- 掩码 `mask_base64`：逐像素 8 位 0/255 字节数组的 base64；解码长度必须等于
  像素总数，取值只能是 0/255，否则 `E_FORMAT_INVALID`。
- `style.channel_weights`：R/G/B 浮点权重对象，缺任一通道即
  `E_FORMAT_INVALID`；带命名调色板 `palette`。
- 未知 magic/版本号（如 `IED3`）返回 `E_VERSION_UNSUPPORTED`。

两类工程都解析为同一个 `UnifiedProject`（统一掩码位图、`StyleParams`、
来源版本元数据、损失清单、编辑日志）。

## 4. 双向转换与降级损失

- v1 -> v2（升级）：按 `version_map.json` 把强度档位展开为 R/G/B 权重；
  调色板由参考图确定性等桶排序均值生成并命名。升级不产生信息损失。
- v2 -> v1（降级）：按 `quant_rules.json` 以最近 L2 折叠为单一强度档位，
  并写入四类降级损失：
  - `intensity_fold`：折叠档位、L2 距离、逐通道偏差、折叠前原始权重；
  - `float_precision`：定点量化小数位与逐通道舍入误差；
  - `palette_dropped`：被丢弃的命名调色板条目快照；
  - `mask_bit_width`：8 位 0/255 -> 1 位 RLE 的位宽差异与覆盖像素统计。
- 转换产物重新由原版本解析器读入，掩码位图逐像素一致（测试强制校验）。

## 5. 确定性算法

### 修复填充 `inpaint.py`

- 分层：每层 = 与已知像素 4-连通的缺损像素，按行主序固定排序。
- 同层像素只引用此前各层的已知像素，层内互不引用，保证与遍历顺序无关。
- 邻域窗口按行主序扫描，边界外像素确定性跳过；权重
  `w = scale // (dy²+dx²)`（距离平方反比，scale=2^16 定点）。
- 通道累加后做半向上整数除法；零权重时回退到最近已知像素
  （距离再按坐标字典序决胜）。
- 输出每层像素数、来源像素数、权重和与坐标，以及总填充像素数。

### 风格迁移 `style.py`

- 参考图统计量：逐通道四舍五入整数均值。
- LUT 大小固定 `L³`（默认 L=16 => 4096 项），输入颜色按
  `floor(c*L/256)` 量化为索引。
- 目标色 = 参考均值与调色板颜色按通道权重定点混合，再与原色按固定
  `style_blend_num/den` 定点混合；调色板平局以索引/名称确定性决胜。
- 输出构建耗时、表规模、命中桶计数、调色板命中统计；无任何随机采样。

## 6. 诊断、快照与异常隔离

- 诊断报告字段：`business_code`、`max_channel_diff`、`max_abs_diff`、
  `differing_pixel_count`、`mask_coverage`（含 xor 像素）、
  `style_weight_deviation`（逐通道偏差/阈值/折叠前权重）、
  `version_compat_warning`、`loss_log`、双工程 `fingerprints`。
- 折算偏差超过 `deviation_threshold` 仅置告警，不中断渲染。
- 快照经临时文件 + `os.replace` 原子替换，含全局版本号、工程结构指纹、
  损失清单哈希、编辑日志摘要；恢复时指纹/损失哈希不符即
  `E_SNAPSHOT_STALE`，绝不静默读取旧快照。
- 五阶段 `parse/convert/inpaint/style/recovery` 分别捕获异常，记录阶段名、
  源工程路径、坐标或参数键、异常类型到失败清单后继续其余工程。
- SIGINT/SIGTERM 仅置停止标志：当前工程快照原子替换与进度记录
  （最后完成操作序号 + 快照版本号）完成后以退出码 0 结束，
  `--resume` 从该操作之后继续。

## 7. 回放一致性与样本保护

- 交互 CLI 与批量回放共用同一组处理函数与 `config/replay_sequence.json`。
- 路径中的 `${WORK_DIR}` 由引擎展开为各自工作目录，两份干净工作区回放
  得到相同的渲染 SHA-256 与逐字节相同的诊断报告。
- `samples/` 视为只读：对 canonical 样本的首次编辑自动在工作目录生成
  `*_working` 副本，后续写操作只落在工作副本，样本工程永不被污染。

## 8. 业务码

| 业务码 | 触发场景 |
| --- | --- |
| `E_OK` | 成功 |
| `E_FORMAT_INVALID` | 非法行程、base64 长度不符/非法掩码值、缺通道权重、配置非法、路径缺失等 |
| `E_VERSION_UNSUPPORTED` | 未知 magic/版本号（如 IED3） |
| `E_SNAPSHOT_STALE` | 快照指纹或损失哈希与当前工程不符 |
| `E_RENDER_FAILED` | 修复/风格阶段非预期异常（携带 stage 上下文） |
| `E_CONVERSION_FAILED` / `E_RECOVERY_FAILED` | 转换/恢复阶段保留码 |

## 9. 本地验证结果

执行命令：

```bash
python scripts/generate_samples.py
python -m unittest discover -s tests -v
python scripts/run_demo.py
```

实测结果（Python 3.11.0rc1 / NumPy 2.4.6 / Pillow 13.0.0）：

- 20 个 unittest 全部通过，覆盖双版本解析、四类异常码、双向转换掩码一致性、
  四类降级损失、修复/风格逐像素确定性、诊断报告、快照命中与
  `E_SNAPSHOT_STALE`、双工作区回放一致性、断点续跑、SIGINT 优雅退出。
- 4 个异常注入样本分别命中：非法行程与 base64 长度不符 ->
  `E_FORMAT_INVALID`，未知版本 -> `E_VERSION_UNSUPPORTED`，
  缺 B 通道权重 -> `E_FORMAT_INVALID`。
- `workspace_demo/verification.json` 记录双工作区渲染指纹完全相同、
  诊断报告逐字节相同，且兼容告警路径（偏差 0.15 > 阈值 0.12）已验证。

Windows 终端直接运行 `run_demo.bat`（或 `python -m ieditor.cli --help`），
全程无需网络。
