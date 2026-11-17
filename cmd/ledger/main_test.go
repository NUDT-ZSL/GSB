package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func samplePath(name string) string {
	return filepath.Join("..", "..", "samples", name)
}

func runCLI(t *testing.T, args ...string) (int, []byte, []byte) {
	t.Helper()
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	code := run(args, &stdout, &stderr)
	return code, stdout.Bytes(), stderr.Bytes()
}

func TestCLIEndToEndAndErrorCodes(t *testing.T) {
	dir := t.TempDir()
	db := filepath.Join(dir, "demo.db")
	logPath := filepath.Join(dir, "events.jsonl")

	code, stdout, stderr := runCLI(t, "apply", "--spec", samplePath("manifest-v1.json"), "--db", db, "--session", "cli", "--capacity", "1200", "--log", logPath)
	if code != 0 {
		t.Fatalf("apply v1 failed: %s %s", stdout, stderr)
	}
	var first applyReport
	if err := json.Unmarshal(stdout, &first); err != nil || !first.OK {
		t.Fatalf("invalid apply report: %v %s", err, stdout)
	}

	code, stdout, stderr = runCLI(t, "apply", "--spec", samplePath("manifest-v2.json"), "--db", db, "--session", "cli", "--capacity", "1200", "--log", logPath)
	if code != 0 {
		t.Fatalf("apply v2 failed: %s %s", stdout, stderr)
	}
	var second applyReport
	if err := json.Unmarshal(stdout, &second); err != nil || !second.OK {
		t.Fatalf("invalid v2 report: %v %s", err, stdout)
	}
	if second.ProductVersions["base.port"] != 2 || second.ProductVersions["gateway.route"] != 1 {
		t.Fatalf("unexpected product versions: %+v", second.ProductVersions)
	}
	if len(second.Invalidated) != 4 {
		t.Fatalf("unexpected invalidation set: %v", second.Invalidated)
	}

	for _, command := range []string{"verify", "replay"} {
		code, stdout, stderr = runCLI(t, command, "--db", db, "--session", "cli", "--log", logPath)
		if code != 0 || !strings.Contains(string(stdout), `"ok": true`) {
			t.Fatalf("%s failed: %s %s", command, stdout, stderr)
		}
	}

	code, stdout, stderr = runCLI(t, "restore", "--db", db, "--session", "cli", "--version", "v1", "--log", logPath)
	if code != 0 || !strings.Contains(string(stdout), `"whole_replaced": true`) {
		t.Fatalf("restore failed: %s %s", stdout, stderr)
	}

	code, stdout, stderr = runCLI(t, "apply", "--spec", samplePath("manifest-v2.json"), "--db", db, "--session", "cli", "--capacity", "1200", "--log", logPath)
	if code != 0 {
		t.Fatalf("apply after restore failed: %s %s", stdout, stderr)
	}
	var afterRestore applyReport
	if err := json.Unmarshal(stdout, &afterRestore); err != nil || !afterRestore.OK {
		t.Fatalf("invalid post-restore report: %v %s", err, stdout)
	}
	if afterRestore.Snapshot.ParentID != first.Snapshot.ID {
		t.Fatalf("post-restore snapshot parent = %d, want restored snapshot %d", afterRestore.Snapshot.ParentID, first.Snapshot.ID)
	}
	for _, command := range []string{"verify", "replay"} {
		code, stdout, stderr = runCLI(t, command, "--db", db, "--session", "cli", "--log", logPath)
		if code != 0 {
			t.Fatalf("%s after restore failed: %s %s", command, stdout, stderr)
		}
	}

	tests := []struct {
		name string
		args []string
		code string
	}{
		{
			name: "inject dependency cycle",
			args: []string{"apply", "--spec", samplePath("manifest-v1.json"), "--db", filepath.Join(dir, "cycle.db"), "--session", "errors", "--inject", "dependency-cycle", "--log", filepath.Join(dir, "cycle.jsonl")},
			code: "E_DEPEND",
		},
		{
			name: "inject corrupt spec",
			args: []string{"apply", "--spec", samplePath("manifest-v1.json"), "--db", filepath.Join(dir, "corrupt.db"), "--session", "errors", "--inject", "corrupt-spec", "--log", filepath.Join(dir, "corrupt.jsonl")},
			code: "E_SPEC",
		},
		{
			name: "inject zero capacity",
			args: []string{"apply", "--spec", samplePath("manifest-v1.json"), "--db", filepath.Join(dir, "zero.db"), "--session", "errors", "--inject", "zero-capacity", "--log", filepath.Join(dir, "zero.jsonl")},
			code: "E_CAPACITY",
		},
		{
			name: "natural cycle",
			args: []string{"apply", "--spec", samplePath("manifest-cycle.json"), "--db", filepath.Join(dir, "natural-cycle.db"), "--session", "errors", "--log", filepath.Join(dir, "natural-cycle.jsonl")},
			code: "E_DEPEND",
		},
		{
			name: "natural broken spec",
			args: []string{"apply", "--spec", samplePath("manifest-broken.json"), "--db", filepath.Join(dir, "broken.db"), "--session", "errors", "--log", filepath.Join(dir, "broken.jsonl")},
			code: "E_SPEC",
		},
		{
			name: "zero capacity options",
			args: []string{"apply", "--spec", samplePath("manifest-v1.json"), "--db", filepath.Join(dir, "options-zero.db"), "--session", "errors", "--options", samplePath("options-zero-capacity.json"), "--log", filepath.Join(dir, "options-zero.jsonl")},
			code: "E_CAPACITY",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			code, stdout, stderr := runCLI(t, test.args...)
			if code == 0 {
				t.Fatalf("expected failure, stdout=%s", stdout)
			}
			var response errorResponse
			if err := json.Unmarshal(stderr, &response); err != nil {
				t.Fatalf("invalid error response: %v %s", err, stderr)
			}
			if response.Error == nil || response.Error.Code != test.code {
				t.Fatalf("got %+v, want %s", response.Error, test.code)
			}
		})
	}

	code, stdout, stderr = runCLI(t, "apply", "--spec", samplePath("manifest-delete-referenced.json"), "--db", db, "--session", "cli", "--log", logPath)
	if code == 0 {
		t.Fatalf("expected E_REF, stdout=%s", stdout)
	}
	var refResponse errorResponse
	if err := json.Unmarshal(stderr, &refResponse); err != nil {
		t.Fatal(err)
	}
	if refResponse.Error == nil || refResponse.Error.Code != "E_REF" || len(refResponse.Error.Refs) == 0 {
		t.Fatalf("expected E_REF with references, got %+v", refResponse.Error)
	}

	logData, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(logData), "command.failed") || !strings.Contains(string(logData), "E_REF") {
		t.Fatalf("structured log did not record failures: %s", logData)
	}
}
