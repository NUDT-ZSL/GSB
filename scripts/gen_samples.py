"""生成确定性样本清单与数组缓存：python scripts/gen_samples.py"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rope_workbench import samples


def main():
    cases, manifest = samples.generate_all("data", "cache")
    print(f"manifest: {manifest} ({len(cases)} cases)")
    for c in cases:
        print(" ", c["case_id"], c["pos_encoding_variant"])


if __name__ == "__main__":
    main()
