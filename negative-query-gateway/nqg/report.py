"""Human-readable report rendering and machine-readable artifact writers."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path


def _line(title: str) -> str:
    return f"\n=== {title} ==="


def render_text(report: dict, full_trace: bool = False) -> str:
    out = io.StringIO()
    write = out.write
    residual_preview = 15

    write("校园对象存储 · 负查询加速内核（计数布隆 + 代际重建）演示报告\n")
    write(f"工程: {report['project']}  生成器种子: {report['generator_seed']}\n")
    spec = report["canonical_spec"]
    write(
        "统一口径: 位数组={bit_array_size} 位, 哈希函数={hash_count} 个, "
        "每代上限={max_inserts_per_generation} 次插入, 种子={seeds}\n".format(**spec)
    )

    write(_line("1. 事件规模（确定性生成）"))
    write("\n" + json.dumps(report["event_counts"], ensure_ascii=False) + "\n")

    write(_line("2. 逐代过滤器统计（占用率 = 置位数 / 位数组规模）"))
    for row in report["generations"]:
        write(
            "\n第 {generation_id} 代: 激活 tick={activated_at} 停用 tick={deactivated_at} "
            "迁移键={migrated_keys}\n"
            "  已插入键数={inserted_count} 置位计数={set_bit_count}/{bit_array_size} "
            "占用率={occupancy_ratio:.4f}\n"
            "  查询={total_queries} 拦截={intercepted} 放行={released} "
            "负查询={negative_queries} 实测误判={false_positives}\n"
            "  拦截率={intercept_rate:.4f} 实测误判率={measured_false_positive_rate:.4f} "
            "放行错误率={release_error_rate:.4f}\n".format(**row)
        )

    write(_line("3. 换代重建与原子切换"))
    if not report["rotations"]:
        write("\n（样本未触发换代）\n")
    for rotation in report["rotations"]:
        write(
            "\n触发 tick={triggered_at}: 第 {old_generation_id} 代 -> 第 {new_generation_id} 代, "
            "迁移存活键 {migrated_keys} 个, 旧代停用 tick={old_generation_deactivated_at}\n".format(
                **rotation
            )
        )

    write(_line("4. 删除与残留清理（计数布隆逐位计数 + 代际墓碑）"))
    for delete in report["deletes"]:
        write(
            "\ntick={logical_time} 第{generation_id}代 删除 {key}\n"
            "  位位置={positions} 删除后逐位计数={counters_after}\n".format(**delete)
        )
    residual = report["residual_scan"]
    protected = residual["protected_deleted_keys"]
    candidates = residual["residual_negative_candidates"]
    write(f"\n当前第 {residual['current_generation_id']} 代残留扫描:\n")
    write(f"  受墓碑保护、删除后必然判负的键: {len(protected)} 个\n")
    for item in protected[:residual_preview]:
        write(
            f"    - {item['key']} 位={item['positions']} 计数={item['residual_counters']} "
            f"墓碑保护={item['protected_by_tombstone']}\n"
        )
    write(f"  仍可能被误判为存在的非删除负键: {len(candidates)} 个\n")
    for item in candidates[:residual_preview]:
        write(
            f"    - {item['key']} 位={item['positions']} 残留计数={item['residual_counters']}\n"
        )
    if len(candidates) > residual_preview or len(protected) > residual_preview:
        write(
            f"  （终端仅预览前 {residual_preview} 条，完整逐位计数清单见 reports/report.json 的 residual_scan）\n"
        )

    write(_line("5. 总量与两课题组前缀加速效果"))
    totals = report["totals"]
    write(
        "\n查询总数={total_queries} 拦截={intercepted} 放行={released} "
        "负查询={negative_queries} 实测误判={false_positives}\n"
        "整体拦截率={intercept_rate:.4f} 整体实测误判率={measured_false_positive_rate:.4f} "
        "后端实际查询={backend_lookups} 节省后端查询={backend_lookups_saved}\n".format(
            **totals
        )
    )
    for row in report["lab_acceleration"]:
        write(
            "\n[{lab_prefix}] 查询={total_queries} 负查询={negative_queries} "
            "拦截={intercepted} 放行={released} 误判={false_positives}\n"
            "  负查询拦截率={negative_intercept_rate:.4f} "
            "实测误判率={measured_false_positive_rate:.4f} "
            "节省后端查询={backend_lookups_saved}\n".format(**row)
        )

    write(_line("6. 三版本归一与跨版本差异清单"))
    cross = report["cross_version"]
    write(f"\n逐查询裁决与每代统计完全一致: {cross['identical']} （比对 {cross['compared_query_count']} 条查询）\n")
    for row in cross["diff_table"]:
        write(
            "\n版本 {version}:\n"
            "  键字段={key_field}({key_encoding})\n"
            "  操作字段={op_field} 时刻字段={time_field}\n"
            "  规模字段={size_field} 哈希数字段={hash_count_field} 容量字段={capacity_field}\n"
            "  种子字段={seed_field}: {seed_expression} -> 归一 {normalized_seeds}\n".format(
                **row
            )
        )

    write(_line("7. 独立位数组参考实现核对"))
    audit = report["reference_audit"]
    write(
        f"\n参考实现={audit['implementation']}\n"
        f"复用主实现位计算路径={audit['shares_kernel_bit_path']} "
        f"核对代数={audit['audited_generations']} 不一致项={audit['mismatch']}\n"
    )

    write(_line("8. 后端分片索引"))
    write("\n" + json.dumps(report["backend"], ensure_ascii=False) + "\n")

    write(_line("9. 逐查询裁决（可逐条复算）"))
    rows = report["per_query"]
    shown = rows if full_trace else rows[:15]
    for row in shown:
        write(
            "\nseq={query_seq} tick={logical_time} gen={generation_id} key={key}\n"
            "  位={positions} 过滤器={filter_verdict} 裁决={decision} "
            "后端存在={backend_exists} 误判={false_positive} 墓碑={via_tombstone}\n".format(
                **row
            )
        )
    if not full_trace:
        write(f"\n... 仅展示前 15 条，共 {len(rows)} 条；使用 --full-trace 查看全部，或运行 trace 子命令。\n")
    return out.getvalue()


def write_artifacts(report: dict, directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    csv_path = directory / "per_query_verdicts.csv"
    gen_path = directory / "generation_stats.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        if report["per_query"]:
            writer = csv.DictWriter(handle, fieldnames=list(report["per_query"][0].keys()))
            writer.writeheader()
            writer.writerows(report["per_query"])

    with gen_path.open("w", newline="", encoding="utf-8") as handle:
        if report["generations"]:
            writer = csv.DictWriter(handle, fieldnames=list(report["generations"][0].keys()))
            writer.writeheader()
            writer.writerows(report["generations"])
    return {"json": json_path, "csv": csv_path, "generation_csv": gen_path}
