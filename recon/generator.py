"""确定性本地样本生成器。

固定种子一次性决定全部随机性：三源数据、CSV 快照、跨源冲突、
跨页/边界重复、乱序、服务端错误注入规则与预期真值文件。
运行期（采集进程、模拟服务）不再进行任何随机抽样。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import shutil
from typing import Any

# ---- 固定规模（种子 42 基线） ----
REST_UNIQUE = 525          # REST 源去重后记录数
TS_UNIQUE = 320            # 时间戳源去重后记录数
CSV_UNIQUE = 300           # CSV 源去重后记录数
REST_PAGE_SIZE = 200
TS_PAGE_SIZE = 200
TS_WINDOW_MS = 100         # 半开区间窗口宽度
REST_DUP = 10              # 跨页重复条数（任务 6）
TS_DUP = 5                 # 边界重复条数（任务 6）
CUSTOMERS = (
    "Acme Corporation", "Globex LLC", "Initech GmbH", "Umbrella S.A.",
    "Stark Industries", "Wayne Enterprises", "Wonka Co.", "Soylent Ltd",
)
CSV_HEADER = ["serial_no", "amount", "customer", "event_ts", "detail_code"]
TS_BASE = 1_700_000_000_000  # 毫秒基准时间戳


# ---------------------------------------------------------------- utilities

def _rng(seed: int, tag: str) -> random.Random:
    return random.Random(f"recon-{seed}-{tag}")


def canonical_hash(rows: list[dict]) -> str:
    """主表内容哈希：按主键排序后做规范化 JSON 的 SHA256。"""
    ordered = sorted(rows, key=lambda r: r["serial_no"])
    blob = json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def merge_row(existing: dict, incoming: dict, source_order: tuple[str, ...]) -> dict:
    """字段级合并：新值覆盖 NULL；非空冲突保留先生效来源的旧值。

    source 来源追踪字段的差异不视为业务字段冲突，直接合并为来源列表。
    返回 (合并后行, 冲突字段列表[(field, old_val, new_val)])。
    """
    merged = dict(existing)
    conflicts: list[dict] = []
    for key, new_val in incoming.items():
        if key == "source":
            old_src = existing.get("source", "")
            srcs = [s for s in old_src.split("|") if s]
            if new_val not in srcs:
                srcs.append(new_val)
            merged["source"] = "|".join(
                sorted(srcs, key=lambda s: source_order.index(s) if s in source_order else 99)
            )
            continue
        old_val = existing.get(key)
        if old_val is None and new_val is not None:
            merged[key] = new_val
        elif old_val is not None and new_val is not None and old_val != new_val:
            # 非空冲突：保留旧值（固定提交顺序决定），登记逐项冲突
            conflicts.append({"field": key, "old_value": old_val, "new_value": new_val})
    return merged, conflicts


def _make_row(rng: random.Random, prefix: str, idx: int, serial: str | None = None) -> dict:
    return {
        "serial_no": serial or f"{prefix}-{idx:06d}",
        "amount": round(rng.uniform(10.0, 9000.0), 2),
        "customer": rng.choice(CUSTOMERS),
        "event_ts": rng.randint(TS_BASE, TS_BASE + 10_000_000),
        "detail_code": f"DC{rng.randint(1, 99):02d}",
    }


# ---------------------------------------------------------------- data build

def _build_rest(seed: int) -> tuple[list[dict], dict]:
    rng = _rng(seed, "rest")
    rows = [_make_row(rng, "A", i) for i in range(REST_UNIQUE)]
    total_pages = (len(rows) + REST_PAGE_SIZE - 1) // REST_PAGE_SIZE
    # 分页（尚未注入重复时的稳定分页边界）
    pages: list[list[dict]] = [
        [dict(r) for r in rows[p * REST_PAGE_SIZE:(p + 1) * REST_PAGE_SIZE]]
        for p in range(total_pages)
    ]
    # 注入 10 条跨页重复：取第 2、3 页中的记录，复制到其后一页
    duplicate_serials: list[str] = []
    targets = [5, 33, 60, 87, 110, 205, 233, 260, 287, 310]
    for idx in targets:
        victim = rows[idx]
        page_of_victim = idx // REST_PAGE_SIZE
        duplicate_serials.append(victim["serial_no"])
        pages[page_of_victim + 1].append(dict(victim))
    # 页内乱序（每页独立、确定性）
    for p, page in enumerate(pages):
        random.Random(f"recon-{seed}-rest-shuffle-{p}").shuffle(page)
    server_rows = [r for page in pages for r in page]
    meta = {
        "page_size": REST_PAGE_SIZE,
        "total_pages": total_pages,
        "unique_count": REST_UNIQUE,
        "served_count": len(server_rows),
        "duplicate_serials": duplicate_serials,
        "duplicate_count": REST_DUP,
    }
    return server_rows, meta


def _build_ts(seed: int) -> tuple[list[dict], dict]:
    rng = _rng(seed, "ts")
    rows = [_make_row(rng, "B", i) for i in range(TS_UNIQUE)]
    # 按毫秒时间戳构造窗口；同一毫秒内允许多条
    for i, row in enumerate(rows):
        row["event_ts"] = TS_BASE + (i // 3) * 10  # 每 10ms 一批，每批 3 条
    rows.sort(key=lambda r: (r["event_ts"], r["serial_no"]))
    max_ts = rows[-1]["event_ts"]
    # 注入 5 条边界重复：复制若干已有毫秒的记录，服务端在相邻窗口边界重复返回
    boundary_serials: list[str] = []
    dupes: list[dict] = []
    for i in (9, 39, 69, 99, 129):  # event_ts 恰好落在窗口边界附近
        victim = rows[i]
        boundary_serials.append(victim["serial_no"])
        dupes.append(dict(victim))
    served = rows + dupes
    meta = {
        "page_size": TS_PAGE_SIZE,
        "window_ms": TS_WINDOW_MS,
        "unique_count": TS_UNIQUE,
        "served_count": len(served),
        "max_ts": max_ts,
        "next_cursor_expected": max_ts + 1,
        "boundary_duplicate_serials": boundary_serials,
        "duplicate_count": TS_DUP,
    }
    return served, meta


def _build_csv(seed: int, rest_rows: list[dict]) -> tuple[list[dict], dict, str]:
    rng = _rng(seed, "csv")
    rows = [_make_row(rng, "C", i) for i in range(CSV_UNIQUE)]
    # 跨源冲突：取 REST 第一条，同 serial_no 在 CSV 再出现一次，
    # 仅金额不同，其余业务字段完全一致 -> 冲突审计恰为 1 条（任务 3）
    overlap_serial = rest_rows[0]["serial_no"]
    overlap_rest = rest_rows[0]
    rows[0]["serial_no"] = overlap_serial
    rows[0]["customer"] = overlap_rest["customer"]
    rows[0]["event_ts"] = overlap_rest["event_ts"]
    rows[0]["detail_code"] = overlap_rest["detail_code"]
    # 保证金额确实不同（确定性调整）
    if rows[0]["amount"] == overlap_rest["amount"]:
        rows[0]["amount"] = round((overlap_rest["amount"] + 123.45) % 8000.0 + 10.0, 2)
    conflict_amount = rows[0]["amount"]
    lines = [",".join(CSV_HEADER)]
    for r in rows:
        lines.append(",".join([
            r["serial_no"], f"{r['amount']:.2f}", r["customer"],
            str(r["event_ts"]), r["detail_code"],
        ]))
    # 发布契约：每个完整行（含表头）均以换行结尾
    content = "".join(line + "\n" for line in lines)
    meta = {
        "header": CSV_HEADER,
        "unique_count": CSV_UNIQUE,
        "overlap_serial": overlap_serial,
        "overlap_csv_amount": conflict_amount,
        "overlap_rest_amount": rest_rows[0]["amount"],
    }
    return rows, meta, content


def _expected_truth(rest_unique: list[dict], ts_unique: list[dict],
                    csv_unique: list[dict]) -> dict:
    order: tuple[str, ...] = ("rest", "ts", "csv")
    table: dict[str, dict] = {}
    conflict_rows: list[dict] = []
    for source, rows in (("rest", rest_unique), ("ts", ts_unique), ("csv", csv_unique)):
        for r in rows:
            incoming = dict(r)
            incoming["source"] = source
            serial = incoming["serial_no"]
            if serial not in table:
                table[serial] = dict(incoming)
            else:
                merged, conflicts = merge_row(table[serial], incoming, order)
                table[serial] = merged
                for c in conflicts:
                    conflict_rows.append({
                        "serial_no": serial, "field": c["field"],
                        "old_value": c["old_value"], "new_value": c["new_value"],
                        "winner_source": table[serial]["source"].split("|")[0],
                        "loser_source": source,
                    })
    final_rows = sorted(table.values(), key=lambda r: r["serial_no"])
    return {
        "final_rows": final_rows,
        "content_hash": canonical_hash(final_rows),
        "conflict_count": len(conflict_rows),
        "conflicts": conflict_rows,
        "unique_serials": len(final_rows),
    }


def _base_config(workdir: str, port: int) -> dict:
    data_dir = os.path.join(workdir, "data")
    return {
        "rest_base_url": f"http://127.0.0.1:{port}/rest",
        "ts_base_url": f"http://127.0.0.1:{port}/ts",
        "csv_path": os.path.join(data_dir, "snapshot.csv"),
        "page_size": 200,
        "qps": 5,
        "concurrency": 2,
        "ts_min_interval_ms": 60,
        "max_retries": 5,
        "backoff_base_ms": 50,
        "db_path": os.path.join(data_dir, "recon.db"),
        "log_dir": os.path.join(workdir, "logs"),
        "lock_path": os.path.join(data_dir, "recon.lock"),
        "state_path": os.path.join(data_dir, "state.json"),
        "report_dir": os.path.join(workdir, "reports"),
        "control_file": os.path.join(data_dir, "control"),
        "data_dir": data_dir,
        "server_profile_path": os.path.join(data_dir, "server_profile.json"),
    }


def _write_config(workdir: str, name: str, overrides: dict | None = None,
                  port: int = 8765) -> str:
    cfg = _base_config(workdir, port)
    if overrides:
        cfg.update(overrides)
    path = os.path.join(workdir, "configs", name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return path


def generate(workdir: str, seed: int = 42, port: int = 8765,
             clean: bool = True) -> dict[str, Any]:
    """按固定种子生成整套确定性样本与配置，返回产物清单与真值摘要。"""
    workdir = os.path.abspath(workdir)
    if clean and os.path.isdir(workdir):
        for sub in ("data", "configs", "reports", "logs"):
            p = os.path.join(workdir, sub)
            if os.path.isdir(p):
                shutil.rmtree(p)
    for sub in ("data", "configs", "reports", "logs"):
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)

    rest_served, rest_meta = _build_rest(seed)
    ts_served, ts_meta = _build_ts(seed)
    rest_unique_map: dict[str, dict] = {}
    for r in rest_served:
        rest_unique_map.setdefault(r["serial_no"], r)
    rest_unique = [rest_unique_map[k] for k in sorted(rest_unique_map)]
    ts_unique_map: dict[str, dict] = {}
    for r in ts_served:
        ts_unique_map.setdefault(r["serial_no"], r)
    ts_unique = [ts_unique_map[k] for k in sorted(ts_unique_map)]
    csv_unique, csv_meta, csv_content = _build_csv(seed, rest_unique)

    data_dir = os.path.join(workdir, "data")
    with open(os.path.join(data_dir, "rest_data.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": rest_meta, "rows": rest_served}, fh, ensure_ascii=False)
    with open(os.path.join(data_dir, "ts_data.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": ts_meta, "rows": ts_served}, fh, ensure_ascii=False)
    csv_path = os.path.join(data_dir, "snapshot.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(csv_content)

    truth = _expected_truth(rest_unique, ts_unique, csv_unique)
    truth_doc = {
        "seed": seed,
        "rest": rest_meta,
        "ts": ts_meta,
        "csv": csv_meta,
        "content_hash": truth["content_hash"],
        "unique_serials": truth["unique_serials"],
        "conflict_count": truth["conflict_count"],
        "conflicts": truth["conflicts"],
        "final_rows": truth["final_rows"],
        "customers": list(CUSTOMERS),
    }
    with open(os.path.join(data_dir, "truth.json"), "w", encoding="utf-8") as fh:
        json.dump(truth_doc, fh, ensure_ascii=False, indent=2)

    # ---- 服务端规则文件（错误/延迟注入全部显式声明，无随机） ----
    rules = {
        "rest_data": os.path.join(data_dir, "rest_data.json"),
        "ts_data": os.path.join(data_dir, "ts_data.json"),
        "profile_path": os.path.join(data_dir, "server_profile.json"),
        "rest_errors": [],
        "ts_errors": [],
        "rest_delay_after_page": None,
        "rest_delay_ms": 0,
        "rest_delay_concurrency1": False,
        "shutdown_token": "shutdown-token",
    }
    rules_path = os.path.join(data_dir, "server_rules.json")
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, ensure_ascii=False, indent=2)

    # ---- 配置族 ----
    configs: dict[str, str] = {}
    configs["base"] = _write_config(workdir, "base.json", port=port)

    # 任务 5：REST 前 2 次 500、第 3 次成功
    configs["retry_500"] = _write_config(workdir, "retry_500.json", port=port)
    # 任务 5：TS 全部 401
    configs["ts_auth"] = _write_config(workdir, "ts_auth.json", port=port)
    # 任务 5：REST 连续 6 次 503 -> 熔断
    configs["circuit"] = _write_config(workdir, "circuit.json", port=port)
    # 任务 4：并发 1 + 单次 300ms 延迟
    configs["concurrency1"] = _write_config(
        workdir, "concurrency1.json",
        {"concurrency": 1}, port=port)
    # 任务 2/7/10：中断恢复（控制文件触发，REST 第 2 页提交后）
    interrupt_overrides = {"arm_stop_after_rest_batches": 2}
    configs["interrupt"] = _write_config(
        workdir, "interrupt.json", interrupt_overrides, port=port)
    # 任务 11 非法配置由测试自行写出

    scenario_rules = {
        "retry_500": {
            "rest_errors": [{"attempt": 1, "status": 500},
                            {"attempt": 2, "status": 500}],
        },
        "ts_auth": {
            "ts_errors": [{"all": True, "status": 401}],
        },
        "circuit": {
            "rest_errors": [{"all": True, "status": 503}],
        },
        "concurrency1": {
            "rest_delay_concurrency1": True, "rest_delay_ms": 300,
        },
        "interrupt": {
            # 第 2 页提交后，第 3 页请求进入服务端延迟窗口（TS 尚未开始）
            "rest_delay_after_page": 3, "rest_delay_ms": 1500,
        },
    }
    scenario_index = {}
    for name, overrides in scenario_rules.items():
        r = copy.deepcopy(rules)
        r.update(overrides)
        rp = os.path.join(data_dir, f"server_rules_{name}.json")
        with open(rp, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=2)
        scenario_index[name] = rp

    manifest = {
        "workdir": workdir,
        "seed": seed,
        "data_dir": data_dir,
        "csv_path": csv_path,
        "rules_path": rules_path,
        "scenario_rules": scenario_index,
        "configs": configs,
        "truth_path": os.path.join(data_dir, "truth.json"),
        "summary": {
            "rest_unique": rest_meta["unique_count"],
            "rest_served": rest_meta["served_count"],
            "rest_pages": rest_meta["total_pages"],
            "ts_unique": ts_meta["unique_count"],
            "ts_served": ts_meta["served_count"],
            "csv_unique": csv_meta["unique_count"],
            "content_hash": truth["content_hash"],
            "conflict_count": truth["conflict_count"],
        },
    }
    with open(os.path.join(data_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest


if __name__ == "__main__":
    import sys
    wd = sys.argv[1] if len(sys.argv) > 1 else "workspace"
    sd = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    pt = int(sys.argv[3]) if len(sys.argv) > 3 else 8765
    m = generate(wd, seed=sd, port=pt)
    print(json.dumps(m["summary"], ensure_ascii=False, indent=2))
