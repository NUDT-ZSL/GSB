package verify

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/engine"
	_ "github.com/ncruces/go-sqlite3/driver"
	_ "github.com/ncruces/go-sqlite3/embed"
)

func populatedDatabase(t *testing.T) (string, string) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "queue.db")
	eng, err := engine.Open(context.Background(), engine.Options{
		DBPath:         dbPath,
		MaxRetries:     3,
		DisableSweeper: true,
	})
	if err != nil {
		t.Fatalf("open engine: %v", err)
	}
	ctx := context.Background()
	for seq := int64(1); seq <= 4; seq++ {
		if _, biz := eng.Append(ctx, "append", "verify-partition", "producer", seq, "body"); biz != nil {
			t.Fatalf("append: %v", biz)
		}
	}
	if _, biz := eng.Pull(ctx, "pull", "verify-partition", "consumer", 3, 5000); biz != nil {
		t.Fatalf("pull: %v", biz)
	}
	if _, biz := eng.Commit(ctx, "commit", "verify-partition", "consumer", 1); biz != nil {
		t.Fatalf("commit: %v", biz)
	}
	if _, biz := eng.Fail(ctx, "fail", "verify-partition", "consumer", 2, "injected"); biz != nil {
		t.Fatalf("fail: %v", biz)
	}
	if err := eng.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	return dir, dbPath
}

func TestVerifyHealthyLedger(t *testing.T) {
	_, dbPath := populatedDatabase(t)
	report, err := Run(context.Background(), Options{DBPath: dbPath})
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !report.OK || report.Code != "OK" || len(report.Violations) != 0 {
		t.Fatalf("healthy database was rejected: %#v", report)
	}
	if report.PartitionsChecked != 1 || report.MessagesChecked != 4 {
		t.Fatalf("unexpected verify counts: %#v", report)
	}
}

func TestVerifyDetectsRetryLedgerMismatch(t *testing.T) {
	dir, dbPath := populatedDatabase(t)
	db, err := sql.Open("sqlite3", filepath.Join(dir, "queue.db"))
	if err != nil {
		t.Fatalf("open database: %v", err)
	}
	if _, err := db.Exec(`UPDATE retry_counts SET attempts=99 WHERE partition_key='verify-partition' AND message_offset=2`); err != nil {
		t.Fatalf("corrupt retry count: %v", err)
	}
	db.Close()
	report, err := Run(context.Background(), Options{DBPath: dbPath})
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if report.OK || report.Code != "E_VERIFY" || report.FirstViolation == nil {
		t.Fatalf("expected E_VERIFY, got %#v", report)
	}
	first := report.FirstViolation
	if first.PartitionKey != "verify-partition" || first.Offset != 2 || first.Field != "retry_counts" {
		t.Fatalf("unexpected first violation: %#v", first)
	}
}

func TestVerifyDetectsOrderingBreak(t *testing.T) {
	dir, dbPath := populatedDatabase(t)
	db, err := sql.Open("sqlite3", filepath.Join(dir, "queue.db"))
	if err != nil {
		t.Fatalf("open database: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO events(partition_key,kind,session,message_offset,start_offset,end_offset,committed_before,committed_after,retry_count,code,detail,created_unix_ms) VALUES('verify-partition','commit','consumer',4,1,4,0,4,0,'OK','injected ordering break',0)`); err != nil {
		t.Fatalf("corrupt event journal: %v", err)
	}
	db.Close()
	report, err := Run(context.Background(), Options{DBPath: dbPath})
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if report.OK || report.FirstViolation == nil || report.FirstViolation.Field != "commit" {
		t.Fatalf("expected commit ordering violation, got %#v", report)
	}
}
