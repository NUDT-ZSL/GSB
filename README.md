# 帧序列 PNG → 透明对齐精灵表工具

把混有尺寸不一、模式不一（RGB/RGBA/L/LA/P）、缺失透明通道和个别损坏文件的
帧序列 PNG，按文件名中的数字序号处理为**透明、居中对齐的 RGBA 水平精灵表**，
并输出逐帧处理状态与拼接位置的核对报告。全程离线、确定性、不使用容器。

## 环境要求

- Python 3.10+
- Pillow（`pip install -r requirements.txt`，本机已验证 Pillow 12.3.0 / Python 3.10.20）

## 本地启动步骤

```powershell
# 1. （可选）安装依赖
python -m pip install -r requirements.txt

# 2. 确定性重建样例帧序列（固定常量生成，无随机、无网络）
python generate_samples.py --out samples --force

# 3. 打包：默认把 sprite_sheet.png 与 report.json 输出到输入目录
python pack_sprites.py samples/normal
# 或指定输出目录
python pack_sprites.py samples/with_corrupt --output-dir build/with_corrupt

# 4. 运行全部核对测试（9 条需求逐一覆盖，含 CLI 子进程端到端）
python -I -m unittest discover -s tests -v
```

退出码：`0` 成功（可能含被跳过的无效帧）；`1` 无任何有效帧（仍写报告、无精灵表）；
`2` 序号校验失败（重复/缺号/文件名无唯一数字/目录不存在），不产出任何文件。

## 处理规则与业务样例

| 规则 | 行为 |
|---|---|
| 序号解析 | 文件名 stem 中须恰有一段数字；按数值升序而非字典序（`frame_2` 排在 `frame_10` 前） |
| 重复/缺号 | 报错并终止（退出码 2），且清掉目标目录里可能残留的旧产物 |
| 模式归一 | 统一转 RGBA；无 Alpha 的帧补全为不透明（α=255），带透明度的帧原样保留 |
| 画布对齐 | 单元格取所有有效帧最大宽高，每帧居中粘贴，四周填完全透明像素 (0,0,0,0) |
| 损坏帧 | `verify()`+全量 `load()` 双重校验；无法识别/截断的 PNG 跳过并在报告记录文件名与原因 |
| 拼接 | 有效帧按序号从左到右水平拼接，表宽 = 单帧宽 × 有效帧数，表高 = 单帧高，RGBA |
| 重复执行 | 覆盖旧精灵表与旧报告；输入目录内残留的旧 `sprite_sheet.png` 不会被当作输入帧 |

确定性样例（`generate_samples.py` 生成）：

- `samples/normal/`：6 帧，尺寸 40×32 ~ 60×48，模式依次 RGB/RGBA/L/RGBA/P/LA，
  其中 RGBA/LA 帧含半透明条带，用于核对 Alpha 保留。
- `samples/with_corrupt/`：同上 + 1 个内容为非 PNG 字节的 `frame_07.png`。
- `samples/all_corrupt/`：3 个全部损坏的帧。
- `samples/duplicate_index/`、`samples/missing_index/`：序号重复 / 缺号场景。

## report.json 字段

```json
{
  "input_frame_count": 7,          // 输入 PNG 总数
  "valid_frame_count": 6,          // 有效帧数
  "invalid_frames": [              // 无效帧名单（文件名 + 原因）
    {"file": "frame_07.png", "reason": "UnidentifiedImageError: ..."}
  ],
  "frame_width": 60,               // 单元格（最大）宽高
  "frame_height": 48,
  "sprite_sheet": {"file": "sprite_sheet.png", "width": 360, "height": 48, "mode": "RGBA"},
  "frames": [                      // 每个有效帧的状态与拼接位置
    {
      "index": 1, "file": "frame_01.png",
      "x": 0,                      // 在精灵表中的起始 X 像素坐标
      "width": 40, "height": 32,   // 原始帧尺寸
      "offset_x": 10, "offset_y": 8, // 在单元格内的居中偏移
      "original_mode": "RGB"
    }
  ]
}
```

无有效帧时 `sprite_sheet` 为 `null`、`frames` 为空、`valid_frame_count` 为 0，
报告仍正常输出，进程以退出码 1 结束。

## 验证记录

测试文件 `tests/test_sprite_packer.py` 与 9 条需求一一对应：

1. 重复序号、缺号、文件名含多段数字、空目录 → 抛 `SequenceError`/退出码 2，无任何产物（含旧产物清理）
2. RGB→RGBA 全不透明且颜色不变；RGBA/LA 透明值逐像素保持；P/L 模式正确归一
3. 三帧不同尺寸 → 画布取最大宽高、逐帧居中、四角透明像素校验
4. 垃圾字节 + 截断 PNG 均被跳过，精灵表照常生成，报告记录文件名与原因
5. 精灵表模式为 RGBA，宽 = 单帧宽 × 有效帧数，高 = 单帧高
6. 报告字段齐全，无效帧名单一致，尺寸与 X 坐标合法
7. 全损坏目录 → 退出码 1、无精灵表、报告有效帧数 0 且逐条列出无效帧
8. 按报告 X 坐标从实际精灵表裁剪，与独立重建的期望单元格逐字节一致；末帧右边界 = 表宽
9. 预置旧产物连续运行两次，输出字节级一致，旧文件被覆盖，无关文件不受影响

另含 CLI 端到端测试：以 `python -I` 子进程跑生成器与 CLI，验证样例可字节级
确定性重建，并覆盖正常/损坏/全损坏/重复/缺号五种场景的退出码与产物。
所有测试仅使用本地临时目录，不访问网络。
