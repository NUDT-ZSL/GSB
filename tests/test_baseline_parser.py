from __future__ import annotations

from pathlib import Path

from config_forensics.baseline import parse_baseline_file
from config_forensics.codes import Code
from tests.helpers import write_baseline


def test_baseline_parser_continues_and_honors_interval_semantics(tmp_path: Path) -> None:
    path = write_baseline(
        tmp_path / "baseline.json",
        [
            {
                "host_id": "host-a",
                "path": "quota.limit",
                "source": "source-a",
                "effective_from_seq": 2,
                "effective_to_seq": 5,
                "ranges": [{"interval": "(100,200]", "source": "strict"}],
            },
            {
                "host_id": "host-a",
                "path": "broken.range",
                "ranges": [{"min": 10, "max": 1}],
            },
            {
                "host_id": "host-a",
                "path": "broken.interval",
                "effective_from_seq": 4,
                "effective_to_seq": 2,
                "ranges": [{"interval": "[0,1]"}],
            },
            {
                "host_id": "host-b",
                "path": "allowed.value",
                "effective_from_seq": 1,
                "ranges": [{"allowed_values": ["yes"]}],
            },
        ],
    )
    result = parse_baseline_file(path)
    assert len(result.rules) == 2
    codes = {issue.code for issue in result.issues}
    assert Code.BASELINE_INVALID_RANGE in codes
    assert Code.BASELINE_INVALID_INTERVAL in codes

    quota = next(rule for rule in result.rules if rule.path == "quota.limit")
    assert not quota.active_at(1)
    assert quota.active_at(2)
    assert quota.active_at(5)
    assert not quota.active_at(6)
    assert not quota.permitted(100)
    assert quota.permitted(101)
    assert quota.permitted(200)
    assert not quota.permitted(201)

    exact = next(rule for rule in result.rules if rule.path == "allowed.value")
    assert exact.permitted("yes")
    assert not exact.permitted("no")
