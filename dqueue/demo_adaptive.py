"""Demonstrate heap -> wheel adaptive switching on a large deterministic set."""

import json
import os
import shutil

from dqueue.config import QueueConfig
from dqueue.manifest import payload_digest
from dqueue.queue import DelayedQueue

ROOT = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(ROOT, "run", "adaptive")


def main():
    os.makedirs(RUN, exist_ok=True)
    data_dir = os.path.join(RUN, "data")
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir)
    db = os.path.join(data_dir, "bulk.db")
    cfg = QueueConfig.from_dict(json.load(open(os.path.join(
        ROOT, "samples", "queue_config.json"))))
    q = DelayedQueue(db, cfg)
    count, span = 128, 20000
    tasks = []
    for i in range(count):
        payload = {"seq": i}
        tasks.append({
            "task_id": f"bulk-{i:04d}", "topic": "bulk.stream",
            "fire_ms": 1000 + i * span // count,
            "policy_label": "fixed", "max_retries": 1,
            "payload": payload,
            "payload_digest": payload_digest(payload)})
    policies = {"fixed": {"label": "fixed", "base_ms": 100,
                          "multiplier": 2, "cap_ms": 1000,
                          "jitter_pct": 0}}
    q.enqueue_manifest(tasks, policies, 0)

    # Observation scan before anything is due: rule chooses the mode.
    decision = q.scan(500)
    # Drain the whole horizon in coarse steps; every task triggers exactly once.
    steps = [1000, 3000, 7000, 12000, 17000, 22000]
    triggered = []
    for step in steps:
        triggered.extend(q.scan(step)["triggered"])
        polled = q.poll("bulk.stream", "bulk-consumer", step)
        for item in polled["tasks"]:
            q.ack(item["pull_id"], "bulk-consumer", True, step + 10)
    report = {
        "task_count": count,
        "decision_scan": {
            "mode": decision["mode"],
            "mode_version": decision["mode_version"],
            "observation": decision["observation"]},
        "switch_log": q.status()["switch_log"],
        "triggered_total": len(triggered),
        "triggered_unique": len(set(triggered)),
        "no_loss_no_duplicate":
            len(triggered) == len(set(triggered)) == count,
    }
    out = os.path.join(RUN, "adaptive-report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    print("report:", out)
    q.close()


if __name__ == "__main__":
    main()
