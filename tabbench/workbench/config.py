"""Experiment config + data description loading and validation.

Every validation problem is recorded as an E_CONFIG_INVALID entry in the
failure list instead of aborting the whole run.
"""

import hashlib
import json

import yaml

from .errors import E_CONFIG_INVALID

NUMERIC = "numeric"
CATEGORICAL = "categorical"
KNOWN_MODELS = ("logistic", "tree", "knn")
DEFAULT_MAX_CARDINALITY = 32


def _err(detail):
    return {
        "phase": "load",
        "task_key": None,
        "code": E_CONFIG_INVALID,
        "exc_type": None,
        "detail": detail,
    }


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def config_hash(experiment_raw, data_raw):
    payload = canonical_json({"experiment": experiment_raw, "data": data_raw})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_data_description(desc, max_cardinality=DEFAULT_MAX_CARDINALITY):
    """Returns a list of E_CONFIG_INVALID failure entries (empty if valid)."""
    errs = []
    if not isinstance(desc, dict):
        return [_err("data description is not a mapping")]

    seed = desc.get("seed")
    if not _is_int(seed):
        errs.append(_err("seed_not_integer: seed=%r is not an integer" % (seed,)))

    n_samples = desc.get("n_samples")
    if not _is_int(n_samples) or n_samples <= 0:
        errs.append(_err("type_mismatch: n_samples=%r must be a positive integer" % (n_samples,)))

    missing_rate = desc.get("missing_rate", 0.0)
    if not _is_number(missing_rate) or not (0.0 <= missing_rate < 1.0):
        errs.append(_err("type_mismatch: missing_rate=%r must be in [0, 1)" % (missing_rate,)))

    imbalance = desc.get("imbalance_ratio", 0.5)
    if not _is_number(imbalance) or not (0.0 < imbalance <= 0.5):
        errs.append(_err("type_mismatch: imbalance_ratio=%r must be in (0, 0.5]" % (imbalance,)))

    resample = desc.get("resample_target_ratio", 1.0)
    if not _is_number(resample) or not (0.0 < resample <= 1.0):
        errs.append(_err("type_mismatch: resample_target_ratio=%r must be in (0, 1]" % (resample,)))

    columns = desc.get("columns")
    names = set()
    if not isinstance(columns, list) or not columns:
        errs.append(_err("columns_missing: no column definitions found"))
        columns = []
    for col in columns:
        if not isinstance(col, dict):
            errs.append(_err("type_mismatch: column definition %r is not a mapping" % (col,)))
            continue
        name = col.get("name")
        if not isinstance(name, str) or not name:
            errs.append(_err("type_mismatch: column %r has no valid name" % (col,)))
            continue
        if name in names:
            errs.append(_err("unknown_column: duplicate column name %r" % (name,)))
        names.add(name)
        ctype = col.get("type")
        if ctype not in (NUMERIC, CATEGORICAL):
            errs.append(
                _err("type_mismatch: column %r has unknown type %r" % (name, ctype))
            )
            continue
        if ctype == NUMERIC:
            if not _is_number(col.get("mean")) or not _is_number(col.get("std")):
                errs.append(
                    _err(
                        "type_mismatch: numeric column %r requires numeric mean/std"
                        % (name,)
                    )
                )
        else:
            cats = col.get("categories")
            if (
                not isinstance(cats, list)
                or not cats
                or not all(isinstance(c, str) for c in cats)
            ):
                errs.append(
                    _err(
                        "type_mismatch: categorical column %r requires a non-empty "
                        "list of string categories" % (name,)
                    )
                )
            elif len(cats) > max_cardinality:
                errs.append(
                    _err(
                        "cardinality_exceeded: column %r has %d categories, limit is %d"
                        % (name, len(cats), max_cardinality)
                    )
                )

    target = desc.get("target")
    if target is None:
        errs.append(_err("target_missing: no target column configured"))
    elif not isinstance(target, str):
        errs.append(_err("type_mismatch: target=%r must be a column name" % (target,)))
    elif target in names:
        errs.append(
            _err("unknown_column: target %r collides with a feature column"
                 % (target,))
        )

    feature_columns = desc.get("feature_columns")
    if feature_columns is not None:
        if not isinstance(feature_columns, list) or not feature_columns:
            errs.append(_err("type_mismatch: feature_columns must be a "
                             "non-empty list when present"))
        else:
            for name in feature_columns:
                if name not in names:
                    errs.append(
                        _err("unknown_column: feature column %r is not "
                             "defined in columns" % (name,))
                    )

    return errs


def validate_experiment(cfg):
    """Light structural validation of the experiment config."""
    errs = []
    if not isinstance(cfg, dict):
        return [_err("experiment config is not a mapping")]

    grid = cfg.get("model_grid")
    if not isinstance(grid, dict) or not grid:
        errs.append(_err("model_grid_missing: no model grid configured"))
    else:
        for model, params in grid.items():
            if model not in KNOWN_MODELS:
                errs.append(_err("unknown_column: unknown model type %r" % (model,)))
                continue
            if not isinstance(params, dict) or not params:
                errs.append(_err("type_mismatch: model %r grid must be a mapping" % (model,)))
                continue
            for pname, values in params.items():
                if not isinstance(values, list) or not values:
                    errs.append(
                        _err(
                            "type_mismatch: grid param %s.%s must be a non-empty list"
                            % (model, pname)
                        )
                    )

    sched = cfg.get("scheduler")
    if not isinstance(sched, dict):
        errs.append(_err("scheduler_missing: no scheduler section configured"))
        sched = {}
    for key in ("max_concurrency", "global_budget_ms", "task_quota_ms", "snapshot_interval"):
        value = sched.get(key)
        if not _is_int(value) or value <= 0:
            errs.append(
                _err("type_mismatch: scheduler.%s=%r must be a positive integer" % (key, value))
            )
    seeds = sched.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        errs.append(_err("type_mismatch: scheduler.seeds must be a non-empty list"))
    else:
        for s in seeds:
            if not _is_int(s):
                errs.append(_err("seed_not_integer: scheduler seed %r is not an integer" % (s,)))

    folds = cfg.get("cv_folds", 3)
    if not _is_int(folds) or folds < 2:
        errs.append(_err("type_mismatch: cv_folds=%r must be an integer >= 2" % (folds,)))

    return errs


def load_configs(config_path, data_path=None):
    """Loads both YAML files. Returns (experiment, data_desc, failures, hash).

    `failures` collects E_CONFIG_INVALID entries; callers decide whether the
    run can proceed (data description must be valid to generate data).
    """
    experiment_raw = load_yaml(config_path)
    if data_path is None and isinstance(experiment_raw, dict):
        data_path = experiment_raw.get("data_file")
    if data_path is None:
        data_raw = None
    else:
        data_raw = load_yaml(data_path)

    failures = []
    failures.extend(validate_experiment(experiment_raw))

    limits = {}
    if isinstance(experiment_raw, dict):
        limits = experiment_raw.get("limits") or {}
    max_card = limits.get("max_cardinality", DEFAULT_MAX_CARDINALITY)
    if not _is_int(max_card) or max_card <= 0:
        max_card = DEFAULT_MAX_CARDINALITY

    failures.extend(validate_data_description(data_raw, max_card))

    digest = config_hash(experiment_raw, data_raw)
    return experiment_raw, data_raw, failures, digest
