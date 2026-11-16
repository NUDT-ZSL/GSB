package docquery

import (
	"errors"
	"os"
	"testing"
)

func mustLoadCollection(t *testing.T) *Collection {
	t.Helper()
	collection, err := LoadCollection("samples/products.json")
	if err != nil {
		t.Fatalf("LoadCollection: %v", err)
	}
	return collection
}

func mustLoadQuery(t *testing.T, path string) *Query {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read query %s: %v", path, err)
	}
	query, err := ParseQuery(data)
	if err != nil {
		t.Fatalf("ParseQuery %s: %v", path, err)
	}
	return query
}

func newTestSession(t *testing.T, indexPaths []string) *Session {
	t.Helper()
	collection := mustLoadCollection(t)
	session, err := NewSession(collection, SessionOptions{IndexPaths: indexPaths})
	if err != nil {
		t.Fatalf("NewSession: %v", err)
	}
	return session
}

func requireBusinessCode(t *testing.T, err error, code string) *BusinessError {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error code %s, got nil", code)
	}
	var business *BusinessError
	if !errors.As(err, &business) {
		t.Fatalf("expected BusinessError %s, got %T: %v", code, err, err)
	}
	if business.Code != code {
		t.Fatalf("expected error code %s, got %s: %v", code, business.Code, business)
	}
	return business
}
