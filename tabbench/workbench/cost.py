"""Deterministic cost model.

Budget decisions (global budget gating, per-task quota) are made against
billed virtual milliseconds computed from task shape (fold sizes, feature
dim, hyperparameters). This makes the budget ledger and every scheduling
decision exactly reproducible across runs and concurrency levels, while
real elapsed time is still measured with the monotonic clock and recorded
for audit (see clock.py / report observability section).

Estimated cost = full-run billed cost (tree uses a node-count upper
bound). Actual billed cost equals the estimate unless the task aborts
early on its quota, in which case it is the billed cost of completed work.
"""

DEFAULT_COST_MODEL = {
    "lr_per_epoch_per_sample_feature": 0.02,
    "tree_per_node_per_sample_feature": 0.01,
    "knn_per_row_per_train_feature": 0.002,
}


def cost_model(cfg):
    model = dict(DEFAULT_COST_MODEL)
    raw = cfg.get("cost_model") or {}
    for key in model:
        if key in raw:
            model[key] = float(raw[key])
    return model


def lr_epoch_cost(n_train, dim, costs):
    return n_train * dim * costs["lr_per_epoch_per_sample_feature"]


def tree_node_cost(n_node, dim, costs):
    return n_node * dim * costs["tree_per_node_per_sample_feature"]


def knn_row_cost(n_train, dim, costs):
    return n_train * dim * costs["knn_per_row_per_train_feature"]


def estimate_task_cost(spec, fold_shapes, dim, costs):
    """Upper-bound reservation for one task, in virtual milliseconds."""
    total = 0.0
    if spec.model == "logistic":
        epochs = spec.params["epochs"]
        for n_train, _ in fold_shapes:
            total += epochs * lr_epoch_cost(n_train, dim, costs)
    elif spec.model == "tree":
        max_nodes = 2 ** (spec.params["max_depth"] + 1) - 1
        for n_train, _ in fold_shapes:
            total += max_nodes * tree_node_cost(n_train, dim, costs)
    elif spec.model == "knn":
        for n_train, n_test in fold_shapes:
            total += n_test * knn_row_cost(n_train, dim, costs)
    else:  # pragma: no cover - guarded by config validation
        raise ValueError("unknown model %r" % spec.model)
    return total
