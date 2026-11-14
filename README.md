# 精灵图集动画接入核对工具（sprite-check）

横版像素游戏接入辅助工具：动画师交付按帧网格切分的 PNG 精灵图集后，
接入同学用本工具在接入前核对——每帧局部更新是否漏改像素、循环动画
首尾拼接是否跳变、是否残留透明边缘。工具输出逐动画 JSON 核对报告与
自左向右的拼接预览图。

## 环境要求

- Python 3.10+，Pillow 10.x+（开发验证环境：Python 3.10.20 / Pillow 12.3.0）
- 仅使用本地文件，无需容器、无需网络、无需外部附件

## 本地启动步骤

```bash
# 1. 确定性生成业务样例（samples/atlas.png 与 samples/animations.json）
python make_samples.py

# 2. 运行核对（输出目录不存在时自动创建）
python sprite_check.py --atlas samples/atlas.png --desc samples/animations.json --out output

# 3. 查看结果
#    output/report.json        逐动画核对报告
#    output/<动画名>_preview.png 拼接预览图

# 4. 运行测试（16 项，覆盖全部核对方式）
python -m pytest tests/ -v
```

命令行参数：`--atlas` 图集路径、`--desc` 动画描述 JSON 路径、`--out` 输出目录。
运行时在控制台打印每个动画的帧数与状态概况。图集缺失/损坏或描述 JSON
非法时工具不会崩溃：前者在报告中列出不可用提示并正常结束，后者返回退出码 1。

## 动画描述 JSON 格式

```json
{
  "animations": [
    {
      "name": "walk",
      "frame_width": 32,
      "frame_height": 32,
      "frames": [0, 1, 2, 3],
      "diff_threshold": 0,
      "loop": true
    }
  ]
}
```

- 帧序号为图集网格的行优先序号（0 起），网格列数 = 图集宽 ÷ 帧宽；
- `diff_threshold`：相邻帧差异像素总数**超过**该值才计为变化；
- `loop` 为 true 时额外检测末帧→首帧衔接。

## 核对规则与报告字段

- 帧网格宽高必须整除图集宽高；帧序号必须在网格内且不重复，违规动画
  标记 `incomplete` 且不生成预览，其余动画照常核对；
- 逐对相邻帧做 RGBA 四通道逐像素比较（仅 alpha 差异也算变化），
  变化像素数超阈值时输出脏矩形，坐标以图集像素为单位、原点在图集
  左上角，以 `lower_left`（左下）/ `upper_right`（右上）角点表示，
  矩形恰好覆盖全部变化像素；
- 差异未超阈值的帧脏矩形为 `null`，拼接预览中保持前一帧内容；
- 循环衔接差异超阈值时在 `loop_jump` 字段单列（含衔接帧对与脏矩形），
  整体状态置为 `jump`；
- 整体状态三值：`pass`（通过）/ `jump`（存在循环跳变）/ `incomplete`（不完整）；
- 报告与预览对相同输入完全确定，重复运行哈希一致。

## 业务样例

`make_samples.py` 以固定种子确定性生成 128×64 图集（32×32 帧网格，
4 列 × 2 行共 8 格），内容为一个像素小人，含半透明地面阴影：

| 动画 | 帧 | 循环 | 预期结果 |
|------|-----------|------|----------|
| walk  | 0,1,2,3 | 是 | 末帧→首帧存在 68 像素步进差异，标记 `jump` 并给出衔接脏矩形 |
| blink | 4,5,6   | 是 | 帧 5 与帧 4 完全相同（脏矩形为空、预览保持前帧）；末帧闭眼，衔接标记 `jump` |
| idle  | 4,7     | 是 | 两帧逐像素一致，无变化、无跳变，`pass` |

## 验证记录

在本机（Windows 11，Python 3.10.20，Pillow 12.3.0）执行：

1. `python -m pytest tests/ -v` —— **16 项全部通过**，逐条覆盖任务核对方式：
   - 网格不整除 / 重复与越界帧序号被报告且不生成预览（方式 1）；
   - 单像素变化样本脏矩形恰为该像素、无冗余扩大（方式 2）；
   - 差异未超阈值时脏矩形为空且预览逐像素等于前一帧（方式 3）；
   - 循环动画首尾超阈值差异单列为 `loop_jump`（方式 4）；
   - alpha 0→128 被识别为变化且脏矩形包含该像素（方式 5）；
   - 预览宽 = 帧宽 × 帧数，各帧像素与图集对应区域一致（方式 6）；
   - 损坏 / 缺失图集：报告含不可用提示，描述仍被读取，工具正常结束（方式 7）；
   - 一个动画越界时正常动画的报告与预览完整生成（方式 8）；
   - 报告字段齐全，三种整体状态可区分（方式 9）；
   - 已知网格偏移样本的脏矩形坐标与手工计算一致（方式 10）；
   - 连续两次运行报告 JSON 与预览图 SHA-256 完全相同（方式 11）；
   - 命令行指定新输出目录自动创建，控制台打印各动画帧数与状态（方式 12）。
2. 样例确定性重建：连续两次执行 `make_samples.py` + 核对流程，
   `samples/atlas.png`、`samples/animations.json`、`output/report.json`
   与全部预览图的 SHA-256 完全一致。
3. 样例运行控制台输出：

   ```
   [jump      ] walk: 4 frame(s), preview=walk_preview.png
   [jump      ] blink: 3 frame(s), preview=blink_preview.png
   [pass      ] idle: 2 frame(s), preview=idle_preview.png
   report written to output\report.json
   ```

## 文件结构

```
sprite_check.py          核心核对逻辑与命令行入口
make_samples.py          确定性样例生成
tests/test_sprite_check.py  16 项本地测试（仅操作临时目录）
samples/                 生成的样例图集与描述
output/                  生成的报告与预览
```
