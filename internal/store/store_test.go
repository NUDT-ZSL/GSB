package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"locklease/internal/model"
)

func newTestStore(t *testing.T) *Store {
	t.Helper()
	dir := t.TempDir()
	gf := model.GraphFile{Version: 7, Nodes: []model.Node{
		{Key: "A"},
		{Key: "B", DependsOn: []string{"A"}},
		{Key: "C", DependsOn: []string{"A"}},
		{Key: "D", DependsOn: []string{"B", "C"}},
	}}
	gPath := filepath.Join(dir, "graph.json")
	data, _ := json.Marshal(gf)
	if err := os.WriteFile(gPath, data, 0o644); err != nil {
		t.Fatal(err)
	}
	st, err := New(filepath.Join(dir, "data"), gPath)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	return st
}

func TestAcquireLockedAndRenewVersion(t *testing.T) {
	st := newTestStore(t)
	lk, _, err := st.Acquire("A", "h1", 1000, 1000, false)
	if err != nil {
		t.Fatal(err)
	}
	if lk.Version != 1 || lk.ExpiresAt != 2000 {
		t.Fatalf("unexpected lock: %+v", lk)
	}
	if _, _, err := st.Acquire("A", "h2", 1000, 1000, false); err == nil {
		t.Fatal("expected E_LOCKED")
	} else if be, ok := err.(*BizError); !ok || be.Code != ELocked || be.Detail["holder"] != "h1" {
		t.Fatalf("unexpected duplicate error: %v", err)
	}
	if _, err := st.Renew("A", "h1", 99, 1000, 1500); err == nil {
		t.Fatal("expected E_VERSION")
	}
	lk2, err := st.Renew("A", "h1", 1, 1000, 1500)
	if err != nil {
		t.Fatal(err)
	}
	if lk2.ExpiresAt != 2500 {
		t.Fatalf("renew expiry wrong: %d", lk2.ExpiresAt)
	}
}

func TestExpiryBeatsConcurrentRenew(t *testing.T) {
	st := newTestStore(t)
	_, _, _ = st.Acquire("A", "h1", 1000, 1000, false)
	// At the exact boundary now == expires_at (2000): expiry is inclusive (<=).
	if _, err := st.Renew("A", "h1", 1, 1000, 2000); err == nil {
		t.Fatal("renewal at expiry boundary must fail; lock must be reaped")
	} else if be, ok := err.(*BizError); !ok || be.Code != EVersion {
		t.Fatalf("want E_VERSION, got %v", err)
	}
	if lk, _ := st.Query("A", 2000); lk != nil {
		t.Fatal("expired lock still present")
	}
	if len(st.reclaims) != 1 || st.reclaims[0].Reason != "expired" || st.reclaims[0].Version != 1 {
		t.Fatalf("reclaim record wrong: %+v", st.reclaims)
	}
}

func TestPropagationOrderAndNoDoublePropagation(t *testing.T) {
	st := newTestStore(t)
	_, _, _ = st.Acquire("A", "hA", 10000, 1000, false)
	_, _, _ = st.Acquire("B", "hB", 10000, 1001, false)
	_, _, _ = st.Acquire("C", "hC", 10000, 1002, false)
	_, _, _ = st.Acquire("D", "hD", 10000, 1003, false)

	traces, err := st.Release("A", "hA", 1, 2000)
	if err != nil {
		t.Fatal(err)
	}
	got := make([]string, 0, len(traces))
	seen := map[string]int{}
	for _, tr := range traces {
		got = append(got, tr.Affected)
		seen[tr.Affected]++
	}
	want := []string{"B", "C", "D"}
	if len(got) != 3 {
		t.Fatalf("trace = %v", got)
	}
	for i, k := range want {
		if got[i] != k || seen[k] != 1 {
			t.Fatalf("propagation order/dup wrong: %v", got)
		}
	}
	// Downstream leases were invalidated.
	for _, k := range []string{"B", "C", "D"} {
		if lk, _ := st.Query(k, 2000); lk != nil {
			t.Fatalf("downstream lock %s should be invalidated", k)
		}
	}
}

