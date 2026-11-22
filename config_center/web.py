from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from .errors import BusinessError, bad_config, not_found, not_initialized
from .service import ConfigCenter, parse_json_bytes
from .util import read_json, to_jsonable
from .verify import verify_center


def create_app(
    root: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = True
    app.json.ensure_ascii = False
    center = ConfigCenter(root, data_dir=data_dir)
    app.extensions["config_center"] = center

    def _json_body() -> Any:
        return parse_json_bytes(request.get_data())

    def _namespace() -> str | None:
        value = request.args.get("namespace")
        if value:
            return value
        payload = None
        if request.method in {"POST", "PUT", "PATCH"}:
            try:
                payload = _json_body()
            except BusinessError:
                payload = None
        if isinstance(payload, dict) and isinstance(payload.get("namespace"), str):
            return payload["namespace"]
        pointers = center.store.list_pointers()
        if len(pointers) == 1:
            return pointers[0]["namespace"]
        return None

    @app.errorhandler(BusinessError)
    def handle_business_error(exc: BusinessError):
        return jsonify(exc.to_dict()), exc.http_status

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        app.logger.exception("unexpected error")
        return (
            jsonify(
                BusinessError(
                    "E_INTERNAL",
                    "服务内部错误",
                    details={"exception": type(exc).__name__, "message": str(exc)},
                    http_status=500,
                ).to_dict()
            ),
            500,
        )

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "service": "config-center", "version": 1})

    @app.get("/api/overview")
    def overview():
        namespace = _namespace()
        pointers = center.store.list_pointers()
        if namespace is None and len(pointers) == 1:
            namespace = pointers[0]["namespace"]
        if namespace is None:
            return jsonify(
                {
                    "ok": True,
                    "initialized": bool(pointers),
                    "namespace": None,
                    "namespaces": [item["namespace"] for item in pointers],
                    "configs": [],
                    "versions": [],
                    "status": center.status(),
                }
            )
        pointer = next(
            (item for item in pointers if item["namespace"] == namespace),
            None,
        )
        status = center.status(namespace)
        configs = (
            center.store.list_configs(namespace, int(pointer["version"]))
            if pointer is not None
            else []
        )
        return jsonify(
            {
                "ok": True,
                "initialized": pointer is not None,
                "namespace": namespace,
                "pointer": pointer,
                "configs": configs,
                "versions": status["versions"],
                "status": status,
                "samples": {
                    "snapshot": "data/snapshot.json",
                    "patches": [
                        "data/patches/002_add_weight_gamma.json",
                        "data/patches/003_full_replace_same_values.json",
                    ],
                    "compute_input": "data/compute_input.json",
                    "clock_steps": "data/clock_steps.json",
                },
            }
        )

    @app.get("/api/status")
    def status():
        return jsonify(center.status(_namespace()))

    @app.get("/api/versions")
    def versions():
        namespace = _namespace()
        return jsonify(
            {
                "ok": True,
                "namespace": namespace,
                "versions": center.store.list_versions(namespace),
            }
        )

    @app.get("/api/version/<int:version>")
    def version_detail(version: int):
        namespace = _namespace()
        if namespace is None:
            raise not_initialized()
        bundle = center.store.read_version_bundle(namespace, version)
        if bundle is None:
            raise not_found(
                "版本不存在", namespace=namespace, version=version
            )
        return jsonify(
            {
                "ok": True,
                "snapshot": bundle.to_dict(),
                "configs": center.store.list_configs(namespace, version),
            }
        )

    @app.get("/api/result")
    def result_detail():
        namespace = _namespace()
        client_id = request.args.get("client_id")
        round_key = request.args.get("round_key")
        if not namespace or not client_id or not round_key:
            raise bad_config("需要 namespace、client_id 与 round_key")
        row = center.store.read_result_full(namespace, client_id, round_key)
        if row is None:
            raise not_found(
                "客户端结果不存在",
                namespace=namespace,
                client_id=client_id,
                round_key=round_key,
            )
        return jsonify({"ok": True, **row})

    @app.post("/api/load")
    def load_snapshot():
        return jsonify(center.load_snapshot_document(_json_body(), source="http"))

    @app.post("/api/apply")
    def apply_patch():
        return jsonify(center.apply_patch_document(_json_body(), source="http"))

    @app.post("/api/digest")
    def patch_digest():
        return jsonify(center.patch_digest(_json_body()))

    @app.post("/api/compute")
    def compute():
        payload = _json_body()
        if not isinstance(payload, dict):
            raise bad_config("计算请求必须是 JSON 对象")
        return jsonify(
            center.compute_document(
                payload,
                client_id=payload.get("client_id"),
                round_key=payload.get("round_key"),
                namespace=payload.get("namespace"),
            )
        )

    @app.post("/api/resume")
    def resume():
        payload: dict[str, Any] = {}
        if request.get_data():
            raw = _json_body()
            if isinstance(raw, dict):
                payload = raw
        return jsonify(
            center.resume(
                namespace=payload.get("namespace"),
                client_id=payload.get("client_id"),
                round_key=payload.get("round_key"),
            )
        )

    @app.post("/api/compare")
    def compare():
        payload = _json_body()
        if not isinstance(payload, dict):
            raise bad_config("比较请求必须是 JSON 对象")
        namespace = payload.get("namespace") or _namespace()
        if not namespace:
            raise not_initialized()
        from_version = payload.get("from_version")
        to_version = payload.get("to_version")
        if not isinstance(from_version, int) or not isinstance(to_version, int):
            raise bad_config("from_version/to_version 必须是整数")
        tolerance = _decimal_or_none(payload.get("tolerance"))
        scale_factor = payload.get("scale_factor", 100)
        if not isinstance(scale_factor, int):
            raise bad_config("scale_factor 必须是整数")
        return jsonify(
            center.compare_versions(
                namespace,
                from_version,
                to_version,
                payload.get("input") or _sample_compute_input(center),
                tolerance=tolerance,
                scale_factor=scale_factor,
            )
        )

    @app.post("/api/compare-clients")
    def compare_clients():
        payload = _json_body()
        if not isinstance(payload, dict):
            raise bad_config("比较请求必须是 JSON 对象")
        namespace = payload.get("namespace") or _namespace()
        if not namespace:
            raise not_initialized()
        required = [
            "left_client",
            "left_round",
            "right_client",
            "right_round",
        ]
        missing = [item for item in required if not payload.get(item)]
        if missing:
            raise bad_config("客户端比较参数不完整", missing=missing)
        tolerance = _decimal_or_none(payload.get("tolerance"))
        scale_factor = payload.get("scale_factor", 100)
        if not isinstance(scale_factor, int):
            raise bad_config("scale_factor 必须是整数")
        return jsonify(
            center.compare_clients(
                namespace,
                payload["left_client"],
                payload["left_round"],
                payload["right_client"],
                payload["right_round"],
                tolerance=tolerance,
                scale_factor=scale_factor,
            )
        )

    @app.get("/api/verify")
    def verify():
        namespace = _namespace()
        tolerance = _decimal_or_none(request.args.get("tolerance")) or Decimal(
            "0.001"
        )
        try:
            scale_factor = int(request.args.get("scale_factor", "100"))
        except ValueError as exc:
            raise bad_config("scale_factor 必须是整数") from exc
        return jsonify(
            verify_center(
                center,
                namespace=namespace,
                tolerance=tolerance,
                scale_factor=scale_factor,
            )
        )

    @app.get("/api/samples")
    def samples():
        files = {
            "snapshot": center.data_dir / "snapshot.json",
            "patch_002": center.data_dir
            / "patches"
            / "002_add_weight_gamma.json",
            "patch_003": center.data_dir
            / "patches"
            / "003_full_replace_same_values.json",
            "compute_input": center.data_dir / "compute_input.json",
            "clock_steps": center.data_dir / "clock_steps.json",
        }
        payload: dict[str, Any] = {"ok": True}
        for key, path in files.items():
            payload[key] = read_json(path) if path.exists() else None
        payload["error_cases"] = [
            {
                "name": "bad_clock",
                "path": "data/error_cases/bad_clock.json",
                "expected_code": "E_BAD_CLOCK",
            },
            {
                "name": "bad_type",
                "path": "data/error_cases/bad_type.json",
                "expected_code": "E_BAD_CONFIG",
            },
            {
                "name": "bad_digest",
                "path": "data/error_cases/bad_digest_patch.json",
                "expected_code": "E_DIGEST_MISMATCH",
            },
        ]
        return jsonify(to_jsonable(payload))

    @app.post("/api/bootstrap")
    def bootstrap():
        raw: dict[str, Any] = {}
        if request.get_data():
            payload = _json_body()
            if isinstance(payload, dict):
                raw = payload
        if raw and raw.get("confirm") not in {None, "BOOTSTRAP"}:
            raise bad_config("bootstrap 确认短语无效")
        result = center.bootstrap_demo(
            include_patches=bool(raw.get("include_patches", True))
        )
        return jsonify(result)

    @app.post("/api/reset")
    def reset():
        payload = _json_body()
        if not isinstance(payload, dict) or payload.get("confirm") != "RESET":
            raise bad_config("reset 需要 confirm=RESET")
        return jsonify(center.reset())

    return app


def _sample_compute_input(center: ConfigCenter) -> Any:
    path = center.data_dir / "compute_input.json"
    if not path.exists():
        raise not_found("默认计算输入不存在", path=str(path))
    return read_json(path)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise bad_config("tolerance 必须是十进制数") from exc
    if not result.is_finite() or result < 0:
        raise bad_config("tolerance 必须是非负十进制数")
    return result

