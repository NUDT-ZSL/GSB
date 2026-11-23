"""Deterministic local corpus generation from a fixed seed (no network)."""

import json
from pathlib import Path

from .util import Splitmix64, sha256_hex

DOMAINS = ["dialog", "news", "tech"]
NOISE_LEVELS = [0.0, 0.0, 0.05, 0.1, 0.1, 0.2, 0.3, 0.4]


def build_vocab(prefix, size):
    return [f"{prefix}{i:04d}" for i in range(size)]


def write_vocab(path, version, tokens):
    lines = [f"version\t{version}"] + tokens
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_vocab(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("version\t"):
        raise ValueError(f"vocab file missing version header: {path}")
    version = int(lines[0].split("\t", 1)[1])
    return version, [ln for ln in lines[1:] if ln]


def generate_corpus(data_dir, seed=20240501, n_pairs=3000, vocab_size=512, vocab_version=1):
    """Generate controlled vocabularies + parallel corpus JSONL, deterministically."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    src_vocab = build_vocab("s", vocab_size)
    tgt_vocab = build_vocab("t", vocab_size)
    write_vocab(data_dir / "src_vocab.txt", vocab_version, src_vocab)
    write_vocab(data_dir / "tgt_vocab.txt", vocab_version, tgt_vocab)

    rng = Splitmix64(seed)
    corpus_path = data_dir / "corpus.jsonl"
    with corpus_path.open("w", encoding="utf-8") as fh:
        for i in range(n_pairs):
            pair_id = f"p{i:06d}"
            src_len = 3 + rng.randint(0, 9)
            tgt_len = max(1, min(14, src_len + rng.randint(-2, 3)))
            src_tokens = [src_vocab[rng.randint(0, vocab_size - 1)] for _ in range(src_len)]
            tgt_tokens = [tgt_vocab[rng.randint(0, vocab_size - 1)] for _ in range(tgt_len)]
            align = [[j, min(tgt_len - 1, (j * tgt_len) // src_len)] for j in range(src_len)]
            rec = {
                "pair_id": pair_id,
                "src_tokens": src_tokens,
                "tgt_tokens": tgt_tokens,
                "align": align,
                "src_len": src_len,
                "tgt_len": tgt_len,
                "noise_level": rng.choice(NOISE_LEVELS),
                "domain_tag": rng.choice(DOMAINS),
            }
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    manifest = {
        "seed": seed,
        "n_pairs": n_pairs,
        "vocab_size": vocab_size,
        "vocab_version": vocab_version,
        "files": {},
    }
    for name in ("src_vocab.txt", "tgt_vocab.txt", "corpus.jsonl"):
        manifest["files"][name] = sha256_hex((data_dir / name).read_bytes())
    (data_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
