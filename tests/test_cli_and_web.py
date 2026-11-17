import json

from injection_workbench.cli import main
from injection_workbench.samples import generate_all


def test_cli_generate_and_run_exit_codes(tmp_dirs, capsys):
    samples, reports, checkpoints = tmp_dirs
    rc_gen = main(["generate", "--samples-dir", str(samples)])
    assert rc_gen == 0
    assert (samples / "manifest_small.json").exists()

    rc_run = main([
        "run", str(samples / "manifest_small.json"),
        "--report", str(reports / "report.json"),
        "--checkpoint-dir", str(checkpoints),
        "--no-checkpoint",
    ])
    assert rc_run == 0
    report = json.loads((reports / "report.json").read_text(encoding="utf-8"))
    assert report["manifests"][0]["file_id"] == "f-small"


def test_cli_partial_failure_exit_code_one(tmp_dirs):
    samples, reports, checkpoints = tmp_dirs
    generate_all(samples)
    rc = main([
        "run", str(samples / "manifest_large.json"),
        "--report", str(reports / "r.json"),
        "--checkpoint-dir", str(checkpoints),
        "--deadline", "0.0005",
    ])
    assert rc == 1


def test_cli_bad_arguments_exit_code_two(tmp_path):
    assert main(["run", str(tmp_path / "missing.json")]) == 2
    assert main(["run", "--force-mode", "bogus", str(tmp_path)]) == 2


def test_web_app_renders_dashboard_and_detail(tmp_dirs):
    samples, reports, checkpoints = tmp_dirs
    generate_all(samples)
    from injection_workbench.webapp import create_app
    app = create_app(
        samples_dir=str(samples),
        report_path=str(reports / "web-report.json"),
        checkpoint_dir=str(checkpoints),
    )
    client = app.test_client()
    index = client.get("/")
    assert index.status_code == 200
    assert "注入类漏洞静态检测工作台".encode() in index.data

    api = client.get("/api/report")
    assert api.status_code == 200
    payload = api.get_json()
    position = next(
        i for i, item in enumerate(payload["manifests"]) if item["file_id"] == "f-small"
    )
    detail = client.get(f"/manifest/{position}")
    assert detail.status_code == 200
    assert "E_ORDER".encode() in detail.data
