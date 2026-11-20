#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kdrift.simulator import simulate_actual  # noqa: E402


def deployment(
    namespace: str,
    name: str,
    app: str,
    replicas: int = 2,
    image: str = "registry.local/example/app:1.0.0",
    refs: list[str] | None = None,
    labels: dict[str, str] | None = None,
    port: int = 8080,
) -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "namespace": namespace,
        "name": name,
        "replicas": replicas,
        "image": image,
        "labels": labels
        if labels is not None
        else {"app": app, "tier": "backend", "team": namespace},
        "selector": {"matchLabels": {"app": app}},
        "configMapRefs": refs or [],
        "ports": [
            {"name": "http", "port": port, "protocol": "TCP"}
        ],
    }


def service(
    namespace: str,
    name: str,
    app: str,
    target_port: int = 8080,
) -> dict[str, Any]:
    return {
        "kind": "Service",
        "namespace": namespace,
        "name": name,
        "labels": {"app": app, "tier": "frontend"},
        "selector": {"matchLabels": {"app": app}},
        "ports": [
            {
                "name": "http",
                "port": 80,
                "targetPort": target_port,
                "protocol": "TCP",
            }
        ],
    }


def config_map(
    namespace: str,
    name: str,
    app: str,
    *,
    scale: bool = False,
) -> dict[str, Any]:
    data = {
        "LOG_LEVEL": "info",
        "TIMEOUT_MS": "5000",
        "FEATURE_FLAG": "enabled",
    }
    if scale:
        data.update(
            {
                f"KEY_{index:02d}": f"value-{index:02d}"
                for index in range(1, 9)
            }
        )
    return {
        "kind": "ConfigMap",
        "namespace": namespace,
        "name": name,
        "labels": {"app": app, "managed-by": "sample-simulator"},
        "data": data,
    }


def small_expected() -> dict[str, Any]:
    objects = [
        deployment(
            "shop",
            "api",
            "api",
            replicas=2,
            image="registry.local/shop/api:1.4.0",
            refs=["api-config"],
        ),
        config_map("shop", "api-config", "api"),
        service("shop", "api-svc", "api"),
        deployment(
            "shop",
            "cache",
            "cache",
            replicas=3,
            image="registry.local/shop/cache:2.1.0",
            refs=["cache-config"],
        ),
        config_map("shop", "cache-config", "cache"),
        service("shop", "cache-svc", "cache"),
        deployment(
            "ops",
            "worker",
            "worker",
            replicas=1,
            image="registry.local/ops/worker:3.0.0",
        ),
    ]
    return {
        "apiVersion": "drift.local/v1",
        "kind": "ExpectedResourceList",
        "scenario": "small-mixed-drift",
        "objects": sorted(
            objects,
            key=lambda item: (
                item["namespace"],
                {"ConfigMap": 0, "Deployment": 1, "Service": 2}[
                    item["kind"]
                ],
                item["name"],
            ),
        ),
    }


def small_actual() -> dict[str, Any]:
    legacy_config = config_map("shop", "legacy-config", "legacy")
    legacy_deployment = deployment(
        "shop",
        "legacy",
        "legacy",
        replicas=1,
        image="registry.local/legacy/app:0.9.0",
        refs=["legacy-config"],
    )
    legacy_service = service("shop", "legacy-svc", "legacy", 9000)
    return simulate_actual(
        small_expected(),
        scenario="small-mixed-drift",
        remove=[
            "Deployment/shop/cache",
            "ConfigMap/shop/cache-config",
            "Service/shop/cache-svc",
            "Deployment/ops/worker",
        ],
        add=[legacy_config, legacy_deployment, legacy_service],
        patch=[
            {
                "identity": "Deployment/shop/api",
                "set": {
                    "replicas": 1,
                    "image": "registry.local/shop/api:1.3.0",
                },
            },
            {
                "identity": "Service/shop/api-svc",
                "set": {
                    "selector": {"matchLabels": {"app": "api-v2"}}
                },
            },
        ],
    )


