"""Generate the deterministic local sample set and the baseline manifest.

Everything is derived from fixed seeds; no network, no real model weights.
"""

from __future__ import annotations

import json
import os

import numpy as np

VOCAB_SIZE = 64
EOS_ID = 1
SCORE_SEED = 20240501
REQUEST_SEED = 778899
MAX_STEPS = 12

CONTEXTS = ["ctx_alpha", "ctx_beta", "ctx_gamma", "ctx_delta", "ctx_epsilon"]


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_json(path, data):
    _write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def generate(root):
    samples = os.path.join(root, "samples")
    os.makedirs(samples, exist_ok=True)

    # Vocabulary: id<TAB>token, plus subword merge rules (deterministic text).
    vocab_lines = ["0\t<pad>", f"{EOS_ID}\t<eos>"]
    for i in range(2, VOCAB_SIZE):
        vocab_lines.append(f"{i}\ttok_{i:03d}")
    _write(os.path.join(samples, "vocab.txt"), "\n".join(vocab_lines) + "\n")

    rules = ["# subword merge rules: left right -> merged"]
    for i in range(2, VOCAB_SIZE - 1, 2):
        rules.append(f"tok_{i:03d} tok_{i+1:03d} -> tok_{i:03d}tok_{i+1:03d}")
    _write(os.path.join(samples, "subword_rules.txt"), "\n".join(rules) + "\n")

    # Score table: fixed-seed logits per context fingerprint and step.
    rng = np.random.RandomState(SCORE_SEED)
    scores_path = os.path.join(samples, "scores.jsonl")
    with open(scores_path, "w", encoding="utf-8", newline="\n") as fh:
        for ctx in CONTEXTS:
            steps = []
            for _ in range(MAX_STEPS):
                logits = rng.normal(loc=0.0, scale=2.0, size=VOCAB_SIZE)
                steps.append([round(float(x), 6) for x in logits])
            fh.write(json.dumps({"fingerprint": ctx, "steps": steps}) + "\n")

    # Request stream.
    rng_req = np.random.RandomState(REQUEST_SEED)
    requests = []
    for i, ctx in enumerate(CONTEXTS):
        requests.append(
            {
                "request_id": f"req-{i+1:03d}",
                "context_fingerprint": ctx,
                "max_length": int(rng_req.randint(6, MAX_STEPS + 1)),
                "eos_token_ids": [EOS_ID],
            }
        )
    # One request targets the injected empty score entry.
    requests.append(
        {
            "request_id": "req-006",
            "context_fingerprint": "ctx_empty",
            "max_length": 8,
            "eos_token_ids": [EOS_ID],
        }
    )
    req_path = os.path.join(samples, "requests.jsonl")
    with open(req_path, "w", encoding="utf-8", newline="\n") as fh:
        for rec in requests:
            fh.write(json.dumps(rec) + "\n")

    # v1 flat key=value policy (milli units, 32-bit seed).
    _write(
        os.path.join(samples, "policy_v1.txt"),
        "decoder=sampling temp_milli=800 top_k=40 top_p_milli=950 "
        "rp_milli=1100 seed32=12345\n",
    )

    # v2 JSON policy (floats, 64-bit seed, beam decoder).
    _write_json(
        os.path.join(samples, "policy_v2.json"),
        {
            "decoder": {"type": "beam", "beam_width": 3, "length_alpha": 0.7},
            "temperature": 0.8,
            "top_k": 40,
            "top_p": 0.95,
            "repetition_penalty": 1.1,
            "seed": 123456789012,
        },
    )

    # Fault-injection configuration.
    _write_json(
        os.path.join(samples, "injection.json"),
        {
            "items": [
                {
                    "id": "INJ-001",
                    "kind": "policy_line",
                    "version": "v1",
                    "line": "decoder=sampling temp_milli=800 top_k=40",
                },
                {
                    "id": "INJ-002",
                    "kind": "policy_line",
                    "version": "v1",
                    "line": "decoder=sampling temp_milli=99999 top_k=40 "
                    "top_p_milli=900 rp_milli=1000 seed32=7",
                },
                {
                    "id": "INJ-003",
                    "kind": "policy_line",
                    "version": "v1",
                    "line": "decoder=beamsample temp_milli=800 top_k=40 "
                    "top_p_milli=900 rp_milli=1000 seed32=7",
                },
                {
                    "id": "INJ-004",
                    "kind": "policy_json_downgrade",
                    "doc": {
                        "decoder": {"type": "sampling"},
                        "temperature": 0.8,
                        "top_k": 40,
                        "top_p": 0.9,
                        "repetition_penalty": 1.0,
                        "seed": 99999999999,
                    },
                },
                {
                    "id": "INJ-005",
                    "kind": "score_empty_entry",
                    "fingerprint": "ctx_empty",
                },
            ]
        },
    )

    # Concurrency / snapshot / conversion configuration.
    _write_json(
        os.path.join(samples, "config.json"),
        {
            "paths": {
                "vocab": "samples/vocab.txt",
                "subword_rules": "samples/subword_rules.txt",
                "scores": "samples/scores.jsonl",
                "requests": "samples/requests.jsonl",
                "policy_v1": "samples/policy_v1.txt",
                "policy_v2": "samples/policy_v2.json",
                "injection": "samples/injection.json",
                "workdir": "out",
            },
            "active_policy": "v2",
            "max_workers": 4,
            "candidate_top_n": 5,
            "temp_quantum": 0.001,
            "seed_width_rule": "truncate",
            "snapshot_version": 1,
            "recover_from_snapshot": True,
        },
    )


def generate_baseline(root):
    """Run the pipeline once and record the expected baseline manifest."""
    import shutil
    import sys

    sys.path.insert(0, root)
    from workbench.convert import convert_policy
    from workbench.policy import parse_policy_text
    from workbench.runner import Workbench

    config_path = os.path.join(root, "samples", "config.json")
    with open(config_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    out_dir = os.path.join(root, config["paths"]["workdir"])
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    wb = Workbench(config, root=root)
    wb.load_inputs()
    result = wb.run()

    with open(os.path.join(root, "samples", "policy_v2.json"), encoding="utf-8") as fh:
        v2_policy = parse_policy_text(fh.read(), "v2")
    _, sidecar, losses = convert_policy(v2_policy, "v1")

    baseline = {
        "seeds": {"score_seed": SCORE_SEED, "request_seed": REQUEST_SEED},
        "expected_tokens": {e["request_id"]: e["tokens"] for e in result["requests"]},
        "expected_finish": {
            e["request_id"]: e["finish_reason"] for e in result["requests"]
        },
        "downgrade_losses": [e.to_dict() for e in losses],
        "downgrade_sidecar": sidecar,
        "injection_hits": result["injection_hits"],
        "failure_codes": sorted({f["code"] for f in result["failures"]}),
    }
    _write_json(os.path.join(root, "samples", "baseline.json"), baseline)
    return baseline


if __name__ == "__main__":
    generate(".")
    generate_baseline(".")
