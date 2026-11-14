# 实际检查结果（machine-checked）

- 生成时间：2026-11-14T19:55:18+00:00
- 平台：Windows-10-10.0.26100-SP0
- Python：3.10.20
- Pillow：12.3.0
- 命令：`python tools/verify_requirements.py --out build/verification`
- 结果：**14/14 项通过**

| # | 检查项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | 按描述顺序导出，与文件名排序无关 | PASS | 描述顺序前 3: ['frames/shot_k.png', 'frames/b_reader.png', 'frames/zeta_09.png']；文件名字典序前 3: ['frames/aaa_start.png', 'frames/b_reader.png', 'frames/frame-0007.png']；报告顺序一致=True |
| 2 | 小尺寸帧居中留白，留白为真实背景像素 | PASS | 160x90 → 320x180，四角像素 (16, 24, 32, 255)，背景 (16, 24, 32, 255)，内容框 [80, 45, 240, 135] |
| 3 | 无透明通道帧按不透明处理并在报告标注 | PASS | 源模式=RGB，导出 alpha 范围=(255, 255)，标注=['源图缺少透明通道，按整体不透明处理（alpha=255）。'] |
| 4 | 超画幅帧等比缩放且完整可见 | PASS | 480x270 → 320x180，缩放 0.666667，内容框 [0, 0, 320, 180] |
| 5 | 停留毫秒参与时间线，累计时长与描述一致 | PASS | 描述累计 3750 ms，报告 3750 ms，预览含 18 个时长 |
| 6 | 停留毫秒为负/-非数值时该帧无效，其余继续 | PASS | 序号 5 状态=invalid，原因=停留毫秒 duration_ms 不能为负数：-1。；序号 13 状态=invalid |
| 7 | 损坏帧不中断整批，其余帧正常产出 | PASS | 序号 9 状态=corrupt，帧文件解码失败：OSError: Truncated File Read；其余导出 14 帧 |
| 8 | 缺失帧与成功帧区分，成功帧像素不受污染 | PASS | 序号 6 状态=missing_file；12/12 帧合成 PNG 与全量基线逐字节一致 |
| 9 | 重复/缺失序号明确列出，缺失帧不导出为空白帧 | PASS | 缺失=[3]，重复=[4]，报告条目数=18（与描述条目一致，无补白） |
| 10 | 报告 JSON 字段齐全，序号顺序与描述一致 | PASS | 18 条逐帧记录，必需字段全部存在，顺序一致=True |
| 11 | 预览页面按顺序展示全部成功帧与停留时长 | PASS | 卡片 18 张，PNG 18 张全部存在，顺序=[1, 2, 3, 4, 5]… |
| 12 | 帧率/画幅非法时给出可读失败说明 | PASS | 退出码 3，报告错误=帧率(fps)必须为大于 0 的数值，当前为 0。 |
| 13 | 整批结论三分类：complete / partial / unexportable | PASS | 全量=complete，含坏帧=partial，描述不可用=unexportable |
| 14 | 连续运行两次输出完全一致 | PASS | report=True，preview=True，18 帧 PNG=True，退出码 0/0 |

## 产物

- 全量样本导出包：`build/verification/clean/preview.html`
- 故障样本导出包：`build/verification/issues/preview.html`
- 非法帧率失败包：`build/verification/invalid_fps/preview.html`

## 单元/验收测试

```
python -m unittest discover -s tests -v
```

覆盖 14 条验收要求及边界情况（路径越界、重复序号文件冲突、别名键、缺省背景色）。
