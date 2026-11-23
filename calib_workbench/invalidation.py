"""Targeted invalidation propagation across config versions (req. 7).

``diff_configs`` classifies a config change into one of:
  * ``bins_only``          - only histogram n_bins changed
  * ``split_ratio_only``   - only split.ref_ratio changed
  * ``calibrator_set``     - only the calibrator set/params changed
  * ``none`` / ``mixed``
and computes the affected node set and affected group set.  The pipeline
then reuses unaffected upstream artifacts from the content-addressed
cache and recomputes only the affected suffix.
"""

from __future__ import annotations

CHANGE_NONE = "none"
CHANGE_OPERATIONAL = "operational_only"
CHANGE_BINS_ONLY = "bins_only"
CHANGE_SPLIT_RATIO_ONLY = "split_ratio_only"
CHANGE_CALIBRATOR_SET = "calibrator_set"
CHANGE_MIXED = "mixed"

# Sections that never affect computational artifacts (cache keys do not
# cover them), so changes there invalidate nothing.
OPERATIONAL_SECTIONS = ("budget", "scheduler", "limits", "output_dir", "state_dir")


def _strip(cfg, *paths):
    import copy
    out = copy.deepcopy(cfg)
    for path in paths:
        node = out
        for key in path[:-1]:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return out


def diff_configs(old: dict, new: dict) -> dict:
    groups = sorted(new["data"]["groups"])
    if old == new:
        return {"kind": CHANGE_NONE, "affected_nodes": [], "affected_groups": [],
                "details": "configs identical"}

    old_core = _strip(old, *[(s,) for s in OPERATIONAL_SECTIONS])
    new_core = _strip(new, *[(s,) for s in OPERATIONAL_SECTIONS])
    if old_core == new_core:
        changed_ops = sorted(s for s in OPERATIONAL_SECTIONS
                             if old.get(s) != new.get(s))
        return {"kind": CHANGE_OPERATIONAL, "affected_nodes": [],
                "affected_groups": [],
                "details": f"only operational sections changed: {changed_ops}"}
    old, new = old_core, new_core

    rest_after_bins = _strip(old, ("calibrators", "histogram", "n_bins"),
                             ("config_version",)) == \
                      _strip(new, ("calibrators", "histogram", "n_bins"),
                             ("config_version",))
    bins_changed = old["calibrators"].get("histogram", {}).get("n_bins") != \
                   new["calibrators"].get("histogram", {}).get("n_bins")
    if bins_changed and rest_after_bins:
        return {
            "kind": CHANGE_BINS_ONLY,
            "affected_nodes": ["fit:histogram", "apply:histogram",
                               "metrics:histogram", "metrics:__overall__:histogram"],
            "affected_groups": groups,
            "details": "only calibrators.histogram.n_bins changed "
                       f"({old['calibrators'].get('histogram', {}).get('n_bins')} -> "
                       f"{new['calibrators'].get('histogram', {}).get('n_bins')})",
        }

    rest_after_ratio = _strip(old, ("split", "ref_ratio"), ("config_version",)) == \
                       _strip(new, ("split", "ref_ratio"), ("config_version",))
    ratio_changed = old["split"]["ref_ratio"] != new["split"]["ref_ratio"]
    if ratio_changed and rest_after_ratio:
        nodes = ["split", "preprocess"]
        for cal in sorted(new["calibrators"]):
            nodes += [f"fit:{cal}", f"apply:{cal}", f"metrics:{cal}",
                      f"metrics:__overall__:{cal}"]
        return {
            "kind": CHANGE_SPLIT_RATIO_ONLY,
            "affected_nodes": nodes,
            "affected_groups": groups,
            "details": f"only split.ref_ratio changed "
                       f"({old['split']['ref_ratio']} -> {new['split']['ref_ratio']})",
        }

    rest_after_cals = _strip(old, ("calibrators",), ("config_version",)) == \
                      _strip(new, ("calibrators",), ("config_version",))
    if rest_after_cals:
        old_cals = old["calibrators"]
        new_cals = new["calibrators"]
        changed = sorted(c for c in set(old_cals) | set(new_cals)
                         if old_cals.get(c) != new_cals.get(c))
        nodes = []
        for cal in changed:
            nodes += [f"fit:{cal}", f"apply:{cal}", f"metrics:{cal}",
                      f"metrics:__overall__:{cal}"]
        return {
            "kind": CHANGE_CALIBRATOR_SET,
            "affected_nodes": nodes,
            "affected_groups": groups,
            "details": f"calibrator definitions changed for: {changed}",
        }

    return {
        "kind": CHANGE_MIXED,
        "affected_nodes": ["*"],
        "affected_groups": groups,
        "details": "multiple config sections changed; full recompute",
    }
