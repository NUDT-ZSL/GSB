"""Flask demo service: extraction, checks, versions and diffs."""

import json

from flask import Flask, jsonify, request

from . import pipeline
from .db import Store
from .errors import WorkbenchError
from .generator import (asset_summary, get_asset, load_project,
                        save_project)


def create_app(project_path, db_path):
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    state = {
        "project_path": project_path,
        "project": load_project(project_path),
        "store": Store(db_path),
    }

    @app.errorhandler(WorkbenchError)
    def _wb_error(err):
        return jsonify(err.to_dict()), 400

    @app.get("/api/project")
    def project_info():
        p = state["project"]
        return jsonify({
            "seed": p["seed"],
            "assets": [asset_summary(a) for a in p["assets"]],
            "board_layout": p["board_layout"],
        })

    @app.get("/api/assets")
    def assets():
        return jsonify({
            "assets": [asset_summary(a) for a in state["project"]["assets"]]
        })

    @app.post("/api/assets/<name>/extract")
    def extract(name):
        asset = get_asset(state["project"], name)
        report, meshes = pipeline.run_asset(state["project"], asset)
        version = state["store"].save_version(
            name, asset["constraints"], report, meshes)
        return jsonify({"asset": name, "version": version, "report": report})

    @app.get("/api/assets/<name>/versions")
    def versions(name):
        get_asset(state["project"], name)
        return jsonify({"asset": name,
                        "versions": state["store"].list_versions(name)})

    @app.get("/api/assets/<name>/versions/<int:version>")
    def version_detail(name, version):
        get_asset(state["project"], name)
        return jsonify(state["store"].get_version(name, version))

    @app.get("/api/assets/<name>/diff")
    def diff(name):
        get_asset(state["project"], name)
        a = int(request.args.get("a", "1"))
        b = int(request.args.get("b", str(a + 1)))
        return jsonify(state["store"].diff_versions(name, a, b))

    @app.post("/api/assets/<name>/params")
    def update_params(name):
        """Modify constraint parameters (and optionally primitive params).

        Body: {"constraints": {...}, "primitives": [{"path": [...],
        "params": {...}}]}.  The project file is updated; nothing is
        extracted or versioned until /extract is called, so invalid
        input can never produce half-built meshes or touch history.
        """
        asset = get_asset(state["project"], name)
        body = request.get_json(force=True, silent=False)
        from .errors import validate_constraints
        candidate = json.loads(json.dumps(asset))
        if "constraints" in body:
            candidate["constraints"].update(body["constraints"])
        validate_constraints(candidate)
        for edit in body.get("primitives", []):
            node = candidate["tree"]
            for step in edit.get("path", []):
                node = node["children"][int(step)]
            if node.get("op") != "primitive":
                raise WorkbenchError(
                    "E_INVALID_PRIMITIVE",
                    "path does not locate a primitive node",
                    asset=name, node_path=str(edit.get("path")))
            node["primitive"]["params"].update(edit.get("params", {}))
            for key, val in edit.get("transform", {}).items():
                if key in ("translate", "rotate", "scale"):
                    node["transform"][key] = val
        asset.clear()
        asset.update(candidate)
        save_project(state["project"], state["project_path"])
        return jsonify({"asset": name, "updated": True,
                        "constraints": asset["constraints"]})

    return app
