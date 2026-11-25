"""Deterministic undercomplete autoencoder with explicit NumPy weight matrices."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .io_utils import array_fingerprint, sha256_hex


@dataclass(frozen=True)
class AEConfig:
    seed: int = 406
    hidden_dim: int = 32
    latent_dim: int = 8
    learning_rate: float = 1.2
    epochs: int = 500
    gradient_clip: float = 20.0


@dataclass
class AEForward:
    reconstruction: np.ndarray
    latent: np.ndarray
    activations: list[np.ndarray]
    preactivations: list[np.ndarray]

    @property
    def hidden_activation(self) -> np.ndarray:
        return self.activations[1]


class DeterministicAutoencoder:
    """Four-layer undercomplete AE.

    Shapes are ``D -> hidden -> latent -> hidden -> D``.  All weights are
    explicitly materialized as named arrays so checksums and replay can prove
    which matrices produced a reconstruction.
    """

    weight_names = ("W1", "b1", "W2", "b2", "W3", "b3", "W4", "b4")

    def __init__(self, input_dim: int, config: AEConfig | None = None):
        self.input_dim = int(input_dim)
        self.config = config or AEConfig()
        if self.config.latent_dim >= self.config.hidden_dim:
            raise ValueError("latent_dim must be smaller than hidden_dim")
        self.weights = self._initial_weights()
        self.fitted_epochs = 0

    def _initial_weights(self) -> dict[str, np.ndarray]:
        cfg = self.config
        rng = np.random.default_rng(cfg.seed)
        d = self.input_dim
        h = int(cfg.hidden_dim)
        z = int(cfg.latent_dim)
        shapes = {
            "W1": (d, h),
            "b1": (h,),
            "W2": (h, z),
            "b2": (z,),
            "W3": (z, h),
            "b3": (h,),
            "W4": (h, d),
            "b4": (d,),
        }
        weights: dict[str, np.ndarray] = {}
        for name, shape in shapes.items():
            if name.startswith("W"):
                fan_in = shape[0]
                weights[name] = rng.normal(0.0, np.sqrt(1.0 / fan_in), size=shape)
            else:
                weights[name] = np.zeros(shape, dtype=np.float64)
        return weights

    @property
    def checksum(self) -> str:
        parts = [f"input_dim={self.input_dim}", f"epochs={self.fitted_epochs}"]
        for name in self.weight_names:
            parts.append(f"{name}:{array_fingerprint(self.weights[name])}")
        return sha256_hex("|".join(parts))

    def forward(self, values: np.ndarray | Sequence[float]) -> AEForward:
        x = np.asarray(values, dtype=np.float64)
        single = x.ndim == 1
        if single:
            x = x[None, :]
        if x.shape[1] != self.input_dim:
            raise ValueError(f"expected dim {self.input_dim}, got {x.shape[1]}")

        w = self.weights
        z1 = x @ w["W1"] + w["b1"]
        a1 = np.tanh(z1)
        z2 = a1 @ w["W2"] + w["b2"]
        a2 = np.tanh(z2)
        z3 = a2 @ w["W3"] + w["b3"]
        a3 = np.tanh(z3)
        z4 = a3 @ w["W4"] + w["b4"]
        a4 = z4

        result = AEForward(
            reconstruction=a4,
            latent=a2,
            activations=[x, a1, a2, a3, a4],
            preactivations=[z1, z2, z3, z4],
        )
        if single:
            result.reconstruction = result.reconstruction[0]
            result.latent = result.latent[0]
            result.activations = [a[0] if a.ndim == 2 else a for a in result.activations]
            result.preactivations = [a[0] for a in result.preactivations]
        return result

    def backward(self, values: np.ndarray, forward_result: AEForward | None = None) -> dict[str, np.ndarray]:
        x = np.asarray(values, dtype=np.float64)
        single = x.ndim == 1
        if single:
            x = x[None, :]
        out = forward_result or self.forward(x)
        recon = np.atleast_2d(out.reconstruction)
        if single and out.reconstruction.ndim == 1:
            recon = out.reconstruction[None, :]
        batch, dim = x.shape
        w = self.weights

        a1 = np.atleast_2d(out.activations[1])
        a2 = np.atleast_2d(out.activations[2])
        a3 = np.atleast_2d(out.activations[3])

        dz4 = 2.0 * (recon - x) / max(1, batch * dim)
        grads: dict[str, np.ndarray] = {}
        grads["W4"] = a3.T @ dz4
        grads["b4"] = dz4.sum(axis=0)
        da3 = dz4 @ w["W4"].T
        dz3 = da3 * (1.0 - a3**2)
        grads["W3"] = a2.T @ dz3
        grads["b3"] = dz3.sum(axis=0)
        da2 = dz3 @ w["W3"].T
        dz2 = da2 * (1.0 - a2**2)
        grads["W2"] = a1.T @ dz2
        grads["b2"] = dz2.sum(axis=0)
        da1 = dz2 @ w["W2"].T
        dz1 = da1 * (1.0 - a1**2)
        grads["W1"] = x.T @ dz1
        grads["b1"] = dz1.sum(axis=0)

        clip = float(self.config.gradient_clip)
        if clip > 0:
            for name, grad in grads.items():
                norm = float(np.linalg.norm(grad))
                if norm > clip:
                    grads[name] = grad * (clip / norm)
        return grads

    def fit(self, windows: Sequence[np.ndarray], epochs: int | None = None) -> list[float]:
        x = np.stack([np.asarray(w, dtype=np.float64).reshape(-1) for w in windows])
        if x.shape[1] != self.input_dim:
            raise ValueError("training window dimension mismatch")
        epochs = int(self.config.epochs if epochs is None else epochs)
        losses = []
        for _ in range(epochs):
            result = self.forward(x)
            recon = np.atleast_2d(result.reconstruction)
            loss = float(np.mean((recon - x) ** 2))
            losses.append(loss)
            grads = self.backward(x, result)
            lr = float(self.config.learning_rate)
            for name in self.weight_names:
                self.weights[name] = self.weights[name] - lr * grads[name]
        self.fitted_epochs += epochs
        return losses

    def save_npz(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        arrays = {name: np.asarray(value) for name, value in self.weights.items()}
        arrays["fitted_epochs"] = np.asarray(self.fitted_epochs, dtype=np.int64)
        arrays["input_dim"] = np.asarray(self.input_dim, dtype=np.int64)
        arrays["config_seed"] = np.asarray(self.config.seed, dtype=np.int64)
        np.savez_compressed(target, **arrays)
        return target

    @classmethod
    def load_npz(cls, path: str | Path, config: AEConfig | None = None) -> "DeterministicAutoencoder":
        with np.load(path, allow_pickle=False) as data:
            input_dim = int(data["input_dim"])
            seed = int(data["config_seed"]) if "config_seed" in data else (config or AEConfig()).seed
            cfg = config or AEConfig(seed=seed)
            if config is not None and config.seed != seed:
                # Explicit persisted seed must win for byte-identical replay.
                cfg = AEConfig(
                    seed=seed,
                    hidden_dim=config.hidden_dim,
                    latent_dim=config.latent_dim,
                    learning_rate=config.learning_rate,
                    epochs=config.epochs,
                    gradient_clip=config.gradient_clip,
                )
            ae = cls(input_dim, cfg)
            for name in ae.weight_names:
                ae.weights[name] = data[name].copy()
            ae.fitted_epochs = int(data["fitted_epochs"]) if "fitted_epochs" in data else 0
        return ae
