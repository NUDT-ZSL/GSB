package docquery

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestSnapshotRestoreReplacesStateAndReplaysSameSummary(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/hardware.json")
	originalResult, err := session.Execute(query)
	if err != nil {
		t.Fatal(err)
	}
	originalJSON, err := originalResult.StableJSON()
	if err != nil {
		t.Fatal(err)
	}
	snapshotPath := filepath.Join(t.TempDir(), "session.json")
	if err := session.SaveSnapshot(snapshotPath); err != nil {
		t.Fatalf("SaveSnapshot: %v", err)
	}
	snapshotBytes, err := os.ReadFile(snapshotPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.ApplyPatchFile("samples/products-patch.json"); err != nil {
		t.Fatal(err)
	}
	if _, err := session.Execute(query); err != nil {
		t.Fatal(err)
	}
	secondQuery := mustLoadQuery(t, "queries/in-list.json")
	if _, err := session.Execute(secondQuery); err != nil {
		t.Fatal(err)
	}
	if session.PlanCacheStatus().EntryCount != 2 || session.PlanCacheStatus().Stale {
		t.Fatalf("expected updated plan cache before restore: %#v", session.PlanCacheStatus())
	}

	report, err := session.RestoreSnapshot(snapshotPath)
	if err != nil {
		t.Fatalf("RestoreSnapshot: %v", err)
	}
	if report.VersionMatched || !report.StateReplaced || report.ReplayedQueries != 1 {
		t.Fatalf("unexpected restore report: %#v", report)
	}
	if session.CollectionVersion() != originalResult.CollectionVersion {
		t.Fatalf("collection was not replaced: %s != %s", session.CollectionVersion(), originalResult.CollectionVersion)
	}
	if session.PlanCacheStatus().EntryCount != 1 || session.PlanCacheStatus().Stale {
		t.Fatalf("plan cache was not restored as one generation: %#v", session.PlanCacheStatus())
	}
	for _, queryID := range session.PlanCacheStatus().QueryIDs {
		if queryID == secondQuery.ID() {
			t.Fatalf("stale post-update cache entry survived overall replacement")
		}
	}

	replayed, err := session.Execute(query)
	if err != nil {
		t.Fatal(err)
	}
	if replayed.Summary != originalResult.Summary {
		t.Fatalf("summary changed after restore: %s != %s", replayed.Summary, originalResult.Summary)
	}
	replayedJSON, err := replayed.StableJSON()
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(originalJSON, replayedJSON) {
		t.Fatalf("result bytes changed after restore")
	}

	secondSnapshot := filepath.Join(t.TempDir(), "session-again.json")
	if err := session.SaveSnapshot(secondSnapshot); err != nil {
		t.Fatal(err)
	}
	// The extra execution above adds one history/trace entry, so compare a fresh
	// restore of the original snapshot against the original bytes instead.
	restoredAgain := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	if _, err := restoredAgain.RestoreSnapshot(snapshotPath); err != nil {
		t.Fatal(err)
	}
	reSavePath := filepath.Join(t.TempDir(), "roundtrip.json")
	if err := restoredAgain.SaveSnapshot(reSavePath); err != nil {
		t.Fatal(err)
	}
	reSavedBytes, err := os.ReadFile(reSavePath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(snapshotBytes, reSavedBytes) {
		t.Fatalf("snapshot round trip is not byte-identical")
	}
}

func TestMissingSnapshotVersionReturnsEReplay(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/hardware.json")
	if _, err := session.Execute(query); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "session.json")
	if err := session.SaveSnapshot(path); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatal(err)
	}
	delete(raw, "index_set_version")
	broken, err := json.Marshal(raw)
	if err != nil {
		t.Fatal(err)
	}
	brokenPath := filepath.Join(t.TempDir(), "broken.json")
	if err := os.WriteFile(brokenPath, broken, 0o644); err != nil {
		t.Fatal(err)
	}
	_, err = session.RestoreSnapshot(brokenPath)
	business := requireBusinessCode(t, err, CodeReplay)
	if business.FieldPath != "index_set_version" {
		t.Fatalf("unexpected missing-version path: %#v", business)
	}
}

func TestReplayMismatchInjectionReturnsEReplay(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/hardware.json")
	if _, err := session.Execute(query); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "session.json")
	if err := session.SaveSnapshot(path); err != nil {
		t.Fatal(err)
	}
	session.SetInjections(false, true)
	report, err := session.RestoreSnapshot(path)
	business := requireBusinessCode(t, err, CodeReplay)
	if business.QueryID == "" || business.Expected == "" || business.Actual == "" {
		t.Fatalf("replay error lacks reproducibility fields: %#v", business)
	}
	if report == nil || report.ReplayedQueries != 0 {
		t.Fatalf("unexpected replay report: %#v", report)
	}
}

func TestFailedReplayRollsBackSessionState(t *testing.T) {
	session := newTestSession(t, []string{"category", "price", "stock.warehouse"})
	query := mustLoadQuery(t, "queries/hardware.json")
	if _, err := session.Execute(query); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "session.json")
	if err := session.SaveSnapshot(path); err != nil {
		t.Fatal(err)
	}
	if err := session.ApplyPatchFile("samples/products-patch.json"); err != nil {
		t.Fatal(err)
	}
	patchedVersion := session.CollectionVersion()
	session.SetInjections(false, true)
	report, err := session.RestoreSnapshot(path)
	requireBusinessCode(t, err, CodeReplay)
	if report == nil || report.StateReplaced || report.ReplacementMode != "rolled_back" {
		t.Fatalf("unexpected rollback report: %#v", report)
	}
	if session.CollectionVersion() != patchedVersion {
		t.Fatalf("session state was not rolled back: %s != %s", session.CollectionVersion(), patchedVersion)
	}
}
