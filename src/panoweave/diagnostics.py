"""End-to-end diagnostic run and interactive HTML report."""

from __future__ import annotations

import html
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from .cache import ContentAddressedCache
from .config import AppConfig
from .errors import BusinessError, ErrorCode
from .formats import parse_text, serialize_v1, serialize_v3
from .injections import default_injection_specs
from .migration import (
    assert_representable_equality,
    assert_semantic_equivalence,
    check_roundtrip,
    migrate_scene,
)
from .model import DistortionStage
from .outputs import write_render_outputs
from .pipeline import RenderResult, render_text
from .pngutil import encode_png, mask_png
from .samples import load_manifest, load_sample_images, parameter_text
from .snapshots import (
    SnapshotStore,
    migration_payload,
    parameter_payload,
    render_payload,
)

VERSIONS = ("v1", "v2", "v3")


def _error_dict(error: BusinessError) -> dict[str, Any]:
    return error.to_dict()


def _check_manifest(samples_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    missing: list[str] = []
    for relative, expected in sorted(manifest.get("files", {}).items()):
        path = samples_root / relative
        if not path.exists():
            missing.append(relative)
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            mismatches.append(
                {"file": relative, "expected": expected, "actual": digest}
            )
    return {
        "ok": not mismatches and not missing,
        "missing": missing,
        "mismatches": mismatches,
        "checked_files": len(manifest.get("files", {})),
    }


def _render_all_versions(
    samples_root: Path,
    out_root: Path,
    config: AppConfig,
    parsed: dict[str, Any],
    images: dict[int, np.ndarray],
    *,
    clean_cache: bool,
) -> tuple[dict[str, RenderResult], dict[str, dict[str, Any]], dict[str, Any]]:
    cache_dir = out_root / "cache"
    cache = ContentAddressedCache(cache_dir, config.resources.max_cache_entries)
    if clean_cache:
        cache.clear()
    renders: dict[str, RenderResult] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    for version in VERSIONS:
        text = parameter_text(samples_root, version)
        result = render_text(text, images, config=config, cache=cache)
        if result.scene_hash != parsed[version].scene.semantic_hash:
            raise BusinessError(
                ErrorCode.INCONSISTENT,
                f"{version} render used a different semantic scene hash",
                stage="diagnostics",
                field_path="scene_hash",
            )
        renders[version] = result
        asset_dir = out_root / "assets" / version
        artifacts[version] = write_render_outputs(result, asset_dir)
    first_hashes = {
        "panorama_hash": renders["v1"].panorama_hash,
        "mask_hash": renders["v1"].mask_hash,
        "geometry_hash": renders["v1"].geometry_hash,
        "weight_hash": renders["v1"].weight_hash,
        "panorama_png_hash": artifacts["v1"]["files"]["panorama_png"],
        "mask_png_hash": artifacts["v1"]["files"]["valid_mask_png"],
    }
    cache.clear()
    rerun = render_text(
        parameter_text(samples_root, "v1"),
        images,
        config=config,
        cache=cache,
    )
    rerun_hashes = {
        "panorama_hash": rerun.panorama_hash,
        "mask_hash": rerun.mask_hash,
        "geometry_hash": rerun.geometry_hash,
        "weight_hash": rerun.weight_hash,
        "panorama_png_hash": hashlib.sha256(
            encode_png(rerun.panorama_rgb)
        ).hexdigest(),
        "mask_png_hash": hashlib.sha256(
            encode_png(mask_png(rerun.valid_mask))
        ).hexdigest(),
    }
    persistence = {
        "first_run": first_hashes,
        "rerun_after_cache_clear": rerun_hashes,
        "byte_identical": first_hashes == rerun_hashes,
    }
    return renders, artifacts, persistence


def _render_report(result: RenderResult, preview_prefix: str) -> dict[str, Any]:
    report = result.report()
    report["previews"] = {
        "panorama": f"{preview_prefix}/panorama.png",
        "valid_mask": f"{preview_prefix}/valid_mask.png",
        "weight_map": f"{preview_prefix}/weight_map.png",
        "weight_heatmap": f"{preview_prefix}/weight_heatmap.png",
    }
    return report


def _run_migrations(
    parsed: dict[str, Any], config: AppConfig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    migrations: list[dict[str, Any]] = []
    roundtrips: list[dict[str, Any]] = []
    for source_version in VERSIONS:
        for target_version in VERSIONS:
            try:
                result = migrate_scene(
                    parsed[source_version],
                    target_version,
                    config.parse_defaults,
                )
                migrations.append(result.report_dict())
            except BusinessError as error:
                migrations.append(
                    {
                        "source_version": source_version,
                        "target_version": target_version,
                        "error": _error_dict(error),
                    }
                )
        for via_version in VERSIONS:
            try:
                roundtrips.append(
                    check_roundtrip(
                        parsed[source_version],
                        via_version,
                        config.parse_defaults,
                    )
                )
            except BusinessError as error:
                roundtrips.append(
                    {
                        "source_version": source_version,
                        "via_version": via_version,
                        "error": _error_dict(error),
                    }
                )
    return migrations, roundtrips


def _drift_probe(
    parsed: dict[str, Any], config: AppConfig, samples_root: Path
) -> dict[str, Any]:
    base = parsed["v3"].scene
    first = base.views[0]
    enriched_chain = first.distortion_chain + (
        DistortionStage("scale", (("sx", 1.02), ("sy", 1.02))),
    )
    views = list(base.views)
    views[0] = replace(first, distortion_chain=enriched_chain)
    custom_scene = replace(
        base,
        feather_radius=13.5,
        views=tuple(views),
    )
    custom_text = serialize_v3(custom_scene)
    custom = parse_text(custom_text, config.parse_defaults)
    try:
        assert_semantic_equivalence(
            [parsed["v1"], custom],
            config.parse_defaults,
        )
        return {
            "triggered": False,
            "message": "expected E_SEMANTIC_DRIFT was not raised",
        }
    except BusinessError as error:
        return {
            "triggered": error.code == ErrorCode.SEMANTIC_DRIFT,
            "code": error.code,
            "field_path": error.field_path,
            "unrecoverable_fields": error.details.get("unrecoverable_fields", []),
            "details": error.details,
        }


def _mismatch_probe(parsed: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    source = parsed["v2"]
    via = migrate_scene(source, "v1", config.parse_defaults)
    reparsed = parse_text(
        serialize_v1(via.scene),
        config.parse_defaults,
    )
    views = list(reparsed.scene.views)
    views[0] = replace(views[0], focal=views[0].focal + 1.0)
    faulty = replace(reparsed.scene, views=tuple(views))
    try:
        assert_representable_equality(
            source.scene,
            faulty,
            "v1",
            source_version="v2",
        )
        return {
            "triggered": False,
            "message": "expected E_ROUNDTRIP_MISMATCH was not raised",
        }
    except BusinessError as error:
        return {
            "triggered": error.code == ErrorCode.ROUNDTRIP_MISMATCH,
            "code": error.code,
            "field_path": error.field_path,
            "details": error.details,
        }


def _run_injections(
    samples_root: Path,
    out_root: Path,
    config: AppConfig,
    images: dict[int, np.ndarray],
    renders: dict[str, RenderResult],
) -> list[dict[str, Any]]:
    text = parameter_text(samples_root, "v3")
    cache = ContentAddressedCache(
        out_root / "injection_cache", config.resources.max_cache_entries
    )
    cache.clear()
    results: list[dict[str, Any]] = []
    for spec in default_injection_specs():
        entry: dict[str, Any] = {
            "stage": spec.stage,
            "kind": spec.kind,
            "view_id": spec.view_id,
        }
        try:
            result = render_text(
                text,
                images,
                config=config,
                cache=cache,
                injections=[spec],
            )
            entry["recovered"] = True
            entry["triggered_code"] = (
                result.ledger.report()["latest_business_error_code"]
            )
            entry["render"] = result.report()
            preview_dir = out_root / "assets" / "injections"
            write_render_outputs(result, preview_dir / spec.stage)
            entry["preview"] = f"assets/injections/{spec.stage}/panorama.png"
            entry["produced_panorama_hash"] = result.panorama_hash
            entry["ledger"] = result.ledger.report()
        except BusinessError as error:
            entry["recovered"] = False
            entry["triggered_code"] = error.code
            entry["error"] = _error_dict(error)
            entry["ledger"] = error.details.get("ledger", {})
        except Exception as error:  # Defensive: diagnostics must expose gaps.
            entry["recovered"] = False
            entry["triggered_code"] = None
            entry["unattributed_exception"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        results.append(entry)
    return results


def _run_resource_checks(
    out_root: Path,
    config: AppConfig,
    images: dict[int, np.ndarray],
    samples_root: Path,
) -> dict[str, Any]:
    low_memory = replace(config.resources, max_memory_bytes=1)
    try:
        from .resources import plan_resources

        plan_resources(
            parse_text(
                parameter_text(samples_root, "v3"), config.parse_defaults
            ).scene,
            low_memory,
            4,
        )
        memory_error = None
    except BusinessError as error:
        memory_error = _error_dict(error)
    parallel_limits = replace(
        config.resources, max_parallel_tasks=1, on_limit="degrade"
    )
    parallel_notice = plan_resources(
        parse_text(
            parameter_text(samples_root, "v3"), config.parse_defaults
        ).scene,
        parallel_limits,
        4,
    )
    cache_dir = out_root / "resource_cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    small_cache = ContentAddressedCache(cache_dir, 1)
    cache_render = render_text(
        parameter_text(samples_root, "v3"),
        images,
        config=config,
        cache=small_cache,
    )
    cache_degradations = [
        record
        for record in cache_render.ledger.report()["degradations"]
        if record["category"] == "cache_entry_evicted"
    ]
    return {
        "memory_limit": {
            "error": memory_error,
            "returns_resource_limit": (
                memory_error is not None
                and memory_error["code"] == ErrorCode.RESOURCE_LIMIT
            ),
        },
        "parallel_limit": {
            "requested": parallel_notice.requested_parallel_tasks,
            "effective": parallel_notice.effective_parallel_tasks,
            "notices": list(parallel_notice.notices),
        },
        "cache_limit": {
            "max_entries": 1,
            "degradations": cache_degradations,
            "degraded": bool(cache_degradations),
            "panorama_hash": cache_render.panorama_hash,
        },
    }


def _run_snapshot_checks(
    out_root: Path,
    parsed: dict[str, Any],
    renders: dict[str, RenderResult],
    artifacts: dict[str, dict[str, Any]],
    config: AppConfig,
) -> dict[str, Any]:
    root = out_root / "snapshots"
    if root.exists():
        shutil.rmtree(root)
    store = SnapshotStore(root)
    scene_payload = parameter_payload(parsed["v3"].scene.semantic_dict())
    first_commit = store.commit("parameters", scene_payload, version=1)
    reuse_commit = store.commit("parameters", scene_payload, version=1)
    replacement_payload = {
        **scene_payload,
        "replacement_note": "whole-payload replacement",
    }
    second_commit = store.commit("parameters", replacement_payload, version=2)
    replaced_snapshot = store.load("parameters")
    rollback_error: BusinessError | None = None
    try:
        store.commit("parameters", scene_payload, version=1)
    except BusinessError as error:
        rollback_error = error
    migration = migrate_scene(parsed["v3"], "v2", config.parse_defaults)
    store.commit("migration", migration_payload(migration.report_dict()), version=1)
    render_payload_value = render_payload(
        renders["v1"].report(), artifacts["v1"]["files"]
    )
    store.commit("render", render_payload_value, version=1)
    render_reuse = store.commit("render", render_payload_value, version=1)
    corruption_root = out_root / "snapshot_corruption"
    if corruption_root.exists():
        shutil.rmtree(corruption_root)
    corrupt_store = SnapshotStore(corruption_root)
    corrupt_store.commit("parameters", scene_payload, version=1)
    corrupt_path = corruption_root / "parameters.snapshot.json"
    corrupt_envelope = json.loads(corrupt_path.read_text(encoding="utf-8"))
    corrupt_envelope["payload"]["semantic_hash"] = "tampered"
    corrupt_path.write_text(
        json.dumps(corrupt_envelope, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    corrupt_error: BusinessError | None = None
    try:
        corrupt_store.load("parameters")
    except BusinessError as error:
        corrupt_error = error
    busy_root = out_root / "snapshot_busy"
    if busy_root.exists():
        shutil.rmtree(busy_root)
    busy_store = SnapshotStore(busy_root)
    busy_error: BusinessError | None = None
    busy_store._commit_lock.acquire()
    try:
        busy_store.commit("parameters", scene_payload, version=1)
    except BusinessError as error:
        busy_error = error
    finally:
        busy_store._commit_lock.release()
    return {
        "first_commit": {
            "version": first_commit.snapshot.version,
            "checksum": first_commit.snapshot.checksum,
            "reused": first_commit.reused,
            "replaced": first_commit.replaced,
        },
        "same_version_reuse": reuse_commit.reused
        and reuse_commit.snapshot.checksum == first_commit.snapshot.checksum,
        "replacement": {
            "version": second_commit.snapshot.version,
            "replaced": second_commit.replaced,
            "current_version": replaced_snapshot.version if replaced_snapshot else None,
            "current_checksum": replaced_snapshot.checksum if replaced_snapshot else None,
        },
        "rollback": _error_dict(rollback_error) if rollback_error else None,
        "rollback_detected": (
            rollback_error is not None
            and rollback_error.code == ErrorCode.SNAPSHOT_ROLLBACK
        ),
        "render_reuse": render_reuse.reused,
        "corruption": _error_dict(corrupt_error) if corrupt_error else None,
        "corruption_detected": (
            corrupt_error is not None
            and corrupt_error.code == ErrorCode.SNAPSHOT_CORRUPT
        ),
        "busy": _error_dict(busy_error) if busy_error else None,
        "busy_detected": (
            busy_error is not None and busy_error.code == ErrorCode.STAGE_BUSY
        ),
        "store_report": store.report(),
    }


def _coverage_check(result: RenderResult) -> dict[str, Any]:
    valid = np.asarray(result.valid_mask, dtype=bool)
    weight_positive = result.weight_sum > 0.0
    invalid_not_filled = not bool(
        np.any(result.panorama_rgb[~valid] != 0)
    ) if (~valid).any() else True
    every_valid_has_source = bool(
        np.all(result.weight_map[:, valid].sum(axis=0) > 0.0)
    ) if valid.any() else True
    return {
        "valid_equals_weight_positive": bool(np.array_equal(valid, weight_positive)),
        "invalid_not_filled": invalid_not_filled,
        "every_valid_pixel_has_source": every_valid_has_source,
        "valid_pixels": int(valid.sum()),
        "invalid_pixels": int((~valid).sum()),
    }


def _write_html(report: dict[str, Any], path: Path) -> None:
    data_json = json.dumps(report, ensure_ascii=False, allow_nan=False).replace(
        "</", "<\\/"
    )
    version_sections = []
    for version in VERSIONS:
        render = report["versions"][version]["render"]
        previews = render["previews"]
        params = html.escape(report["versions"][version]["parameter_text"])
        version_sections.append(
            f"""
            <section class="version-panel" data-version="{version}">
              <div class="row">
                <div class="media">
                  <h3>全景 {version}</h3>
                  <img src="{html.escape(previews['panorama'])}" alt="panorama {version}">
                </div>
                <div class="media">
                  <h3>有效掩码</h3>
                  <img src="{html.escape(previews['valid_mask'])}" alt="mask {version}">
                </div>
                <div class="media">
                  <h3>重叠权重</h3>
                  <img src="{html.escape(previews['weight_heatmap'])}" alt="weights {version}">
                </div>
              </div>
              <details open><summary>参数原文</summary><pre>{params}</pre></details>
              <pre class="json">{html.escape(json.dumps(render, ensure_ascii=False, indent=2))}</pre>
            </section>
            """
        )
    migration_cards = []
    for item in report["migrations"]:
        if "error" in item:
            migration_cards.append(
                f"<article class='card error'><b>{html.escape(item['source_version'])} → "
                f"{html.escape(item['target_version'])}</b><pre>"
                f"{html.escape(json.dumps(item['error'], ensure_ascii=False, indent=2))}</pre></article>"
            )
            continue
        rows = "".join(
            f"<tr><td>{html.escape(entry['action'])}</td>"
            f"<td>{html.escape(entry['field_path'])}</td>"
            f"<td>{html.escape(entry['source'])}</td>"
            f"<td>{html.escape(entry['reason'])}</td></tr>"
            for entry in item["items"]
        )
        dropped = ", ".join(item.get("dropped_fields", [])) or "无"
        migration_cards.append(
            f"<article class='card'><b>{html.escape(item['source_version'])} → "
            f"{html.escape(item['target_version'])}</b>"
            f"<p>补全/展平/丢弃项：{len(item['items'])}；丢弃字段：{html.escape(dropped)}</p>"
            f"<details><summary>展开逐项明细</summary><table><thead><tr>"
            f"<th>动作</th><th>字段</th><th>来源</th><th>原因</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></details></article>"
        )
    injection_cards = []
    for item in report["injections"]:
        ledger = item.get("ledger", {})
        injection_cards.append(
            f"<article class='card injection' data-stage='{html.escape(item['stage'])}'>"
            f"<b>{html.escape(item['stage'])} / {html.escape(item['kind'])}</b> "
            f"<span class='badge'>{html.escape(str(item.get('triggered_code')))}</span>"
            f"<p>恢复运行：{item.get('recovered')}</p>"
            + (
                f"<img class='small' src='{html.escape(item['preview'])}' alt='injection preview'>"
                if item.get("preview")
                else ""
            )
            + f"<pre class='json'>{html.escape(json.dumps(ledger, ensure_ascii=False, indent=2))}</pre>"
            f"</article>"
        )
    checks = report["checks"]
    check_rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td class='{'ok' if value else 'bad'}'>"
        f"{'PASS' if value else 'FAIL'}</td></tr>"
        for name, value in checks.items()
    )
    latest = report.get("latest_business_error_code") or "NONE"
    status = "PASS" if report["consistent"] else "E_INCONSISTENT"
    snippet = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PanoWeave 多视角拼接诊断</title>
<style>
:root{{--bg:#0b1020;--panel:#141b2d;--line:#2b3650;--text:#e8eefc;--muted:#93a4c6;--accent:#65d5b4;--bad:#ff7d8a}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(150deg,#080c18,#111a2c);color:var(--text);font:14px/1.5 ui-monospace,Consolas,monospace}}
header{{padding:28px 32px 18px;border-bottom:1px solid var(--line)}}h1{{margin:0 0 8px;font-size:25px}}
.badge{{display:inline-block;padding:3px 9px;border-radius:999px;background:#20304b;color:var(--accent)}}
.badge.bad{{background:#401e27;color:var(--bad)}}nav{{display:flex;gap:8px;flex-wrap:wrap;padding:14px 32px;position:sticky;top:0;background:#0b1020ee;border-bottom:1px solid var(--line);z-index:3}}
nav button,.filter{{background:#182239;color:var(--text);border:1px solid var(--line);padding:8px 12px;border-radius:8px;cursor:pointer}}
nav button.active{{border-color:var(--accent);color:var(--accent)}}main{{padding:20px 32px 60px;max-width:1500px;margin:auto}}
.tab{{display:none}}.tab.active{{display:block}}.row{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}}
.media,.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}}.media img{{width:100%;image-rendering:pixelated;background:#000;border-radius:8px}}
.card{{margin:10px 0}}.card.error{{border-color:var(--bad)}}img.small{{max-width:420px;width:100%;border-radius:8px}}
pre{{white-space:pre-wrap;overflow:auto;background:#080d18;border:1px solid var(--line);padding:12px;border-radius:8px;max-height:420px}}
table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid var(--line);padding:7px;text-align:left;vertical-align:top}}
.ok{{color:var(--accent)}}.bad{{color:var(--bad)}}details{{margin:8px 0}}summary{{cursor:pointer;color:var(--accent)}}
select{{background:#182239;color:var(--text);border:1px solid var(--line);padding:8px;border-radius:8px}}
@media(max-width:900px){{.row{{grid-template-columns:1fr}}main,header,nav{{padding-left:16px;padding-right:16px}}}}
</style>
</head>
<body>
<header>
  <h1>PanoWeave 多视角拼接诊断</h1>
  <div>一致性：<span class="badge {'bad' if not report['consistent'] else ''}">{html.escape(status)}</span>
  &nbsp; 最近业务错误码：<span class="badge">{html.escape(str(latest))}</span>
  &nbsp; 语义哈希：<span class="badge">{html.escape(report['semantic_hash'][:16])}</span></div>
</header>
<nav>
  <button class="active" data-tab="overview">总览</button>
  <button data-tab="v1">v1 视图</button>
  <button data-tab="v2">v2 视图</button>
  <button data-tab="v3">v3 视图</button>
  <button data-tab="migration">迁移裁决</button>
  <button data-tab="injection">注入与降级</button>
  <button data-tab="checks">自检与快照</button>
</nav>
<main>
<section class="tab active" id="tab-overview">
  <div class="row">
    <div class="media"><h3>全景（v1/v2/v3 同哈希）</h3><img src="{html.escape(report['versions']['v1']['render']['previews']['panorama'])}"></div>
    <div class="media"><h3>无效掩码</h3><img src="{html.escape(report['versions']['v1']['render']['previews']['valid_mask'])}"></div>
    <div class="media"><h3>重叠权重热度</h3><img src="{html.escape(report['versions']['v1']['render']['previews']['weight_heatmap'])}"></div>
  </div>
  <article class="card"><h3>三版本输出哈希</h3><pre>{html.escape(json.dumps(report['cross_version'], ensure_ascii=False, indent=2))}</pre></article>
  <article class="card"><h3>确定性复跑</h3><pre>{html.escape(json.dumps(report['determinism'], ensure_ascii=False, indent=2))}</pre></article>
  <article class="card"><h3>差异明细</h3><pre>{html.escape(json.dumps(report['differences'], ensure_ascii=False, indent=2))}</pre></article>
</section>
{"".join(f'<section class="tab" id="tab-{version}">{section}</section>' for version, section in zip(VERSIONS, version_sections))}
<section class="tab" id="tab-migration">
  <h2>双向迁移</h2>{"".join(migration_cards)}
  <article class="card"><h3>往返核对</h3><pre>{html.escape(json.dumps(report['roundtrips'], ensure_ascii=False, indent=2))}</pre></article>
  <article class="card"><h3>漂移与回退探针</h3><pre>{html.escape(json.dumps({'drift': report['drift_probe'], 'roundtrip_mismatch': report['mismatch_probe']}, ensure_ascii=False, indent=2))}</pre></article>
</section>
<section class="tab" id="tab-injection">
  <h2>四阶段异常注入</h2>
  <label>阶段过滤 <select id="stage-filter"><option value="all">全部</option><option value="parse">parse</option><option value="geometry">geometry</option><option value="gain">gain</option><option value="blend">blend</option></select></label>
  <div id="injection-list">{"".join(injection_cards)}</div>
</section>
<section class="tab" id="tab-checks">
  <h2>自检</h2><table><tbody>{check_rows}</tbody></table>
  <article class="card"><h3>资源上限</h3><pre>{html.escape(json.dumps(report['resources'], ensure_ascii=False, indent=2))}</pre></article>
  <article class="card"><h3>版本化快照</h3><pre>{html.escape(json.dumps(report['snapshots'], ensure_ascii=False, indent=2))}</pre></article>
</section>
</main>
<script id="report-data" type="application/json">{data_json}</script>
<script>
const tabs=[...document.querySelectorAll('nav button')];
const panels=[...document.querySelectorAll('.tab')];
function activate(name){{
  tabs.forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
  panels.forEach(p=>p.classList.toggle('active',p.id==='tab-'+name));
}}
tabs.forEach(button=>button.addEventListener('click',()=>activate(button.dataset.tab)));
document.getElementById('stage-filter').addEventListener('change',event=>{{
  document.querySelectorAll('.injection').forEach(card=>{{
    card.style.display=(event.target.value==='all'||card.dataset.stage===event.target.value)?'block':'none';
  }});
}});
</script>
</body>
</html>
"""
    path.write_text(snippet, encoding="utf-8", newline="\n")


def run_diagnostics(
    samples_dir: str | Path,
    output_dir: str | Path,
    config: AppConfig,
    *,
    clean_cache: bool = True,
    raise_on_inconsistent: bool = True,
) -> dict[str, Any]:
    samples_root = Path(samples_dir)
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(samples_root)
    images = load_sample_images(samples_root, manifest)
    parsed = {
        version: parse_text(
            parameter_text(samples_root, version), config.parse_defaults
        )
        for version in VERSIONS
    }
    equivalence = assert_semantic_equivalence(
        list(parsed.values()), config.parse_defaults
    )
    renders, artifacts, determinism = _render_all_versions(
        samples_root,
        out_root,
        config,
        parsed,
        images,
        clean_cache=clean_cache,
    )
    migrations, roundtrips = _run_migrations(parsed, config)
    drift_probe = _drift_probe(parsed, config, samples_root)
    mismatch_probe = _mismatch_probe(parsed, config)
    injections = _run_injections(
        samples_root, out_root, config, images, renders
    )
    resources = _run_resource_checks(
        out_root, config, images, samples_root
    )
    snapshots = _run_snapshot_checks(
        out_root, parsed, renders, artifacts, config
    )
    cross_version = {
        version: {
            "panorama_hash": renders[version].panorama_hash,
            "mask_hash": renders[version].mask_hash,
            "geometry_hash": renders[version].geometry_hash,
            "weight_hash": renders[version].weight_hash,
            "panorama_png_hash": artifacts[version]["files"]["panorama_png"],
            "mask_png_hash": artifacts[version]["files"]["valid_mask_png"],
        }
        for version in VERSIONS
    }
    cross_version_same = len(
        {
            (
                value["panorama_hash"],
                value["mask_hash"],
                value["geometry_hash"],
                value["weight_hash"],
            )
            for value in cross_version.values()
        }
    ) == 1
    coverage = {version: _coverage_check(renders[version]) for version in VERSIONS}
    coverage_ok = all(
        item["valid_equals_weight_positive"]
        and item["invalid_not_filled"]
        and item["every_valid_pixel_has_source"]
        for item in coverage.values()
    )
    roundtrip_ok = all("error" not in item for item in roundtrips)
    injection_attribution = all(
        item.get("ledger", {}).get("all_attributed", False)
        and item.get("ledger", {}).get("degradation_chain_traceable", False)
        for item in injections
    )
    checks = {
        "sample_manifest_integrity": _check_manifest(samples_root, manifest)["ok"],
        "semantic_equivalence": equivalence["equivalent"],
        "cross_version_byte_identity": cross_version_same,
        "deterministic_first_run_vs_rerun": determinism["byte_identical"],
        "roundtrip_representable_fields": roundtrip_ok,
        "semantic_drift_detected": drift_probe.get("triggered", False),
        "roundtrip_mismatch_detected": mismatch_probe.get("triggered", False),
        "injection_attribution": injection_attribution,
        "resource_memory_limit": resources["memory_limit"]["returns_resource_limit"],
        "resource_parallel_or_cache_degrade": bool(
            resources["parallel_limit"]["notices"]
            or resources["cache_limit"]["degraded"]
        ),
        "snapshot_same_version_reuse": snapshots["same_version_reuse"],
        "snapshot_rollback_detected": snapshots["rollback_detected"],
        "snapshot_corruption_detected": snapshots["corruption_detected"],
        "snapshot_concurrency_busy": snapshots["busy_detected"],
        "panorama_coverage_valid": coverage_ok,
    }
    differences = [name for name, value in checks.items() if not value]
    consistent = not differences
    latest_code = next(
        (
            item.get("triggered_code")
            for item in reversed(injections)
            if item.get("triggered_code")
        ),
        None,
    )
    report: dict[str, Any] = {
        "schema": 1,
        "samples": {
            "root": str(samples_root),
            "seed": manifest.get("seed"),
            "semantic_hash": manifest.get("semantic_hash"),
            "manifest_integrity": _check_manifest(samples_root, manifest),
        },
        "semantic_hash": parsed["v1"].scene.semantic_hash,
        "semantic_equivalence": equivalence,
        "versions": {
            version: {
                "parameter_text": parameter_text(samples_root, version),
                "completion_count": len(parsed[version].completions),
                "render": _render_report(
                    renders[version], f"assets/{version}"
                ),
            }
            for version in VERSIONS
        },
        "cross_version": cross_version,
        "determinism": determinism,
        "migrations": migrations,
        "roundtrips": roundtrips,
        "drift_probe": drift_probe,
        "mismatch_probe": mismatch_probe,
        "injections": injections,
        "resources": resources,
        "snapshots": snapshots,
        "coverage": coverage,
        "checks": checks,
        "consistent": consistent,
        "differences": differences,
        "latest_business_error_code": latest_code,
    }
    if not consistent:
        report["error"] = {
            "code": ErrorCode.INCONSISTENT,
            "message": "diagnostic self-check found inconsistencies",
            "differences": differences,
        }
    report_path = out_root / "diagnostic_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    html_path = out_root / "diagnostic.html"
    _write_html(report, html_path)
    if not consistent and raise_on_inconsistent:
        raise BusinessError(
            ErrorCode.INCONSISTENT,
            "diagnostic self-check found inconsistencies",
            stage="diagnostics",
            field_path=differences[0] if differences else None,
            details={
                "differences": differences,
                "report_path": str(report_path),
                "html_path": str(html_path),
            },
        )
    return report

