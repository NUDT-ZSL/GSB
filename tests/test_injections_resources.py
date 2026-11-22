from __future__ import annotations

from dataclasses import replace

import pytest

from panoweave.cache import ContentAddressedCache
from panoweave.errors import BusinessError, ErrorCode
from panoweave.injections import (
    AttributionLedger,
    InjectionSpec,
)
from panoweave.pipeline import render_text
from panoweave.resources import plan_resources
from panoweave.samples import parameter_text


def test_parse_injection_produces_attributed_record(sample, tmp_path):
    text = parameter_text(sample["root"], "v3")
    spec = InjectionSpec("parse", "extrinsic_dimension", view_id=0)
    with pytest.raises(BusinessError) as caught:
        render_text(
            text,
            sample["images"],
            config=sample["config"],
            cache=ContentAddressedCache(tmp_path / "cache", 8),
            injections=[spec],
        )
    error = caught.value
    assert error.code == ErrorCode.FORMAT_CORRUPT
    ledger = error.details["ledger"]
    assert ledger["injections"][0]["stage"] == "parse"
    assert ledger["injections"][0]["code"] == ErrorCode.FORMAT_CORRUPT
    assert ledger["stage_records"][0]["stage"] == "parse"
    assert ledger["all_attributed"] is True


def test_resource_memory_limit_and_parallel_degrade(sample):
    scene = sample["parsed"]["v3"].scene
    low_memory = replace(sample["config"].resources, max_memory_bytes=1)
    with pytest.raises(BusinessError) as caught:
        plan_resources(scene, low_memory, 4)
    assert caught.value.code == ErrorCode.RESOURCE_LIMIT
    limited_parallel = replace(
        sample["config"].resources, max_parallel_tasks=1, on_limit="degrade"
    )
    plan = plan_resources(scene, limited_parallel, 3)
    assert plan.effective_parallel_tasks == 1
    assert plan.notices[0]["category"] == "parallelism_reduced"


def test_cache_entry_limit_degrades_and_evicts(sample, tmp_path):
    cache = ContentAddressedCache(tmp_path / "cache", 1)
    result = render_text(
        parameter_text(sample["root"], "v3"),
        sample["images"],
        config=sample["config"],
        cache=cache,
    )
    report = result.ledger.report()
    assert any(
        item["category"] == "cache_entry_evicted"
        for item in report["degradations"]
    )
    assert report["degradation_chain_traceable"] is True
    assert cache.report()["entries"] <= 1


def test_unattributed_errors_are_summarized():
    ledger = AttributionLedger()
    ledger.unattributed_error("unit-test", RuntimeError("boom"))
    report = ledger.report()
    assert report["all_attributed"] is False
    assert report["unattributed"][0]["exception_type"] == "RuntimeError"
    assert report["unattributed"][0]["message"] == "boom"

