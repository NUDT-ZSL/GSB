from pipeline.extractor import extract, parse_declaration
from site_server import render_pages, sample_data


def _html(source, n, version=None):
    biz = sample_data.biz_id(n)
    version = version if version is not None else sample_data.default_version(source, n)
    return render_pages.render_detail(source, version, sample_data.product(biz))


def test_alpha_v1_fields():
    result = extract(_html("alpha", 1))
    assert result.status == "ok"
    assert (result.source, result.version) == ("alpha", 1)
    fields = result.fields
    assert fields["business_id"] == "BIZ-001"
    assert fields["name"] == "Gadget-001"
    assert fields["price"] == 87.5
    assert fields["currency"] == "CNY"
    assert fields["category"] == "cat-1"
    assert fields["stock"] == 6
    assert "color" not in fields


def test_alpha_v2_adds_color():
    result = extract(_html("alpha", 4))
    assert result.status == "ok" and result.version == 2
    assert result.fields["color"] == "black"
    assert result.fields["currency"] == "CNY"


def test_beta_v1_fields():
    result = extract(_html("beta", 2))
    assert result.status == "ok" and result.version == 1
    assert result.fields["currency"] == "USD"
    assert result.fields["price"] == 12.5
    assert "color" not in result.fields


def test_beta_v2_fields():
    result = extract(_html("beta", 3))
    assert result.status == "ok" and result.version == 2
    assert result.fields["color"] == "green"
    assert result.fields["stock"] == 8


def test_unknown_version_is_unresolved_not_guessed():
    result = extract(_html("beta", 6))
    assert result.status == "unresolved"
    assert result.reason == "unknown_schema_version"
    assert result.raw["declared_version"] == 9
    assert result.fields is None


def test_missing_declaration_is_unresolved():
    html = "<html><body><div>no meta here</div></body></html>"
    result = extract(html)
    assert result.status == "unresolved"
    assert result.reason == "missing_declaration"
    assert result.raw["declared_source"] is None


def test_parse_declaration_roundtrip():
    source, version = parse_declaration(_html("alpha", 5))
    assert (source, version) == ("alpha", 2)
