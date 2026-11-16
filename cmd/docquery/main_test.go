package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestUnsupportedQueryCLIExitsOneWithEQuery(t *testing.T) {
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	exit := run([]string{"query", "-root", "../..", "-query", "queries/unsupported.json", "-log", ""}, &stdout, &stderr)
	if exit != 1 {
		t.Fatalf("expected exit 1, got %d; stdout=%s stderr=%s", exit, stdout.String(), stderr.String())
	}
	if !strings.Contains(stdout.String(), `"code": "E_QUERY"`) {
		t.Fatalf("expected E_QUERY output, got %s", stdout.String())
	}
}

func TestDocumentCorruptionInjectionCLIExitsZero(t *testing.T) {
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	logPath := t.TempDir() + "/doc.log"
	exit := run([]string{"inject", "-root", "../..", "-kind", "doc-corruption", "-log", logPath}, &stdout, &stderr)
	if exit != 0 {
		t.Fatalf("expected exit 0, got %d; stdout=%s stderr=%s", exit, stdout.String(), stderr.String())
	}
	if !strings.Contains(stdout.String(), `"code": "E_DOC"`) || !strings.Contains(stdout.String(), `"field_path": "stock.bad-key"`) {
		t.Fatalf("unexpected injection output: %s", stdout.String())
	}
}
