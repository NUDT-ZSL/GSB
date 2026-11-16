"""Fixed-seed generation of the local deterministic sample set."""

import json
import os

import numpy as np

GENERATOR_SEED = 20261117


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, ensure_ascii=False, indent=2)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
            handle.write("\n")


def build_vocab_rules():
    nouns = [f"noun{i:02d}" for i in range(24)]
    verbs = [f"verb{i:02d}" for i in range(16)]
    modifiers = [f"mod{i:02d}" for i in range(12)]
    words = nouns + verbs + modifiers
    return {
        "format_version": 1,
        "special_tokens": ["<SYS>", "<HIST>", "<Q>"],
        "words": words,
        "embedding": {"seed": GENERATOR_SEED, "dim": 8, "scale": 1.0},
        "limits": {"max_context_tokens": 120},
    }


def build_config():
    return {
        "format_version": 1,
        "block_tokens": 6,
        "hash_derive": {
            "version": "fp-v1",
            "hash_algo": "SHA256",
            "content_prefix": "C",
            "chain_prefix": "P",
        },
        "min_reusable_blocks": 2,
        "residency_budget_blocks": 10,
        "eviction_policy": "LRU",
        "session_retention_cap": 12,
        "max_workers": 4,
        "snapshot_commit_interval": 3,
        "shutdown_observe_interval_ms": 200,
    }


def _phrase(rng, words, length):
    return " ".join(rng.choice(words, size=length, replace=True).tolist())


def _request(index, session, turn, system, history, question, injection_id=None):
    row = {
        "stream_index": index,
        "session": session,
        "turn": turn,
        "system_prompt": system,
        "history": history,
        "question": question,
    }
    if injection_id:
        row["injection_id"] = injection_id
    return row


def build_requests(vocab_rules):
    rng = np.random.default_rng(np.random.SeedSequence(GENERATOR_SEED))
    words = vocab_rules["special_tokens"] + vocab_rules["words"]
    common_system = "<SYS> " + _phrase(rng, words, 10)
    shared_history = "<HIST> " + _phrase(rng, words, 14)
    requests = []

    requests.append(
        _request(0, "alpha", 1, common_system, shared_history,
                 "<Q> " + _phrase(rng, words, 6))
    )
    requests.append(
        _request(1, "beta", 1, common_system, shared_history,
                 "<Q> " + _phrase(rng, words, 6))
    )
    requests.append(
        _request(2, "gamma", 1, common_system,
                 "<HIST> " + _phrase(rng, words, 14),
                 "<Q> " + _phrase(rng, words, 6))
    )
    requests.append(
        _request(3, "alpha", 2, common_system,
                 shared_history + " " + _phrase(rng, words, 8),
                 "<Q> " + _phrase(rng, words, 6))
    )

    long_text = "<SYS> " + _phrase(rng, words, 60)
    requests.append(
        _request(4, "delta", 1, long_text,
                 "<HIST> " + _phrase(rng, words, 50),
                 "<Q> " + _phrase(rng, words, 20))
    )
    requests.append(
        _request(5, "epsilon", 1, common_system, shared_history,
                 "<Q> " + _phrase(rng, words, 6))
    )

    requests.append(
        _request(6, "empty", 1, "", "", "", injection_id="INJ-EMPTY-01")
    )
    requests.append(
        _request(
            7,
            "zeta",
            1,
            common_system,
            shared_history,
            "<Q> word_outside_vocab",
            injection_id="INJ-TOKEN-02",
        )
    )
    requests.append(
        _request(
            8,
            "oversize",
            1,
            long_text,
            "<HIST> " + _phrase(rng, words, 40),
            "<Q> " + _phrase(rng, words, 10),
            injection_id="INJ-CAP-03",
        )
    )
    requests.append(
        _request(
            9,
            "eta",
            1,
            common_system,
            shared_history,
            "<Q> " + _phrase(rng, words, 6),
            injection_id="INJ-CHAIN-04",
        )
    )
    return requests


def build_injection_config():
    return {
        "format_version": 1,
        "injections": [
            {
                "id": "INJ-EMPTY-01",
                "type": "empty_context",
                "target_session": "empty",
                "expected_code": "E_EMPTY_CONTEXT",
            },
            {
                "id": "INJ-TOKEN-02",
                "type": "unknown_token",
                "target_session": "zeta",
                "expected_code": "E_TOKEN_UNKNOWN",
            },
            {
                "id": "INJ-CAP-03",
                "type": "session_retention_exceeded",
                "target_session": "oversize",
                "expected_code": "E_SESSION_LIMIT_EXCEEDED",
            },
            {
                "id": "INJ-CHAIN-04",
                "type": "chain_broken_match",
                "target_session": "eta",
                "expected_code": "E_CHAIN_BROKEN",
            },
            {
                "id": "INJ-POLICY-05",
                "type": "invalid_eviction_policy",
                "eviction_policy": "RANDOM",
                "expected_code": "E_EVICT_POLICY_INVALID",
            },
        ],
    }


def generate_sample_set(sample_dir):
    os.makedirs(sample_dir, exist_ok=True)
    vocab_rules = build_vocab_rules()
    write_json(os.path.join(sample_dir, "vocab_rules.json"), vocab_rules)
    write_json(os.path.join(sample_dir, "cache_config.json"), build_config())
    write_jsonl(
        os.path.join(sample_dir, "request_stream.jsonl"),
        build_requests(vocab_rules),
    )
    write_json(
        os.path.join(sample_dir, "injection_config.json"),
        build_injection_config(),
    )
    return sample_dir
