"""Flask HTTP 服务：展陈动画工作台接口。

接口：
  GET  /health              健康检查
  POST /api/parse           仅解析脚本，返回结构化动作对象
  POST /api/animate         完整流水线，返回关键帧片段/摘要/核对报告

服务保持无状态：每次请求独立计算，不共享任何会话数据。
"""

from flask import Flask, jsonify, request

from museum_anim.parser import UnparsedEntry, parse_script
from museum_anim.pipeline import PipelineError, UnparsedAction, run_pipeline


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "museum-animation-workbench"})

    @app.post("/api/parse")
    def parse_only():
        payload = request.get_json(silent=True)
        if not payload or "script" not in payload:
            return jsonify({
                "status": "ERROR", "code": "E_INPUT",
                "message": "请求体需包含 script 数组",
            }), 400
        try:
            parsed = parse_script(payload["script"])
        except UnparsedEntry as exc:
            return jsonify({
                "status": "ERROR", "code": "E_UNPARSED",
                "entry_index": exc.index, "entry_text": exc.text,
                "reason": exc.reason,
            }), 422
        return jsonify({"status": "OK", "parsed_actions": parsed})

    @app.post("/api/animate")
    def animate():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({
                "status": "ERROR", "code": "E_INPUT",
                "message": "请求体必须是 JSON 对象",
            }), 400
        try:
            result = run_pipeline(payload)
        except UnparsedAction as exc:
            return jsonify({
                "status": "ERROR", "code": exc.code,
                "entry_index": exc.index, "entry_text": exc.text,
                "reason": exc.reason,
            }), 422
        except (PipelineError, ValueError, KeyError) as exc:
            return jsonify({
                "status": "ERROR", "code": "E_INPUT", "message": str(exc),
            }), 400
        return jsonify(result)

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"status": "ERROR", "code": "E_NOT_FOUND"}), 404

    @app.errorhandler(500)
    def internal_error(_error):
        return jsonify({"status": "ERROR", "code": "E_INTERNAL"}), 500

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
