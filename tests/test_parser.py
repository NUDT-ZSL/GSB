import pytest

from app.crawler.parser import (ParseFailure, normalize, parse_detail,
                                parse_list_meta)

V1_HTML = """<html><head><meta name="schemaVersion" content="1"></head><body>
<script type="application/json" id="detail-data">
{"id":"item-001","title":"t","category":"c","price":101,"stock":1}
</script></body></html>"""

V2_HTML = """<html><head><meta name="schemaVersion" content="2"></head><body>
<script type="application/json" id="detail-data">
{"id":"item-003","name":"t3","category":{"id":3,"label":"类目-3"},
 "pricing":{"amount":103,"currency":"CNY"},"tags":["a","b"],"stock":3}
</script></body></html>"""

UNKNOWN_HTML = """<html><head><meta name="schemaVersion" content="99"></head><body>
<script type="application/json" id="detail-data">
{"uid":"item-024","headline":"h","cost_fen":12400}
</script></body></html>"""


def test_parse_v1_maps_to_shared_fields():
    result = parse_detail(V1_HTML)
    assert not result.unresolved
    assert result.schema_version == 1
    assert result.normalized == {
        "item_id": "item-001", "title": "t", "category": "c",
        "price": 101, "currency": "CNY", "stock": 1, "tags": [],
    }
    assert result.evidence["parser"] == "detail_v1"
    assert result.evidence["schema_version"] == 1
    assert "title" in result.evidence["source_fields"]


def test_parse_v2_maps_to_shared_fields():
    result = parse_detail(V2_HTML)
    assert result.normalized["title"] == "t3"
    assert result.normalized["category"] == "类目-3"
    assert result.normalized["price"] == 103
    assert result.normalized["tags"] == ["a", "b"]
    assert result.evidence["parser"] == "detail_v2"


def test_unknown_version_keeps_raw_and_marks_unresolved():
    result = parse_detail(UNKNOWN_HTML)
    assert result.unresolved
    assert result.normalized["_raw"] == {"uid": "item-024", "headline": "h", "cost_fen": 12400}
    assert result.evidence["parser"] == "unresolved"


def test_missing_declaration_raises_parse_failure():
    with pytest.raises(ParseFailure):
        parse_detail("<html><body>nothing</body></html>")


def test_normalize_rejects_nothing_for_known_versions():
    normalized, name = normalize(1, {"id": "x", "title": "t", "category": "c",
                                     "price": 1, "stock": 0})
    assert name == "detail_v1" and normalized["currency"] == "CNY"


def test_parse_list_meta():
    html = ('<div id="items" data-render-src="/render/list?page=2" '
            'data-total-pages="3" data-next-page="3"></div>')
    meta = parse_list_meta(html)
    assert meta == {"render_src": "/render/list?page=2", "total_pages": 3, "next_page": 3}
    last = parse_list_meta('<div id="items" data-render-src="/r" '
                           'data-total-pages="3" data-next-page=""></div>')
    assert last["next_page"] is None
