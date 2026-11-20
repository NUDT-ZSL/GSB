"""Generate the fixed, self-consistent deterministic JSON samples.

Run once (and whenever the sample design changes); the generated files are
checked in so demonstrations never depend on randomness.
"""

import json
import os

from dqueue.manifest import payload_digest

HERE = os.path.dirname(os.path.abspath(__file__))

POLICIES = [
    {"label": "fixed", "base_ms": 500, "multiplier": 2,
     "cap_ms": 4000, "jitter_pct": 0},
    {"label": "jittered", "base_ms": 200, "multiplier": 2,
     "cap_ms": 3200, "jitter_pct": 25},
    {"label": "gentle", "base_ms": 100, "multiplier": 3,
     "cap_ms": 2000, "jitter_pct": 10},
]

RAW_TASKS = [
    ("t-001", "orders.created", 1000, "fixed", 2, {"order": 1, "amount": 10}),
    ("t-002", "orders.created", 1000, "jittered", 1, {"order": 2, "amount": 20}),
    ("t-003", "orders.paid", 1500, "gentle", 3, {"order": 3, "amount": 30}),
    ("t-004", "emails.welcome", 1500, "jittered", 2, {"user": "alice"}),
    ("t-005", "emails.welcome", 2200, "fixed", 1, {"user": "bob"}),
    ("t-006", "reports.daily", 2600, "gentle", 2, {"day": "2026-11-21"}),
    ("t-007", "orders.created", 3100, "jittered", 0, {"order": 7}),
    ("t-008", "reports.daily", 3200, "fixed", 3, {"day": "2026-11-22"}),
    ("t-009", "emails.welcome", 4000, "gentle", 1, {"user": "carol"}),
    ("t-010", "orders.paid", 4200, "jittered", 2, {"order": 10}),
    ("t-011", "reports.daily", 5000, "fixed", 1, {"day": "2026-11-23"}),
    ("t-012", "orders.created", 5600, "gentle", 0, {"order": 12}),
]

CLOCK_STEPS = [0, 1000, 1500, 2200, 2600, 3100, 3200, 4000, 4200,
               5000, 5600, 8000, 12000]


def main():
    manifest = []
    for task_id, topic, fire_ms, label, max_retries, payload in RAW_TASKS:
        manifest.append({
            "task_id": task_id,
            "topic": topic,
            "fire_ms": fire_ms,
            "policy_label": label,
            "max_retries": max_retries,
            "payload": payload,
            "payload_digest": payload_digest(payload),
        })
    with open(os.path.join(HERE, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(os.path.join(HERE, "policies.json"), "w",
              encoding="utf-8") as fh:
        json.dump(POLICIES, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(os.path.join(HERE, "clock_steps.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"steps_ms": CLOCK_STEPS}, fh, indent=2)
        fh.write("\n")
    print(f"wrote {len(manifest)} tasks, {len(POLICIES)} policies, "
          f"{len(CLOCK_STEPS)} clock steps into {HERE}")


if __name__ == "__main__":
    main()
