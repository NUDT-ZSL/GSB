# 验证结果记录

环境：Python 3.11 + numpy（见 `pyproject.toml`），纯本地执行，无网络/容器/数据库。

## 单元测试

`python -m unittest discover -s tests` → Ran 30 tests，OK（覆盖卷校验、位图、CoW、
三路径逐位一致、原子切换/中断恢复、清单整体替换、完整性/链异常、审计版本化、CLI）。

## 端到端演示（examples/run_demo.py，输出 demo_workspace/demo_summary.json）

### first_snapshot_and_rollback
- all_paths_bit_identical: true
- audit_status: "PASS"
- live_digest_before_rollback: "4aa7ef2cf80bacf033341145f91ea30df6bff02e81746cedff1f53ea33b5eb30"
- paths: {"bitmap_diff": {"bit_identical": true, "changed_blocks": 2, "changed_ratio": 0.0625, "path": "bitmap_diff", "peak_memory_estimate": 139264, "post_rollback_digest": "ba2250ca996f2182cc3a28fcc2d67a3090741cc90dd2b56265fbe862b02ae61f", "volume_blocks": 32}, "full_compare": {"bit_identical": true, "changed_blocks": 2, "changed_ratio": 0.0625, "path": "full_compare", "peak_memory_estimate": 262400, "post_rollback_digest": "ba2250ca996f2182cc3a28fcc2d67a3090741cc90dd2b56265fbe862b02ae61f", "volume_blocks": 32}, "incremental_chain": {"bit_identical": true, "changed_blocks": 2, "changed_ratio": 0.0625, "path": "incremental_chain", "peak_memory_estimate": 131328, "post_rollback_digest": "ba2250ca996f2182cc3a28fcc2d67a3090741cc90dd2b56265fbe862b02ae61f", "volume_blocks": 32}}

### multi_snapshot_chain
- all_paths_bit_identical: true
- audit_status: "PASS"
- changed_blocks: 1
- path_digests: {"bitmap_diff": "ba2250ca996f2182cc3a28fcc2d67a3090741cc90dd2b56265fbe862b02ae61f", "full_compare": "ba2250ca996f2182cc3a28fcc2d67a3090741cc90dd2b56265fbe862b02ae61f", "incremental_chain": "ba2250ca996f2182cc3a28fcc2d67a3090741cc90dd2b56265fbe862b02ae61f"}
- peak_memory_estimate: 131328
- selected_path: "incremental_chain"
- snapshot_count: 3
- target_snapshot: "snap-a"
- volume_blocks: 32

### high_change_ratio_full_path
- all_paths_bit_identical: true
- changed_blocks: 12
- changed_ratio: 0.375
- peak_memory_estimate: 262400
- selected_path: "full_compare"
- threshold: 0.25
- volume_blocks: 32

### manifest_upgrade_whole_replace
- affected_snapshots: ["snap-0001-6af80de0", "snap-0002-6ec5e2dd", "snap-0003-6f368853"]
- audit_after: "PASS"
- audit_before: "PASS"
- backup_readonly: true
- digest_changed: true
- new_format_version: 2
- old_format_version: 1
- rebuilt_chain: [{"parent_snapshot_id": null, "snapshot_id": "snap-0001-6af80de0"}, {"parent_snapshot_id": "snap-0001-6af80de0", "snapshot_id": "snap-0002-6ec5e2dd"}, {"parent_snapshot_id": "snap-0002-6ec5e2dd", "snapshot_id": "snap-0003-6f368853"}]
- version_changed: true

### interrupted_rollback_recovery
- crash_observed: true
- detached_after_recovery: true
- journal_present_after_crash: true
- live_volume_unpolluted: true
- recovered_digest_matches_snapshot: true
- recovery_aborted: false
- recovery_completed: true

### business_code_paths
- codes_present_in_structured_log: ["E_CHAIN", "E_INTEGRITY", "E_SNAPSHOT", "E_VOLUME"]
- first_corrupt_block: 5
- raised_codes: ["E_VOLUME", "E_SNAPSHOT", "E_INTEGRITY", "E_CHAIN"]
