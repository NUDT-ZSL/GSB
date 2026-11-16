package docquery

import (
	"strings"
	"testing"
)

func TestCollectionLoadsAndHasStableNestedVersion(t *testing.T) {
	first := mustLoadCollection(t)
	second := mustLoadCollection(t)
	if first.Version == "" || first.Version != second.Version {
		t.Fatalf("collection versions are not stable: %q != %q", first.Version, second.Version)
	}
	if got := len(first.Documents); got != 12 {
		t.Fatalf("expected 12 documents, got %d", got)
	}
	value, ok := GetField(first.Documents[0], "stock.warehouse")
	if !ok || value != "east" {
		t.Fatalf("unexpected nested value: %#v, %v", value, ok)
	}
}

func TestCorruptDocumentReportsFirstDocumentIndexAndFieldPath(t *testing.T) {
	_, err := LoadCollection("samples/corrupt-products.json")
	business := requireBusinessCode(t, err, CodeDoc)
	if business.DocIndex == nil || *business.DocIndex != 2 {
		t.Fatalf("expected document index 2, got %#v", business.DocIndex)
	}
	if business.FieldPath != "stock.bad-key" {
		t.Fatalf("expected field path stock.bad-key, got %q", business.FieldPath)
	}
}

func TestDuplicatePrimaryKeyRejectsWholeCollection(t *testing.T) {
	data := []byte(`{
	  "name": "dup",
	  "primary_key": "id",
	  "documents": [{"id":"same"},{"id":"same"}]
	}`)
	_, err := ParseCollection(data)
	business := requireBusinessCode(t, err, CodeDoc)
	if business.DocIndex == nil || *business.DocIndex != 1 || business.FieldPath != "id" {
		t.Fatalf("unexpected duplicate key report: %#v", business)
	}
}

func TestMissingPrimaryKeyAndArrayAreRejected(t *testing.T) {
	tests := []struct {
		name string
		json string
		path string
	}{
		{
			name: "missing primary key",
			json: `{"name":"bad","primary_key":"id","documents":[{"name":"x"}]}`,
			path: "id",
		},
		{
			name: "array field",
			json: `{"name":"bad","primary_key":"id","documents":[{"id":"x","tags":["a"]}]}`,
			path: "tags",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := ParseCollection([]byte(test.json))
			business := requireBusinessCode(t, err, CodeDoc)
			if business.FieldPath != test.path {
				t.Fatalf("expected path %q, got %q", test.path, business.FieldPath)
			}
		})
	}
}

func TestCatalogRejectsCaseInsensitiveDuplicateNames(t *testing.T) {
	a, err := NewCollection("Products", "id", []Document{{"id": "a"}})
	if err != nil {
		t.Fatal(err)
	}
	b, err := NewCollection("products", "id", []Document{{"id": "b"}})
	if err != nil {
		t.Fatal(err)
	}
	catalog := NewCatalog()
	if err := catalog.Add(a); err != nil {
		t.Fatal(err)
	}
	err = catalog.Add(b)
	business := requireBusinessCode(t, err, CodeDoc)
	if !strings.Contains(business.Message, "duplicate collection name") {
		t.Fatalf("unexpected message: %s", business.Message)
	}
}

func TestInvalidPrimaryKeyPathIsEDoc(t *testing.T) {
	_, err := ParseCollection([]byte(`{"name":"bad","primary_key":"bad-key","documents":[]}`))
	business := requireBusinessCode(t, err, CodeDoc)
	if business.FieldPath != "bad-key" {
		t.Fatalf("unexpected primary key path: %#v", business)
	}
}
