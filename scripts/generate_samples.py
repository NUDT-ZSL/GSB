from __future__ import annotations

from cold_diag.sample_data import SEED, generate_sample


if __name__ == "__main__":
    paths = generate_sample("data", seed=SEED)
    for name, path in paths.items():
        print(f"{name}: {path}")
