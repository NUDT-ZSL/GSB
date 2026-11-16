from pipeline.alignment import alignment_report
from pipeline.db import Database


def _seed(db):
    base = {"business_id": "BIZ-001", "name": "Gadget-001", "price": 87.5,
            "currency": "CNY", "category": "cat-1", "stock": 6,
            "description": "d"}
    db.insert_normalized("t", "alpha", 1, "BIZ-001", dict(base),
                         {"source": "alpha", "schema_version": 1})
    v2 = dict(base, color="red")
    db.insert_normalized("t", "alpha", 2, "BIZ-001", v2,
                         {"source": "alpha", "schema_version": 2})
    beta = dict(base, price=11.25, currency="USD")
    db.insert_normalized("t", "beta", 1, "BIZ-001", beta,
                         {"source": "beta", "schema_version": 1})
    beta2 = dict(beta, color="red")
    db.insert_normalized("t", "beta", 2, "BIZ-001", beta2,
                         {"source": "beta", "schema_version": 2})


def test_alignment_pairs_and_added_fields(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    _seed(db)
    report = alignment_report(db, "BIZ-001")
    assert report["versions"] == ["alpha/1", "alpha/2", "beta/1", "beta/2"]
    assert len(report["pairs"]) == 6
    added_pairs = [p for p in report["pairs"] if p["added_fields"]]
    assert len(added_pairs) == 3
    assert all(p["added_fields"] == ["color"] for p in added_pairs)
    assert report["summary"]["all_required_complete"]
    assert report["summary"]["missing_required_total"] == 0
    assert report["summary"]["version_evidence_consistent"]
    db.close()


def test_alignment_detects_type_change_and_missing_required(tmp_path):
    db = Database(str(tmp_path / "b.db"))
    db.insert_normalized("t", "alpha", 1, "BIZ-009",
                         {"business_id": "BIZ-009", "name": "x", "price": 1.5,
                          "currency": "CNY", "category": "c", "stock": 3},
                         {"source": "alpha", "schema_version": 1})
    db.insert_normalized("t", "beta", 1, "BIZ-009",
                         {"business_id": "BIZ-009", "name": "x", "price": "1.50",
                          "currency": "CNY", "category": None, "stock": 3, "color": "red"},
                         {"source": "beta", "schema_version": 1})
    report = alignment_report(db, "BIZ-009")
    pair = report["pairs"][0]
    assert pair["type_changes"] == ["category", "price"]
    assert pair["added_fields"] == ["color"]
    assert report["required_missing"]["beta/1"] == ["category"]
    assert not report["summary"]["all_required_complete"]
    db.close()
