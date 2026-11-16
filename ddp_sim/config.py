"""Simulation configuration, validation and JSON config-file loading."""

import json
from dataclasses import dataclass, field, asdict

from .errors import E_BLOCK, E_INIT, SimError
from .model import validate_init

DEFAULT_BASE_PORT = 29500
DEFAULT_LR = 0.01


@dataclass
class SimConfig:
    workers: int = 4
    dim: int = 1024
    seed: int = 20240501
    block_size: int = 256
    interleave: int = 2
    base_port: int = DEFAULT_BASE_PORT
    lr: float = DEFAULT_LR
    delays_ms: dict = field(default_factory=dict)  # rank -> milliseconds
    log_dir: str = "runs/latest"
    recv_timeout_s: float = 60.0

    def validate(self):
        validate_init(self.seed, self.workers, self.dim)
        if isinstance(self.block_size, bool) or not isinstance(self.block_size, int) or self.block_size <= 0:
            raise SimError(E_BLOCK, "block size must be a positive integer",
                           block_size=repr(self.block_size))
        if self.block_size > self.dim:
            raise SimError(E_BLOCK, "block size exceeds shard dimension",
                           block_size=self.block_size, dim=self.dim)
        if not 1 <= int(self.interleave) <= 64:
            raise SimError(E_INIT, "interleave depth must be in [1, 64]",
                           interleave=self.interleave)
        if not 1024 <= int(self.base_port) <= 65535 - 8:
            raise SimError(E_INIT, "base_port out of usable range",
                           base_port=self.base_port)
        if float(self.lr) <= 0:
            raise SimError(E_INIT, "learning rate must be positive", lr=self.lr)
        for rank in self.delays_ms:
            if not 0 <= int(rank) < self.workers:
                raise SimError(E_INIT, "delay configured for unknown rank", rank=rank)
            if float(self.delays_ms[rank]) < 0:
                raise SimError(E_INIT, "delay must be non-negative", rank=rank)

    @property
    def num_chunks(self):
        return (self.dim + self.block_size - 1) // self.block_size

    @property
    def chunk_sizes(self):
        sizes = [self.block_size] * self.num_chunks
        tail = self.dim - self.block_size * (self.num_chunks - 1)
        sizes[-1] = tail
        return sizes

    def ports(self):
        return [self.base_port + r for r in range(self.workers)]

    def to_dict(self):
        data = asdict(self)
        data["delays_ms"] = {str(k): v for k, v in self.delays_ms.items()}
        return data


def load_config_file(path):
    """Load a JSON seed/config file into a SimConfig."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SimError(E_INIT, f"cannot read config file: {exc}", path=path)
    delays = raw.pop("delays_ms", raw.pop("delays", {})) or {}
    cfg = SimConfig(
        workers=int(raw.get("workers", 4)),
        dim=int(raw.get("dim", 1024)),
        seed=int(raw.get("seed", 20240501)),
        block_size=int(raw.get("block_size", 256)),
        interleave=int(raw.get("interleave", 2)),
        base_port=int(raw.get("base_port", DEFAULT_BASE_PORT)),
        lr=float(raw.get("lr", raw.get("learning_rate", DEFAULT_LR))),
        delays_ms={int(k): float(v) for k, v in delays.items()},
    )
    cfg.validate()
    return cfg
