"""确定性归档工程生成器。

固定种子 + 固定参数 + 固定迭代顺序下，生成的工程 JSON 字节级可重放。
"""

import random

from .levels import BASE_TIME_MS, CADENCE_MS, LEVEL_POLICY, MERGE_GAP_MS, SITE_LEVEL_CYCLE

PRIMARY = "primary"
BACKUP = "backup"
ENDS = (PRIMARY, BACKUP)


def generate_project(seed, n_sites=8, batches_per_element=24, base_time_ms=BASE_TIME_MS):
    rng = random.Random(seed)
    sites = _build_sites(n_sites)
    events = _build_events(base_time_ms, batches_per_element)
    outage = _outage_window(events)

    manifest = []
    landed = {PRIMARY: [], BACKUP: []}
    for site in sites:
        for element in site["elements"]:
            for seq in range(1, batches_per_element + 1):
                ref_time = base_time_ms + (seq - 1) * CADENCE_MS
                manifest.append(
                    {
                        "site_id": site["site_id"],
                        "element": element,
                        "seq": seq,
                        "write_time": ref_time,
                        "level": site["level"],
                    }
                )
                for end in ENDS:
                    if _is_missing(rng, end, ref_time, outage):
                        continue
                    jitter = rng.randint(-3_000, 5_000)
                    landed[end].append(
                        {
                            "site_id": site["site_id"],
                            "element": element,
                            "seq": seq,
                            "write_time": ref_time + jitter,
                            "level": site["level"],
                        }
                    )

    project = {
        "schema_version": "1.0",
        "generator": {
            "seed": seed,
            "n_sites": n_sites,
            "batches_per_element": batches_per_element,
            "cadence_ms": CADENCE_MS,
            "merge_gap_ms": MERGE_GAP_MS,
            "base_time_ms": base_time_ms,
        },
        "level_policy": LEVEL_POLICY,
        "sites": sites,
        "events": events,
        "manifest": manifest,
        "ends": {
            end: {
                "end": end,
                "landed": landed[end],
                "last_commit": _last_commit(sites, landed[end]),
            }
            for end in ENDS
        },
    }
    return project


def _build_sites(n_sites):
    sites = []
    for idx in range(n_sites):
        level = SITE_LEVEL_CYCLE[idx % len(SITE_LEVEL_CYCLE)]
        sites.append(
            {
                "site_id": f"ST{idx + 1:03d}",
                "level": level,
                "elements": list(LEVEL_POLICY[level]["elements"]),
            }
        )
    return sites


def _build_events(base_time_ms, batches_per_element):
    last_ref = base_time_ms + (batches_per_element - 1) * CADENCE_MS
    interrupt_start = base_time_ms + 8 * CADENCE_MS
    interrupt_end = min(base_time_ms + 12 * CADENCE_MS, last_ref)
    recovery_at = min(base_time_ms + 18 * CADENCE_MS, last_ref)
    return [
        {
            "event_id": "EVT-1",
            "type": "interrupt",
            "side": PRIMARY,
            "at": interrupt_start,
            "until": interrupt_end,
        },
        {
            "event_id": "EVT-2",
            "type": "switchover",
            "at": interrupt_end,
            "active_end_after": BACKUP,
        },
        {
            "event_id": "EVT-3",
            "type": "recovery",
            "side": PRIMARY,
            "at": recovery_at,
            "active_end_after": PRIMARY,
        },
    ]


def _outage_window(events):
    for event in events:
        if event["type"] == "interrupt":
            return event["at"], event["until"]
    return None, None


def _is_missing(rng, end, ref_time, outage):
    outage_start, outage_end = outage
    in_outage = outage_start is not None and outage_start <= ref_time < outage_end
    if in_outage:
        if end == PRIMARY:
            return rng.random() < 0.85
        return rng.random() < 0.10
    if end == PRIMARY:
        return rng.random() < 0.10
    return rng.random() < 0.13


def _last_commit(sites, landed_records):
    per_element = {}
    site_commit = {}
    for record in landed_records:
        site_id = record["site_id"]
        element = record["element"]
        key = (site_id, element)
        current = per_element.get(key)
        if current is None or record["seq"] > current["last_seq"]:
            per_element[key] = {
                "last_seq": record["seq"],
                "commit_time": record["write_time"],
            }
        site_state = site_commit.setdefault(
            site_id, {"last_seq": 0, "commit_time": 0, "elements": {}}
        )
        if record["seq"] > site_state["last_seq"]:
            site_state["last_seq"] = record["seq"]
        if record["write_time"] > site_state["commit_time"]:
            site_state["commit_time"] = record["write_time"]

    for site in sites:
        site_id = site["site_id"]
        element_map = site_commit.setdefault(
            site_id, {"last_seq": 0, "commit_time": 0, "elements": {}}
        )
        for element in site["elements"]:
            element_map["elements"][element] = per_element.get(
                (site_id, element), {"last_seq": 0, "commit_time": 0}
            )
    return site_commit

