# 实际验收记录

- 总体结果：通过
- Python：3.11.18；numpy：1.26.4；Pillow：10.3.0
- 整数偏移最大误差：0 px
- 亚像素最大误差：0.184265 px；容差：0.25 px
- 掩码冲突：12 条；墓碑：1 条
- 亮度/对比度校正前后重叠区 MAE：12.3149 -> 5.1957
- 串行行序参考与加权融合逐像素一致：True
- workers=1 与 workers=3 PNG 逐像素一致：True
- 中断退出码：130；中断时清单：tile_00.manifest.json
- E_DIGEST 篡改恢复：True
- 非法输入业务码：E_MANIFEST，且未生成拼接图
- pytest 退出码：0

## 检查项

- `sample_generated`：PASS
- `registration_integer_error_zero`：PASS
- `registration_subpixel_within_tolerance`：PASS
- `conflict_arbitration_count_12`：PASS
- `all_conflicts_located`：PASS
- `serial_row_reference_matches`：PASS
- `worker_counts_pixel_identical`：PASS
- `interrupt_exit_130`：PASS
- `interrupt_exactly_one_manifest`：PASS
- `digest_tamper_recovered`：PASS
- `invalid_input_returns_E_code`：PASS
- `invalid_input_has_no_mosaic`：PASS
- `version_archive_created`：PASS
- `pytest_returncode_zero`：PASS