func TestPreemptionPropagates(t *testing.T) {
	st := newTestStore(t)
	_, _, _ = st.Acquire("A", "old", 10000, 1000, false)
	_, _, _ = st.Acquire("B", "down", 10000, 1001, false)
	lk, traces, err := st.Acquire("A", "new", 10000, 2000, true)
	if err != nil {
		t.Fatal(err)
	}
	if lk.Holder != "new" || lk.Version != 2 {
		t.Fatalf("preempt lock wrong: %+v", lk)
	}
	affected := make([]string, 0, len(traces))
	for _, tr := range traces {
		if tr.Cause != "preempt" || tr.SourceKey != "A" {
			t.Fatalf("preempt trace wrong: %+v", tr)
		}
		affected = append(affected, tr.Affected)
	}
	want := []string{"B"}
	if strings.Join(affected, ",") != strings.Join(want, ",") {
		t.Fatalf("preempt downstream = %v, want %v", affected, want)
	}
}

func TestSnapshotRestoreReplacesAndReplays(t *testing.T) {
	st := newTestStore(t)
	_, _, _ = st.Acquire("A", "hA", 10000, 1000, false)
	_, _, _ = st.Acquire("B", "hB", 10000, 1001, false)
	_, _ = st.Release("A", "hA", 1, 2000)
	path, _, err := st.TakeSnapshot("snap-1")
	if err != nil {
		t.Fatal(err)
	}

	// A second, divergent store instance restores the snapshot wholesale.
	st2 := newTestStore(t)
	_, _, _ = st2.Acquire("C", "divergent", 10000, 5000, false)
	res, err := st2.RestoreSnapshot(path)
	if err != nil {
		t.Fatalf("replay should pass: %v", err)
	}
	if !res.Replaced {
		t.Fatal("version mismatch must trigger wholesale replacement")
	}
	if _, ok := st2.locks["C"]; ok {
		t.Fatal("incremental merge detected: divergent key survived restore")
	}
	if lk, _ := st2.Query("B", 2000); lk != nil {
		t.Fatal("restored B should reflect invalidation")
	}
	// New requests must continue to work after restore.
	if _, _, err := st2.Acquire("D", "post-restore", 1000, 9000, false); err != nil {
		t.Fatalf("post-restore acquire: %v", err)
	}
}

func TestReplayDetectsTamperedTrace(t *testing.T) {
	st := newTestStore(t)
	_, _, _ = st.Acquire("A", "hA", 10000, 1000, false)
	_, _, _ = st.Acquire("B", "hB", 10000, 1001, false)
	_, _ = st.Release("A", "hA", 1, 2000)
	path, _, _ := st.TakeSnapshot("snap-bad")

	raw, _ := os.ReadFile(path)
	var snap model.Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		t.Fatal(err)
	}
	// Corrupt the recorded version so replay comparison must fail.
	snap.Traces[0].Version = 1
	tampered := filepath.Join(st.dir, "tampered.json")
	data, _ := json.Marshal(snap)
	_ = os.WriteFile(tampered, data, 0o644)

	st2 := newTestStore(t)
	_, err := st2.RestoreSnapshot(tampered)
	if err == nil {
		t.Fatal("expected E_REPLAY")
	}
	be, ok := err.(*BizError)
	if !ok || be.Code != EReplay {
		t.Fatalf("want E_REPLAY, got %v", err)
	}
}

func TestCorruptSnapshotRejected(t *testing.T) {
	st := newTestStore(t)
	bad := filepath.Join(st.dir, "bad.json")
	_ = os.WriteFile(bad, []byte("{not-json"), 0o644)
	_, err := st.RestoreSnapshot(bad)
	if be, ok := err.(*BizError); !ok || be.Code != ESnapshot {
		t.Fatalf("want E_SNAPSHOT, got %v", err)
	}
}
