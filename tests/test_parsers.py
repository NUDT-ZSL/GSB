from app.mock_site import TICKETS, _detail_html
from app.parsers import parse_detail


def test_parse_v1():
    t = next(t for t in TICKETS if t["version"] == "v1")
    r = parse_detail(_detail_html(t), t["ticket_id"])
    assert r.version == "v1" and r.parse_status == "ok"
    assert r.normalized["ticket_id"] == t["ticket_id"]
    assert r.normalized["assignee"] == t["requester"]


def test_parse_v2():
    t = next(t for t in TICKETS if t["version"] == "v2")
    r = parse_detail(_detail_html(t), t["ticket_id"])
    assert r.version == "v2" and r.parse_status == "ok"
    assert r.normalized["assignee"] == t["assignee"]
    assert r.normalized["department"] == t["department"]
    assert r.normalized["due_date"] == t["due_date"]


def test_unknown_version_falls_back_without_data_loss():
    t = next(t for t in TICKETS if t["version"] == "v9")
    r = parse_detail(_detail_html(t), t["ticket_id"])
    assert r.version == "v9" and r.parse_status == "raw_fallback"
    assert r.normalized is None
    assert "_html_excerpt" in r.raw and t["title"] in r.raw["_html_excerpt"]
