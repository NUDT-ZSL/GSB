from __future__ import annotations

from meeting_vad.app import _run_command, render_status


def test_status_renders_all_nodes(session) -> None:
    result = session.analyze()
    status = render_status(session)
    assert f"snapshot_version={result.snapshot_version}" in status
    for node in ("envelope", "vad", "refine", "speaker", "export"):
        assert node in status
    assert "self_check_ok=True" in status
    assert "last_error=-" in status


def test_interactive_business_error_is_shown_and_retained(session, capsys) -> None:
    session.analyze()
    assert _run_command(session, "edit missing-segment S01") is True
    captured = capsys.readouterr().out
    assert "E_SEGMENT_UNKNOWN" in captured
    assert session.last_error["code"] == "E_SEGMENT_UNKNOWN"
    assert _run_command(session, "last") is True
    assert "E_SEGMENT_UNKNOWN" in capsys.readouterr().out
