#!/usr/bin/env python3
"""样本校验: 事件序号递增、逻辑时钟非负、依赖图无环、枚举合法。

用法: python3 scripts/validate_samples.py [samples_dir]
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alertcorr import errors as E
from alertcorr.validation import validate_events_list, validate_topology_dict


def main():
    samples = sys.argv[1] if len(sys.argv) > 1 else "samples"
    with open(os.path.join(samples, "topology.json"), encoding="utf-8") as f:
        topo_raw = json.load(f)
    with open(os.path.join(samples, "events.json"), encoding="utf-8") as f:
        events_raw = json.load(f)["events"]
    try:
        topo = validate_topology_dict(topo_raw)
        events = validate_events_list(events_raw, topo)
    except E.BusinessError as exc:
        print("SAMPLE INVALID: %s" % exc.to_dict())
        return 1
    print("samples OK: services=%d instances=%d events=%d "
          "seq_increasing=yes clock_non_negative=yes dag=yes"
          % (len(topo.services), len(topo.instances), len(events)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
