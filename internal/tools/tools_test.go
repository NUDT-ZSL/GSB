package tools

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/engine"
)

func TestCheckValidDatabase(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "queue.db")
	eng, err := engine.Open(context.Background(), engine.Options{
		DBPath:               dbPath,
		MaxRetries:           3,
		DefaultWindowTimeout: 5 * time.Second,
		DisableSweeper:       true,
	})
	if err != nil {
		t.Fatalf("open engine: %v", err)
	}
	if _, biz := eng.Append(context.Background(), "append-1", "orders.eu", "producer-1", 1, "sample"); biz != nil {
		t.Fatalf("append: %v", biz)
	}
	if _, biz := eng.Pull(context.Background(), "pull-1", "orders.eu", "consumer-1", 1, 5000); biz != nil {
		t.Fatalf("pull: %v", biz)
	}
	if _, biz := eng.Commit(context.Background(), "commit-1", "orders.eu", "consumer-1", 1); biz != nil {
		t.Fatalf("commit: %v", biz)
	}
	if err := eng.Close(); err != nil {
		t.Fatalf("close engine: %v", err)
	}

	reportPath := filepath.Join(dir, "verification.json")
	logPath := filepath.Join(dir, "verify.jsonl")
	if code := Check("verify", []string{"--db", dbPath, "--out", reportPath, "--log", logPath}); code != 0 {
		t.Fatalf("verify exit code = %d", code)
	}
	data, err := os.ReadFile(reportPath)
	if err != nil {
		t.Fatalf("read verification report: %v", err)
	}
	var report struct {
		OK         bool  `json:"ok"`
		Violations []any `json:"violations"`
	}
	if err := json.Unmarshal(data, &report); err != nil {
		t.Fatalf("decode verification report: %v", err)
	}
	if !report.OK || len(report.Violations) != 0 {
		t.Fatalf("unexpected verification report: %s", data)
	}
}

func TestFaultKinds(t *testing.T) {
	tests := []struct {
		kind string
		code string
	}{
		{kind: "invalid-partition", code: "E_APPEND"},
		{kind: "corrupt-ledger", code: "E_LEDGER"},
		{kind: "persistent-failure", code: "E_POISON"},
	}
	for _, test := range tests {
		t.Run(test.kind, func(t *testing.T) {
			dir := t.TempDir()
			outPath := filepath.Join(dir, "fault.json")
			logPath := filepath.Join(dir, "fault.jsonl")
			if code := Fault("fault", []string{
				"--kind", test.kind,
				"--out", outPath,
				"--work-dir", filepath.Join(dir, "work"),
				"--log", logPath,
			}); code != 0 {
				t.Fatalf("fault exit code = %d", code)
			}
			data, err := os.ReadFile(outPath)
			if err != nil {
				t.Fatalf("read fault report: %v", err)
			}
			var report struct {
				Pass       bool   `json:"pass"`
				ActualCode string `json:"actual_code"`
			}
			if err := json.Unmarshal(data, &report); err != nil {
				t.Fatalf("decode fault report: %v", err)
			}
			if !report.Pass || report.ActualCode != test.code {
				t.Fatalf("unexpected fault report: %s", data)
			}
		})
	}
}
