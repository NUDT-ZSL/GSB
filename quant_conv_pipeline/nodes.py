"""The four DAG nodes of a quantized conv job.

    quant_input_prep -> weight_quant -> blocked_mac -> reduce_dequant

Each node is a pure function of its canonical payload plus upstream artifacts
(fetched by key). Payloads embed everything needed to replay the node
standalone: the job manifest, versioned parameter rows and upstream
cache keys.
"""
from __future__ import annotations

from . import conv_core
from .cache import ContentCache, hash_array
from .errors import Code, PipelineError
from .quant import dequantize, quantize_int8
from .sample_gen import gen_activation, gen_kernel

QUANT_INPUT_PREP = "quant_input_prep"
WEIGHT_QUANT = "weight_quant"
BLOCKED_MAC = "blocked_mac"
REDUCE_DEQUANT = "reduce_dequant"

NODE_ORDER = (QUANT_INPUT_PREP, WEIGHT_QUANT, BLOCKED_MAC, REDUCE_DEQUANT)

# invalidation propagation: a change at node i invalidates nodes[i:]
DOWNSTREAM = {node: list(NODE_ORDER[i:]) for i, node in enumerate(NODE_ORDER)}

# revision numbers are version bookkeeping, not compute inputs; the
# versioned inputs themselves (weight_params / calib_stats rows) are
# embedded explicitly, so revisions must not leak into cache keys
_BOOKKEEPING = ("weight_revision", "calib_revision")


def _san(job):
    return {k: v for k, v in job.items() if k not in _BOOKKEEPING}


def payload_quant_input_prep(job):
    job = _san(job)
    x = gen_activation(job)
    return {"job": job, "input_sha256": hash_array(x)}


def payload_weight_quant(job, weight_params, upstream_key):
    job = _san(job)
    w = gen_kernel(job)
    return {"job": job, "weight_params": weight_params,
            "kernel_sha256": hash_array(w),
            "upstream": {QUANT_INPUT_PREP: upstream_key}}


def payload_blocked_mac(job, weight_params, input_prep_key, weight_quant_key):
    return {"job": _san(job),
            "weight_zero_point": int(weight_params["weight_zero_point"]),
            "upstream": {QUANT_INPUT_PREP: input_prep_key,
                         WEIGHT_QUANT: weight_quant_key}}


def payload_reduce_dequant(job, weight_params, calib_stats, upstream_key):
    return {"job": _san(job), "calib_stats": calib_stats,
            "weight_scale": weight_params["weight_scale"],
            "upstream": {BLOCKED_MAC: upstream_key}}


def _compute_quant_input_prep(payload, fetch):
    job = payload["job"]
    x = gen_activation(job)
    q = quantize_int8(x, job["quant_scale"], job["zero_point"])
    return {"q_input": q,
            "deq_input": dequantize(q, job["quant_scale"], job["zero_point"])}


def _compute_weight_quant(payload, fetch):
    job = payload["job"]
    wp = payload["weight_params"]
    w = gen_kernel(job)
    q = quantize_int8(w, wp["weight_scale"], wp["weight_zero_point"])
    return {"q_weight": q,
            "deq_weight": dequantize(q, wp["weight_scale"],
                                     wp["weight_zero_point"])}


def _compute_blocked_mac(payload, fetch):
    job = payload["job"]
    n1 = fetch(payload["upstream"][QUANT_INPUT_PREP])
    n2 = fetch(payload["upstream"][WEIGHT_QUANT])
    kh, kw = int(job["kernel_shape"][2]), int(job["kernel_shape"][3])
    q_cols = conv_core.im2col(n1["q_input"], kh, kw,
                              int(job["stride"]), int(job["padding"]),
                              pad_value=int(job["zero_point"]))
    q_wmat = conv_core.kernel_matrix(n2["q_weight"])
    wgt_zp = int(payload["weight_zero_point"])
    acc = conv_core.blocked_int8_mac(
        q_cols, q_wmat, int(job["block_size"]),
        int(job["zero_point"]), wgt_zp)
    return {"acc": acc}


def _compute_reduce_dequant(payload, fetch):
    job = payload["job"]
    calib = payload["calib_stats"]
    n3 = fetch(payload["upstream"][BLOCKED_MAC])
    q_out = conv_core.dequant_output(
        n3["acc"], job["quant_scale"], payload["weight_scale"],
        calib["out_scale"], calib["out_zero_point"])
    return {"q_out": q_out,
            "f_out": dequantize(q_out, calib["out_scale"],
                                calib["out_zero_point"])}


COMPUTE = {
    QUANT_INPUT_PREP: _compute_quant_input_prep,
    WEIGHT_QUANT: _compute_weight_quant,
    BLOCKED_MAC: _compute_blocked_mac,
    REDUCE_DEQUANT: _compute_reduce_dequant,
}


def run_node(cache: ContentCache, node: str, payload: dict):
    """Execute ``node`` content-addressably. Returns (key, artifact, reused)."""
    key = ContentCache.key_for(node, payload)
    hit = cache.get(key)
    if hit is not None:
        return key, hit[0], True

    def fetch(upstream_key):
        got = cache.get(upstream_key)
        if got is None:
            raise PipelineError(
                Code.E_LINEAGE_BROKEN,
                f"upstream artifact missing for key {upstream_key}",
                job_id=payload["job"].get("job_id"), node=node,
                missing_key=upstream_key)
        return got[0]

    artifact = COMPUTE[node](payload, fetch)
    cache.put(key, artifact, {"node": node,
                              "job_id": payload["job"].get("job_id"),
                              "payload": payload})
    return key, artifact, False


def replay(cache: ContentCache, key: str):
    """Recompute a cache entry from its recorded payload.

    Returns (artifact, matches_stored_hash). Raises E_LINEAGE_BROKEN when
    the entry or any upstream dependency is missing.
    """
    meta = cache.meta(key)
    if meta is None:
        raise PipelineError(Code.E_LINEAGE_BROKEN,
                            f"no cache entry for key {key}",
                            missing_key=key)
    node = meta["node"]
    payload = meta["payload"]

    def fetch(upstream_key):
        got = cache.get(upstream_key)
        if got is None:
            raise PipelineError(
                Code.E_LINEAGE_BROKEN,
                f"upstream artifact missing for key {upstream_key}",
                job_id=payload["job"].get("job_id"), node=node,
                missing_key=upstream_key)
        return got[0]

    artifact = COMPUTE[node](payload, fetch)
    from .cache import hash_bytes, serialize_artifact
    digest = hash_bytes(serialize_artifact(artifact))
    return artifact, digest == meta["artifact_sha256"]
