"""Deterministic synthetic tabular data layer.

All randomness comes from ``random.Random`` seeded with deterministic
string seeds (never ``hash()`` of str, which is process-randomized), so
generated data, fold splits, imputation and resampling are bit-identical
across runs, processes and thread counts.
"""

import hashlib
import random

from .config import NUMERIC


class Dataset:
    def __init__(self, numeric_names, categorical_names, categories,
                 numeric, categorical, y):
        self.numeric_names = numeric_names
        self.categorical_names = categorical_names
        self.categories = categories
        self.numeric = numeric
        self.categorical = categorical
        self.y = y
        self.n = len(y)

    @property
    def feature_dim(self):
        dim = len(self.numeric_names)
        for name in self.categorical_names:
            dim += len(self.categories[name])
        return dim


def generate_data(desc):
    """Generates the base dataset deterministically from the description."""
    seed = desc["seed"]
    n = desc["n_samples"]
    ratio = desc.get("imbalance_ratio", 0.5)

    rng_label = random.Random("%s:label" % seed)
    y = [1 if rng_label.random() < ratio else 0 for _ in range(n)]
    if not any(y):
        y[0] = 1
    if all(y):
        y[0] = 0

    numeric = {}
    categorical = {}
    categories = {}
    numeric_names = []
    categorical_names = []

    columns = desc["columns"]
    feature_columns = desc.get("feature_columns")
    if feature_columns:
        by_name = {col["name"]: col for col in columns}
        columns = [by_name[name] for name in feature_columns]

    for col in columns:
        name = col["name"]
        if col["type"] == NUMERIC:
            numeric_names.append(name)
            rng = random.Random("%s:col:%s" % (seed, name))
            shift = rng.uniform(0.3, 1.0)
            mean = float(col["mean"])
            std = float(col["std"])
            vals = []
            for yi in y:
                offset = shift * std * 0.5 if yi == 1 else -shift * std * 0.5
                vals.append(mean + offset + rng.gauss(0.0, std))
            numeric[name] = vals
        else:
            categorical_names.append(name)
            cats = list(col["categories"])
            categories[name] = cats
            rng = random.Random("%s:col:%s" % (seed, name))
            m = len(cats)
            vals = []
            for yi in y:
                idx = rng.randrange(m)
                if yi == 1:
                    idx = (idx + 1) % m
                vals.append(cats[idx])
            categorical[name] = vals

    missing = desc.get("missing_rate", 0.0)
    if missing > 0.0:
        for name in numeric_names:
            rng = random.Random("%s:missing:%s" % (seed, name))
            numeric[name] = [None if rng.random() < missing else v
                             for v in numeric[name]]
        for name in categorical_names:
            rng = random.Random("%s:missing:%s" % (seed, name))
            categorical[name] = [None if rng.random() < missing else v
                                 for v in categorical[name]]

    ds = Dataset(numeric_names, categorical_names, categories,
                 numeric, categorical, y)
    return oversample_minority(ds, desc.get("resample_target_ratio", 1.0), seed)


def oversample_minority(ds, target_ratio, seed):
    """Deterministically duplicates minority-class rows (cyclic order with
    a seeded shuffle) until minority/majority reaches ``target_ratio``."""
    counts = [ds.y.count(0), ds.y.count(1)]
    minority = 1 if counts[1] <= counts[0] else 0
    majority = 1 - minority
    min_idx = [i for i, v in enumerate(ds.y) if v == minority]
    maj_count = counts[majority]
    want = int(target_ratio * maj_count + 0.999999)
    extra = max(0, want - len(min_idx))

    order = list(range(ds.n))
    if extra > 0 and min_idx:
        pool = list(min_idx)
        random.Random("%s:resample" % seed).shuffle(pool)
        for i in range(extra):
            order.append(pool[i % len(pool)])

    numeric = {name: [ds.numeric[name][i] for i in order]
               for name in ds.numeric_names}
    categorical = {name: [ds.categorical[name][i] for i in order]
                   for name in ds.categorical_names}
    y = [ds.y[i] for i in order]
    return Dataset(ds.numeric_names, ds.categorical_names, ds.categories,
                   numeric, categorical, y)


def fingerprint(ds):
    """Stable content fingerprint of the generated dataset."""
    parts = [
        ",".join(ds.numeric_names),
        ",".join(ds.categorical_names),
        repr(ds.categories),
        repr(ds.y),
    ]
    for name in ds.numeric_names:
        parts.append(repr(ds.numeric[name]))
    for name in ds.categorical_names:
        parts.append(repr(ds.categorical[name]))
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fold_indices(n, k, seed):
    """K non-overlapping folds derived from the seed and sample indices."""
    idx = list(range(n))
    random.Random("fold:%s:%d:%d" % (seed, n, k)).shuffle(idx)
    folds = [sorted(idx[i::k]) for i in range(k)]
    return folds


def _median(sorted_vals):
    m = len(sorted_vals)
    if m == 0:
        return 0.0
    if m % 2 == 1:
        return float(sorted_vals[m // 2])
    return (sorted_vals[m // 2 - 1] + sorted_vals[m // 2]) / 2.0


def _mode(values, categories):
    counts = {}
    for v in values:
        if v is not None:
            counts[v] = counts.get(v, 0) + 1
    best = None
    for cat in categories:
        c = counts.get(cat, 0)
        if best is None or c > counts.get(best, 0):
            best = cat
    return best if best is not None else categories[0]


def prepare_fold(ds, train_idx, test_idx):
    """Fits imputation (train-fold median / mode) and one-hot encoding on
    the train fold only, then encodes both folds. Fully deterministic."""
    medians = {}
    for name in ds.numeric_names:
        vals = sorted(ds.numeric[name][i] for i in train_idx
                      if ds.numeric[name][i] is not None)
        medians[name] = _median(vals)
    modes = {}
    for name in ds.categorical_names:
        modes[name] = _mode((ds.categorical[name][i] for i in train_idx),
                            ds.categories[name])

    def encode(i):
        row = []
        for name in ds.numeric_names:
            v = ds.numeric[name][i]
            row.append(medians[name] if v is None else float(v))
        for name in ds.categorical_names:
            v = ds.categorical[name][i]
            if v is None:
                v = modes[name]
            for cat in ds.categories[name]:
                row.append(1.0 if v == cat else 0.0)
        return row

    x_train = [encode(i) for i in train_idx]
    y_train = [ds.y[i] for i in train_idx]
    x_test = [encode(i) for i in test_idx]
    y_test = [ds.y[i] for i in test_idx]
    return x_train, y_train, x_test, y_test
