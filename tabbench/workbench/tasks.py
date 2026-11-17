"""Hyperparameter grid expansion and deterministic task execution."""

from . import cost as cost_mod
from .data import fold_indices, prepare_fold
from .errors import E_TASK_TIMEOUT, TaskExecutionError, TaskTimeout
from .models import (BudgetMeter, accuracy, confusion_matrix, knn_predict,
                     logistic_predict, logistic_train, tree_predict,
                     tree_train)

MODEL_ORDER = ("logistic", "tree", "knn")


class TaskSpec:
    def __init__(self, key, model, params, seed):
        self.key = key
        self.model = model
        self.params = params
        self.seed = seed


def task_key(model, params, seed):
    """Stable task key: canonical param rendering, sorted param names."""
    rendered = ",".join("%s=%r" % (name, params[name]) for name in sorted(params))
    return "%s[%s]#%d" % (model, rendered, seed)


def expand_tasks(cfg):
    """Enumerates the full grid x seeds, sorted by stable key; assigns the
    enqueue sequence number used for deterministic result merging."""
    grid = cfg["model_grid"]
    seeds = cfg["scheduler"]["seeds"]
    specs = []
    for model in MODEL_ORDER:
        params_grid = grid.get(model)
        if not params_grid:
            continue
        names = sorted(params_grid)
        combos = [[]]
        for name in names:
            combos = [prefix + [value] for prefix in combos
                      for value in params_grid[name]]
        for combo in combos:
            params = dict(zip(names, combo))
            for seed in seeds:
                specs.append(TaskSpec(task_key(model, params, seed),
                                      model, params, seed))
    specs.sort(key=lambda s: s.key)
    for seq, spec in enumerate(specs):
        spec.enqueue_seq = seq
    return specs


def result_sort_key(task_key_str, fold_index):
    """Merge key carried by every fold result; ordering never depends on
    thread completion order."""
    return "%s#fold=%d" % (task_key_str, fold_index)


def run_task(spec, dataset, cfg):
    """Executes one training task. Pure function of (spec, dataset, cfg):
    no shared mutable state, no wall-clock dependence in any output value.

    Returns a result dict. On quota exhaustion returns a deterministic
    E_TASK_TIMEOUT result preserving partial progress. Unexpected errors
    are wrapped in TaskExecutionError with the phase name.
    """
    folds_count = cfg.get("cv_folds", 3)
    quota_ms = float(cfg["scheduler"]["task_quota_ms"])
    costs = cost_mod.cost_model(cfg)
    meter = BudgetMeter(quota_ms)
    dim = dataset.feature_dim

    try:
        folds = fold_indices(dataset.n, folds_count, spec.seed)
    except Exception as exc:  # pragma: no cover - defensive
        raise TaskExecutionError("fold", exc)

    all_indices = set(range(dataset.n))
    fold_results = []
    losses = []
    status = "ok"
    timeout_code = None

    for fold_no, test_idx in enumerate(folds):
        train_idx = sorted(all_indices - set(test_idx))
        n_train = len(train_idx)
        try:
            x_train, y_train, x_test, y_test = prepare_fold(
                dataset, train_idx, test_idx)
        except Exception as exc:
            raise TaskExecutionError("fold", exc)
        try:
            if spec.model == "logistic":
                model = logistic_train(
                    x_train, y_train,
                    float(spec.params["learning_rate"]),
                    int(spec.params["epochs"]),
                    meter,
                    cost_mod.lr_epoch_cost(n_train, dim, costs))
                losses.append(model["losses"])
                preds = logistic_predict(model, x_test)
            elif spec.model == "tree":
                tree = tree_train(
                    x_train, y_train,
                    int(spec.params["max_depth"]),
                    int(spec.params["min_samples_split"]),
                    meter,
                    lambda n, d: cost_mod.tree_node_cost(n, d, costs))
                preds = tree_predict(tree, x_test)
            elif spec.model == "knn":
                preds = knn_predict(
                    x_train, y_train, x_test,
                    int(spec.params["k"]),
                    meter,
                    cost_mod.knn_row_cost(n_train, dim, costs))
            else:  # pragma: no cover - guarded by config validation
                raise ValueError("unknown model %r" % spec.model)
        except TaskTimeout:
            status = "timeout"
            timeout_code = E_TASK_TIMEOUT
            break
        except Exception as exc:
            raise TaskExecutionError("train", exc)
        try:
            fold_results.append({
                "fold": fold_no,
                "sort_key": result_sort_key(spec.key, fold_no),
                "test_indices": list(test_idx),
                "accuracy": accuracy(y_test, preds),
                "confusion": confusion_matrix(y_test, preds),
                "predictions": list(preds),
            })
        except Exception as exc:  # pragma: no cover - defensive
            raise TaskExecutionError("predict", exc)

    completed = [f["accuracy"] for f in fold_results]
    mean_accuracy = sum(completed) / len(completed) if completed else None
    return {
        "task_key": spec.key,
        "model": spec.model,
        "params": spec.params,
        "seed": spec.seed,
        "enqueue_seq": spec.enqueue_seq,
        "status": status,
        "code": timeout_code,
        "timed_out": status == "timeout",
        "folds": fold_results,
        "folds_completed": len(fold_results),
        "losses": losses,
        "mean_accuracy": mean_accuracy,
        "billed_ms": meter.used_ms,
    }
