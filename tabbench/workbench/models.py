"""Deterministic pure-Python classifiers.

Determinism rules:
  * logistic regression: full-batch gradient descent, fixed sample order,
    fixed learning rate, loss recorded after every completed epoch;
  * decision tree: gini gain evaluated in fixed (feature, threshold) order,
    ties broken by first-encountered (lowest feature index, lowest
    threshold), left subtree built before right;
  * k-NN: euclidean distance, neighbours sorted by (distance, train index),
    vote ties broken by (higher count, lower label).

Each model receives a BudgetMeter and charges it at cooperative
checkpoints (per epoch / per node / per test row). Exceeding the per-task
quota raises TaskTimeout from inside the loop itself.

IEEE-754 doubles with a fixed operation order make every result
bit-identical across runs and thread counts.
"""

import math

from .errors import TaskTimeout  # noqa: F401  (re-exported for callers)


class BudgetMeter:
    """Deterministic billed-cost ledger for one task."""

    def __init__(self, quota_ms):
        self.quota_ms = quota_ms
        self.used_ms = 0.0

    def charge(self, amount_ms):
        self.used_ms += amount_ms
        if self.used_ms > self.quota_ms:
            raise TaskTimeout(self.used_ms, self.quota_ms)


def _sigmoid(z):
    if z < -500.0:
        return 0.0
    if z > 500.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def logistic_train(x_train, y_train, learning_rate, epochs, meter, epoch_cost_ms):
    n = len(x_train)
    d = len(x_train[0])
    w = [0.0] * d
    b = 0.0
    losses = []
    for _ in range(epochs):
        grad_w = [0.0] * d
        grad_b = 0.0
        loss = 0.0
        for xi, yi in zip(x_train, y_train):
            z = b
            for j in range(d):
                z += w[j] * xi[j]
            p = _sigmoid(z)
            if p <= 0.0:
                p = 1e-15
            elif p >= 1.0:
                p = 1.0 - 1e-15
            loss += -(yi * math.log(p) + (1 - yi) * math.log(1.0 - p))
            err = p - yi
            for j in range(d):
                grad_w[j] += err * xi[j]
            grad_b += err
        inv_n = 1.0 / n
        for j in range(d):
            w[j] -= learning_rate * grad_w[j] * inv_n
        b -= learning_rate * grad_b * inv_n
        losses.append(loss * inv_n)
        meter.charge(epoch_cost_ms)  # checkpoint: quota checked per epoch
    return {"weights": w, "intercept": b, "losses": losses}


def logistic_predict(model, x_rows):
    w = model["weights"]
    b = model["intercept"]
    d = len(w)
    preds = []
    for xi in x_rows:
        z = b
        for j in range(d):
            z += w[j] * xi[j]
        preds.append(1 if _sigmoid(z) >= 0.5 else 0)
    return preds


MAX_THRESHOLDS_PER_FEATURE = 16


def _gini(labels):
    n = len(labels)
    if n == 0:
        return 0.0
    c1 = sum(labels)
    c0 = n - c1
    p0 = c0 / n
    p1 = c1 / n
    return 1.0 - p0 * p0 - p1 * p1


def _candidate_thresholds(sorted_unique):
    if len(sorted_unique) < 2:
        return []
    mids = []
    for a, b in zip(sorted_unique, sorted_unique[1:]):
        mids.append((a + b) / 2.0)
    if len(mids) <= MAX_THRESHOLDS_PER_FEATURE:
        return mids
    step = (len(mids) - 1) / float(MAX_THRESHOLDS_PER_FEATURE - 1)
    picked = []
    seen = set()
    for i in range(MAX_THRESHOLDS_PER_FEATURE):
        idx = int(round(i * step))
        if idx not in seen:
            seen.add(idx)
            picked.append(mids[idx])
    return picked


def _majority(labels):
    c1 = sum(labels)
    c0 = len(labels) - c1
    return 1 if c1 > c0 else 0  # tie -> class 0 (fixed rule)


def tree_train(x_train, y_train, max_depth, min_samples_split, meter,
               node_cost_fn):
    d = len(x_train[0])

    def build(indices, depth):
        labels = [y_train[i] for i in indices]
        meter.charge(node_cost_fn(len(indices), d))  # checkpoint: per node
        if (depth >= max_depth or len(indices) < min_samples_split
                or _gini(labels) == 0.0):
            return {"leaf": True, "label": _majority(labels)}

        parent_impurity = _gini(labels)
        n = len(indices)
        best = None  # (gain, feature, threshold)
        for j in range(d):
            col = sorted(set(x_train[i][j] for i in indices))
            for thr in _candidate_thresholds(col):
                left = [i for i in indices if x_train[i][j] <= thr]
                right = [i for i in indices if x_train[i][j] > thr]
                if not left or not right:
                    continue
                gain = (parent_impurity
                        - len(left) / n * _gini([y_train[i] for i in left])
                        - len(right) / n * _gini([y_train[i] for i in right]))
                if best is None or gain > best[0]:
                    best = (gain, j, thr)
        if best is None or best[0] <= 0.0:
            return {"leaf": True, "label": _majority(labels)}

        _, feat, thr = best
        left_idx = [i for i in indices if x_train[i][feat] <= thr]
        right_idx = [i for i in indices if x_train[i][feat] > thr]
        return {
            "leaf": False,
            "feature": feat,
            "threshold": thr,
            "left": build(left_idx, depth + 1),
            "right": build(right_idx, depth + 1),
        }

    return build(list(range(len(x_train))), 0)


def tree_predict(tree, x_rows):
    preds = []
    for xi in x_rows:
        node = tree
        while not node["leaf"]:
            node = node["left"] if xi[node["feature"]] <= node["threshold"] \
                else node["right"]
        preds.append(node["label"])
    return preds


def knn_predict(x_train, y_train, x_test, k, meter, row_cost_ms):
    preds = []
    for xi in x_test:
        dists = []
        for idx, xt in enumerate(x_train):
            acc = 0.0
            for a, b in zip(xi, xt):
                diff = a - b
                acc += diff * diff
            dists.append((acc, idx))  # (squared euclidean, train index)
        dists.sort(key=lambda t: (t[0], t[1]))
        votes = {}
        for _, idx in dists[:k]:
            label = y_train[idx]
            votes[label] = votes.get(label, 0) + 1
        best_label = None
        for label in sorted(votes):
            if best_label is None or votes[label] > votes[best_label]:
                best_label = label
        preds.append(best_label)
        meter.charge(row_cost_ms)  # checkpoint: per test row
    return preds


def confusion_matrix(y_true, y_pred):
    tn = fp = fn = tp = 0
    for yt, yp in zip(y_true, y_pred):
        if yt == 0 and yp == 0:
            tn += 1
        elif yt == 0 and yp == 1:
            fp += 1
        elif yt == 1 and yp == 0:
            fn += 1
        else:
            tp += 1
    return [[tn, fp], [fn, tp]]


def accuracy(y_true, y_pred):
    if not y_true:
        return 0.0
    correct = sum(1 for yt, yp in zip(y_true, y_pred) if yt == yp)
    return correct / len(y_true)
