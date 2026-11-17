import json
import os

from workbench.decoders import decode, deterministic_uniform
from workbench.policy import Policy
from workbench.requests_io import load_requests
from workbench.scoring import ScoreTable


def _env(workspace):
    root, config = workspace
    table = ScoreTable.load(os.path.join(root, "samples", "scores.jsonl"))
    requests = load_requests(os.path.join(root, "samples", "requests.jsonl"))
    return table, [r for r in requests if r.context_fingerprint != "ctx_empty"]


def _policy(**kw):
    base = dict(
        decoder_type="sampling",
        temperature=0.8,
        top_k=40,
        top_p=0.95,
        repetition_penalty=1.1,
        seed=12345,
    )
    base.update(kw)
    return Policy(**base)


def test_sampling_deterministic_across_runs(workspace):
    table, requests = _env(workspace)
    pol = _policy()
    first = [decode(pol, table, r) for r in requests]
    second = [decode(pol, table, r) for r in requests]
    assert first == second
    for trace in first:
        assert trace["steps"], trace["request_id"]
        for step in trace["steps"]:
            assert len(step["candidates"]) == 5
            scores = [c["score"] for c in step["candidates"]]
            assert scores == sorted(scores, reverse=True)


def test_zero_temperature_degenerates_to_greedy(workspace):
    table, requests = _env(workspace)
    greedy = _policy(decoder_type="greedy")
    zero_temp = _policy(decoder_type="sampling", temperature=0.0)
    for r in requests:
        g = decode(greedy, table, r)
        s = decode(zero_temp, table, r)
        assert g["tokens"] == s["tokens"]


def test_top_k_zero_and_top_p_one_mean_no_truncation(workspace):
    table, requests = _env(workspace)
    wide = _policy(top_k=0, top_p=1.0)
    narrow = _policy(top_k=1, top_p=1.0)
    # top_k=1 forces the argmax token at every step.
    greedy = _policy(decoder_type="greedy")
    for r in requests:
        assert decode(narrow, table, r)["tokens"] == decode(greedy, table, r)["tokens"]
    # Wide policy still deterministic and valid.
    for r in requests:
        t1 = decode(wide, table, r)["tokens"]
        t2 = decode(wide, table, r)["tokens"]
        assert t1 == t2


def test_random_stream_depends_on_seed_and_request(workspace):
    a = deterministic_uniform("ctx_alpha", 1, 0)
    assert a != deterministic_uniform("ctx_alpha", 2, 0)
    assert a != deterministic_uniform("ctx_beta", 1, 0)
    assert a != deterministic_uniform("ctx_alpha", 1, 1)
    assert a == deterministic_uniform("ctx_alpha", 1, 0)
    assert 0.0 <= a < 1.0


def test_beam_stable_and_records_candidates(workspace):
    table, requests = _env(workspace)
    beam = _policy(decoder_type="beam", beam_width=3, length_alpha=0.7)
    for r in requests:
        t1 = decode(beam, table, r)
        t2 = decode(beam, table, r)
        assert t1 == t2
        assert t1["decoder"] == "beam"
        assert t1["tokens"]
        for step in t1["steps"]:
            assert step["candidates"]
            scores = [c["score"] for c in step["candidates"]]
            assert scores == sorted(scores, reverse=True)


def test_seed_changes_sampling_sequence(workspace):
    table, requests = _env(workspace)
    pol_a = _policy(seed=1)
    pol_b = _policy(seed=2)
    seqs_a = [tuple(decode(pol_a, table, r)["tokens"]) for r in requests]
    seqs_b = [tuple(decode(pol_b, table, r)["tokens"]) for r in requests]
    assert seqs_a != seqs_b


def test_eos_stops_generation(workspace):
    table, requests = _env(workspace)
    pol = _policy(decoder_type="greedy")
    for r in requests:
        trace = decode(pol, table, r)
        if trace["finish_reason"] == "eos":
            assert trace["tokens"][-1] in r.eos_token_ids
            assert len(trace["tokens"]) <= r.max_length
