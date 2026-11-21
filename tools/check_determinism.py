"""Acceptance check: run every subcommand twice from a clean state and
require byte-identical output across the two runs."""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
CONFIG = os.path.join("samples", "config.json")
WORKSPACE = os.path.join(ROOT, "workspace")

SEQUENCE = [
    ["load", "--clock-ms", "3000"],
    ["merge", "--clock-ms", "3100"],
    ["query", "--clock-ms", "3200", "--templates", "legal.json"],
    ["query", "--clock-ms", "3201", "--templates", "invalid_syntax.json"],
    ["query", "--clock-ms", "3202", "--templates", "type_mismatch.json"],
    ["query", "--clock-ms", "1000", "--templates", "legal.json"],
    ["status"],
    ["resume", "--clock-ms", "3300"],
    ["verify"],
]


def run_sequence():
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    outputs = []
    for args in SEQUENCE:
        proc = subprocess.run(
            [PY, "-m", "docengine.cli", "--config", CONFIG] + args,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        outputs.append((args[0], proc.returncode, proc.stdout))
    return outputs


def main():
    first = run_sequence()
    second = run_sequence()
    ok = True
    for (cmd, rc1, out1), (_, rc2, out2) in zip(first, second):
        same = out1 == out2 and rc1 == rc2
        ok = ok and same
        print("%-8s run1_rc=%d run2_rc=%d bytes=%d identical=%s"
              % (cmd, rc1, rc2, len(out1.encode("utf-8")), same))
    print("DETERMINISM: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
