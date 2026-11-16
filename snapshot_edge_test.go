package docquery

import (
	"path/filepath"
	"testing"
)

func TestFreshSessionSnapshotRestoresWithoutHistory(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	path := filepath.Join(t.TempDir(), "fresh.json")
	if err := session.SaveSnapshot(path); err != nil {
		t.Fatalf("SaveSnapshot: %v", err)
	}
	restored := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	report, err := restored.RestoreSnapshot(path)
	if err != nil {
		t.Fatalf("RestoreSnapshot: %v", err)
	}
	if report.ReplayedQueries != 0 || !report.VersionMatched || !report.StateReplaced {
		t.Fatalf("unexpected restore report: %#v", report)
	}
	if restored.PlanCacheStatus().EntryCount != 0 {
		t.Fatalf("fresh snapshot unexpectedly restored plan entries: %#v", restored.PlanCacheStatus())
	}
}
