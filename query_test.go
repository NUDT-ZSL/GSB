package docquery

import (
	"os"
	"testing"
)

func TestSemanticQueryNormalizationAndStableID(t *testing.T) {
	firstData, err := os.ReadFile("queries/hardware.json")
	if err != nil {
		t.Fatal(err)
	}
	secondData, err := os.ReadFile("queries/hardware-equivalent-case.json")
	if err != nil {
		t.Fatal(err)
	}
	first, err := ParseQuery(firstData)
	if err != nil {
		t.Fatalf("first query: %v", err)
	}
	second, err := ParseQuery(secondData)
	if err != nil {
		t.Fatalf("second query: %v", err)
	}
	if first.CanonicalText() != second.CanonicalText() {
		t.Fatalf("canonical text differs:\n%s\n%s", first.CanonicalText(), second.CanonicalText())
	}
	if first.ID() != second.ID() || first.ID() == "" {
		t.Fatalf("query ids differ: %s != %s", first.ID(), second.ID())
	}
}

func TestINListIsCanonicalizedAsSet(t *testing.T) {
	a := []byte(`{"collection":"products","filter":{"op":"in","path":"category","values":["home","stationery","home"]}}`)
	b := []byte(`{"collection":"PRODUCTS","filter":{"values":["stationery","home"],"path":"category","op":"IN"}}`)
	qa, err := ParseQuery(a)
	if err != nil {
		t.Fatal(err)
	}
	qb, err := ParseQuery(b)
	if err != nil {
		t.Fatal(err)
	}
	if qa.ID() != qb.ID() {
		t.Fatalf("IN query ids differ: %s != %s\n%s\n%s", qa.ID(), qb.ID(), qa.CanonicalText(), qb.CanonicalText())
	}
}

func TestRangeShorthandAndRangeObjectNormalizeEqually(t *testing.T) {
	a := []byte(`{"collection":"products","filter":{"op":"range","path":"price","gte":20}}`)
	b := []byte(`{"collection":"products","filter":{"op":"GTE","path":"price","value":20.0}}`)
	qa, err := ParseQuery(a)
	if err != nil {
		t.Fatal(err)
	}
	qb, err := ParseQuery(b)
	if err != nil {
		t.Fatal(err)
	}
	if qa.ID() != qb.ID() {
		t.Fatalf("range query ids differ: %s != %s", qa.CanonicalText(), qb.CanonicalText())
	}
}

func TestUnsupportedQuerySyntaxReturnsEQuery(t *testing.T) {
	data, err := os.ReadFile("queries/unsupported.json")
	if err != nil {
		t.Fatal(err)
	}
	_, err = ParseQuery(data)
	requireBusinessCode(t, err, CodeQuery)
}

func TestInvalidQueryFieldPathReturnsEQuery(t *testing.T) {
	_, err := ParseQuery([]byte(`{"collection":"products","filter":{"op":"eq","path":"bad-key","value":1}}`))
	requireBusinessCode(t, err, CodeQuery)
}
