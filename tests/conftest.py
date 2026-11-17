import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO_ROOT, "samples")


def make_workspace(tmp_path, active_policy="v2", max_workers=1, **overrides):
    """Copy the deterministic samples into a fresh workspace and return
    (root, config)."""
    root = str(tmp_path)
    shutil.copytree(SAMPLES, os.path.join(root, "samples"))
    with open(os.path.join(root, "samples", "config.json"), encoding="utf-8") as fh:
        config = json.load(fh)
    config["active_policy"] = active_policy
    config["max_workers"] = max_workers
    config.update(overrides)
    with open(os.path.join(root, "samples", "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    return root, config


@pytest.fixture()
def workspace(tmp_path):
    return make_workspace(tmp_path)


def fresh_workbench(root, config):
    from workbench.runner import Workbench

    wb = Workbench(config, root=root)
    wb.load_inputs()
    return wb


def semantic(result):
    """The field set that must match between incremental and full decoding."""
    return {
        "requests": [
            {
                "request_id": e["request_id"],
                "decoder": e["decoder"],
                "tokens": e["tokens"],
                "steps": e["steps"],
                "finish_reason": e["finish_reason"],
                "loss_marks": e["loss_marks"],
            }
            for e in result["requests"]
        ],
        "failures": result["failures"],
        "downgrade_losses": result["downgrade_losses"],
        "decoder_summaries": result["decoder_summaries"],
        "injection_hits": result["injection_hits"],
    }
