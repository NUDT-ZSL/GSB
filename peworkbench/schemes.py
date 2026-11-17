"""Positional-encoding schemes with a unified position-vector interface.

All schemes map a list of integer positions to a (n_positions, dim) matrix of
position vectors built from cosine/sine rotation pairs.  The same query/key
feature interface is shared by every scheme: downstream stages add the
position vector to the feature vector element-wise.
"""

import numpy as np

SCHEME_VERSION = "1.0.0"

_PRECISIONS = {"fp32": np.float32, "fp64": np.float64}


class SchemeUnknown(Exception):
    pass


def precision_dtype(name):
    if name not in _PRECISIONS:
        raise ValueError("unknown precision tier: %r" % (name,))
    return _PRECISIONS[name]


class Scheme:
    name = ""
    version = SCHEME_VERSION
    uses_scale = True

    def frequencies(self, base, scale, dim):
        """Return the per-pair angular frequencies (float64, length dim//2)."""
        raise NotImplementedError

    def effective_positions(self, positions, scale):
        return np.asarray(positions, dtype=np.float64)

    def position_vectors(self, positions, dim, base, scale, precision="fp64"):
        theta = self.frequencies(base, scale, dim)
        pos = self.effective_positions(positions, scale)
        angles = pos[:, None] * theta[None, :]
        vectors = np.empty((len(positions), dim), dtype=np.float64)
        vectors[:, 0::2] = np.cos(angles)
        vectors[:, 1::2] = np.sin(angles)
        return vectors.astype(precision_dtype(precision))

    def cache_params(self, stage, config):
        """Parameter fingerprint for the stages this scheme influences."""
        params = {
            "scheme": self.name,
            "scheme_version": self.version,
            "base_freq": config["base_freq"],
            "dim": config["dim"],
            "precision": config["precision"],
            "extrap_target_len": config["extrap_target_len"],
        }
        if self.uses_scale:
            params["scale_factor"] = config["scale_factor"]
        if stage in ("score", "track"):
            params["train_ctx_len"] = config["train_ctx_len"]
        if stage == "track":
            params["tolerance"] = config["tolerance"]
        return params


class RotaryScheme(Scheme):
    """Rotary position embedding (RoPE) rotation pairs."""

    name = "rope"
    uses_scale = False

    def frequencies(self, base, scale, dim):
        exponents = -2.0 * np.arange(dim // 2, dtype=np.float64) / float(dim)
        return np.power(float(base), exponents)


class LinearInterpScheme(Scheme):
    """Linear position interpolation: positions divided by the scale factor."""

    name = "linear_interp"

    def frequencies(self, base, scale, dim):
        exponents = -2.0 * np.arange(dim // 2, dtype=np.float64) / float(dim)
        return np.power(float(base), exponents)

    def effective_positions(self, positions, scale):
        return np.asarray(positions, dtype=np.float64) / float(scale)


class NtkAwareScheme(Scheme):
    """NTK-aware scaling: base' = base * scale ** (dim / (dim - 2))."""

    name = "ntk_aware"

    def frequencies(self, base, scale, dim):
        scaled_base = float(base) * float(scale) ** (float(dim) / (dim - 2.0))
        exponents = -2.0 * np.arange(dim // 2, dtype=np.float64) / float(dim)
        return np.power(scaled_base, exponents)


_REGISTRY = {cls.name: cls() for cls in (RotaryScheme, LinearInterpScheme, NtkAwareScheme)}


def get_scheme(name):
    try:
        return _REGISTRY[name]
    except KeyError:
        raise SchemeUnknown(name)


def all_scheme_names():
    return sorted(_REGISTRY)