def scale_expected() -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for index in range(14):
        app = f"app-{index:02d}"
        objects.append(config_map("scale-a", f"cm-{index:02d}", app, scale=True))
        objects.append(
            deployment(
                "scale-a",
                app,
                app,
                replicas=2 + (index % 3),
                image=f"registry.local/scale/{app}:1.0.0",
                refs=[f"cm-{index:02d}"],
                labels={
                    "app": app,
                    "tier": "backend",
                    "team": "scale",
                    "zone": f"z{index % 3}",
                    "release": "v1",
                },
            )
        )
        if index % 3 == 0:
            objects.append(
                service("scale-a", f"svc-{index:02d}", app)
            )
    for index in range(4):
        app = f"batch-{index:02d}"
        objects.append(config_map("scale-b", f"cm-{index:02d}", app))
        objects.append(
            deployment("scale-b", app, app, refs=[f"cm-{index:02d}"])
        )
    for index in range(2):
        app = f"batch-{index:02d}"
        objects.append(service("scale-b", f"svc-{index:02d}", app))
    return {
        "apiVersion": "drift.local/v1",
        "kind": "ExpectedResourceList",
        "scenario": "scale-threshold-crossing",
        "objects": sorted(
            objects,
            key=lambda item: (
                item["namespace"],
                {"ConfigMap": 0, "Deployment": 1, "Service": 2}[
                    item["kind"]
                ],
                item["name"],
            ),
        ),
    }


def scale_actual() -> dict[str, Any]:
    extra_config = config_map("scale-a", "extra-config", "extra", scale=True)
    extra_deployment = deployment(
        "scale-a",
        "extra-app",
        "extra",
        image="registry.local/scale/extra:1.0.0",
        refs=["extra-config"],
    )
    extra_service = service("scale-a", "extra-svc", "extra")
    orphan = config_map("scale-a", "orphan-config", "orphan")
    return simulate_actual(
        scale_expected(),
        scenario="scale-threshold-crossing",
        remove=[
            "Deployment/scale-a/app-12",
            "ConfigMap/scale-a/cm-12",
            "Service/scale-a/svc-12",
        ],
        add=[extra_config, extra_deployment, extra_service, orphan],
        patch=[
            {
                "identity": "Deployment/scale-a/app-02",
                "set": {
                    "replicas": 7,
                    "image": "registry.local/scale/app-02:2.0.0",
                },
            },
            {
                "identity": "Service/scale-a/svc-06",
                "set": {
                    "selector": {"matchLabels": {"app": "wrong-target"}}
                },
            },
        ],
    )


SMALL_CONFIG = {
    "max_concurrency": 3,
    "operation_timeout_ms": 100,
    "exact_object_threshold": 12,
    "exact_field_threshold": 12,
    "sampling_ratio": 0.5,
    "tolerance": 0,
    "logical_step_ms": 25,
    "initial_logical_ms": 1000,
    "retry_limit": 1,
    "retry_backoff_ms": 10,
    "equivalence_audit": True,
    "strata_count": 12,
}

SCALE_CONFIG = {
    "max_concurrency": 4,
    "operation_timeout_ms": 100,
    "exact_object_threshold": 10,
    "exact_field_threshold": 14,
    "sampling_ratio": 0.5,
    "tolerance": 0,
    "logical_step_ms": 25,
    "initial_logical_ms": 5000,
    "retry_limit": 1,
    "retry_backoff_ms": 10,
    "equivalence_audit": True,
    "strata_count": 12,
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    samples = ROOT / "samples"
    write_json(samples / "expected.json", small_expected())
    write_json(samples / "actual.json", small_actual())
    write_json(samples / "config.json", SMALL_CONFIG)
    write_json(samples / "scale" / "expected.json", scale_expected())
    write_json(samples / "scale" / "actual.json", scale_actual())
    write_json(samples / "scale" / "config.json", SCALE_CONFIG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

