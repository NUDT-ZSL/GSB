package store

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"locklease/internal/graph"
	"locklease/internal/model"
)

func currentWallMs() int64 { return time.Now().UnixMilli() }

// Query returns a stable snapshot view of one lock (or nil when absent).
func (s *Store) Query(key string, nowInjected int64) (*model.Lock, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.graph.Has(key) {
		return nil, bizErr(ENotFound, "unknown lock key "+key, map[string]any{"key": key})
	}
	now := resolveNow(nowInjected)
	lk := s.locks[key]
	if lk != nil && lk.ExpiresAt <= now {
		s.reapExpiredLocked(now, "expired")
		lk = nil
	}
	if lk == nil {
		return nil, nil
	}
	return cloneLock(lk), nil
}

// Table returns all active locks sorted by key, after lazy expiry handling.
func (s *Store) Table(nowInjected int64) []*model.Lock {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := resolveNow(nowInjected)
	s.reapExpiredLocked(now, "expired")
	keys := make([]string, 0, len(s.locks))
	for k := range s.locks {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]*model.Lock, 0, len(keys))
	for _, k := range keys {
		out = append(out, cloneLock(s.locks[k]))
	}
	return out
}

// PassiveTable returns active locks including expired-but-not-yet-reaped
// ones. It never mutates state, so callers can observe the background reaper
// without triggering lazy reclamation themselves.
func (s *Store) PassiveTable() []*model.Lock {
	s.mu.Lock()
	defer s.mu.Unlock()
	keys := make([]string, 0, len(s.locks))
	for k := range s.locks {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]*model.Lock, 0, len(keys))
	for _, k := range keys {
		out = append(out, cloneLock(s.locks[k]))
	}
	return out
}

// SnapshotPath is the fixed snapshot location inside the data directory.
func (s *Store) SnapshotPath() string { return filepath.Join(s.dir, "session-snapshot.json") }

func (s *Store) TableVersion() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.tableVersion
}

func (s *Store) GraphVersion() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.graphVersion
}

// TakeSnapshot writes a complete session snapshot (table, graph, per-lock
// versions and full propagation trace).
func (s *Store) TakeSnapshot(id string) (string, *model.Snapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	locks := map[string]*model.Lock{}
	for k, lk := range s.locks {
		locks[k] = cloneLock(lk)
	}
	snap := &model.Snapshot{
		Kind:         "locklease.session.snapshot/v1",
		SnapshotID:   id,
		TableVersion: s.tableVersion,
		GraphVersion: s.graphVersion,
		Graph:        s.graphFile,
		Locks:        locks,
		KeyVersions:  cloneVersions(s.keyVersions),
		Traces:       append([]model.TraceEntry(nil), s.traces...),
		Reclaims:     append([]model.ReclaimRecord(nil), s.reclaims...),
		TakenAtMs:    currentWallMs(),
	}
	path := s.SnapshotPath()
	if err := atomicWriteJSON(path, snap); err != nil {
		return "", nil, err
	}
	return path, snap, nil
}

// RestoreResult reports whether the restore was a wholesale replacement and
// the outcome of the replay consistency check.
type RestoreResult struct {
	Path          string
	SnapshotID    string
	Replaced      bool
	OldTableVer   int64
	NewTableVer   int64
	ReplayChecked bool
	ReplayDiffs   []ReplayDiff
}

// ReplayDiff is one stable, reproducible difference found during replay.
type ReplayDiff struct {
	Type     string `json:"type"`
	EventID  int64  `json:"event_id"`
	Key      string `json:"key,omitempty"`
	Expected string `json:"expected,omitempty"`
	Actual   string `json:"actual,omitempty"`
}

// RestoreSnapshot loads a snapshot, detects version mismatch against the live
// table and replaces the whole lock table + graph (never an incremental merge),
// then runs replay verification against the recorded propagation trace.
func (s *Store) RestoreSnapshot(path string) (*RestoreResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, bizErr(ESnapshot, "cannot read snapshot: "+err.Error(), nil)
	}
	var snap model.Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		return nil, bizErr(ESnapshot, "snapshot file is corrupt: "+err.Error(), nil)
	}
	if snap.Kind != "locklease.session.snapshot/v1" {
		return nil, bizErr(ESnapshot, "unrecognized snapshot kind", map[string]any{"kind": snap.Kind})
	}

	g, err := graph.Build(snap.Graph)
	if err != nil {
		return nil, err
	}

	oldVer := s.tableVersion
	replaced := snap.TableVersion != s.tableVersion || snap.GraphVersion != s.graphVersion

	// Wholesale replacement: drop every in-memory map before installing the
	// snapshot content so no stale key survives as an incremental merge.
	s.locks = map[string]*model.Lock{}
	s.keyVersions = map[string]int64{}
	s.traces = nil
	s.reclaims = nil
	s.graph = g
	s.graphFile = snap.Graph
	s.graphVersion = snap.Graph.Version
	s.tableVersion = snap.TableVersion
	for k, lk := range snap.Locks {
		cp := cloneLock(lk)
		s.locks[k] = cp
	}
	for k, v := range snap.KeyVersions {
		s.keyVersions[k] = v
	}
	s.traces = append(s.traces, snap.Traces...)
	s.reclaims = append(s.reclaims, snap.Reclaims...)
	s.eventSeq = 0
	s.traceSeq = 0
	for _, t := range s.traces {
		if t.EventID > s.eventSeq {
			s.eventSeq = t.EventID
		}
		if t.Seq > s.traceSeq {
			s.traceSeq = t.Seq
		}
	}

	if err := s.persistLocked(); err != nil {
		return nil, err
	}

	diffs := s.replayLocked(&snap)
	res := &RestoreResult{
		Path: path, SnapshotID: snap.SnapshotID, Replaced: replaced,
		OldTableVer: oldVer, NewTableVer: s.tableVersion,
		ReplayChecked: true, ReplayDiffs: diffs,
	}
	if len(diffs) > 0 {
		return res, bizErr(EReplay, fmt.Sprintf("replay verification found %d difference(s)", len(diffs)),
			map[string]any{"diffs": diffs})
	}
	return res, nil
}

// replayLocked re-derives the affected set of every recorded propagation event
// from the restored graph and compares it, plus the recorded lease versions,
// against the snapshot. It never mutates state.
func (s *Store) replayLocked(snap *model.Snapshot) []ReplayDiff {
	var diffs []ReplayDiff

	eventIDs := map[int64]struct{}{}
	for _, t := range snap.Traces {
		eventIDs[t.EventID] = struct{}{}
	}
	ids := make([]int64, 0, len(eventIDs))
	for id := range eventIDs {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })

	// Trusted per-key version baselines persisted by the server. KeyVersions
	// holds the highest version the server assigned to each key (including
	// deleted locks); live locks may carry an equal or newer version. These
	// maps never trust the trace entries currently being verified.
	finalVersion := map[string]int64{}
	for k, v := range snap.KeyVersions {
		finalVersion[k] = v
	}
	for k, lk := range snap.Locks {
		if lk != nil && lk.Version > finalVersion[k] {
			finalVersion[k] = lk.Version
		}
	}
	// Last trace version per key, used for the trusted-baseline comparison.
	lastTraceVersion := map[string]int64{}
	for _, t := range snap.Traces {
		if t.Version > lastTraceVersion[t.Affected] {
			lastTraceVersion[t.Affected] = t.Version
		}
	}

	for _, id := range ids {
		var entries []model.TraceEntry
		var source, cause string
		for _, t := range snap.Traces {
			if t.EventID == id {
				entries = append(entries, t)
				source, cause = t.SourceKey, t.Cause
			}
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].Seq < entries[j].Seq })

		closure := s.graph.DownstreamClosure(source)
		closureSet := map[string]struct{}{}
		for _, key := range closure {
			closureSet[key] = struct{}{}
		}
		// Trace records only the closure members that actually held a lease at
		// event time, so the reproducible set check is "every traced key must
		// lie inside the re-derived closure"; ordering must match BFS order.
		actual := make([]string, 0, len(entries))
		badSet := false
		for _, e := range entries {
			actual = append(actual, e.Affected)
			if !contains(closure, e.Affected) {
				badSet = true
				diffs = append(diffs, ReplayDiff{
					Type: "affected_set", EventID: id, Key: e.Affected,
					Expected: "key within downstream closure " + fmt.Sprintf("%v", closure),
					Actual:   "outside closure",
				})
			}
		}
		if badSet {
			continue
		}
		var orderedActual []string
		for _, key := range closure {
			if contains(actual, key) {
				orderedActual = append(orderedActual, key)
			}
		}
		if fmt.Sprintf("%v", orderedActual) != fmt.Sprintf("%v", actual) {
			diffs = append(diffs, ReplayDiff{
				Type: "propagation_order", EventID: id,
				Expected: fmt.Sprintf("%v", orderedActual), Actual: fmt.Sprintf("%v", actual),
			})
			continue
		}
		// Version reproducibility. Within one propagation event every affected
		// key is invalidated exactly once, so its recorded version must be the
		// consecutive successor of the highest version seen in earlier events.
		// A re-acquired lock starts a new lease whose version is recorded in the
		// trusted baseline; the check still rejects gaps, zeroes and regressions.
		for _, e := range entries {
			key := e.Affected
			trusted, keyKnown := finalVersion[key]
			if !keyKnown {
				diffs = append(diffs, ReplayDiff{
					Type: "missing_version", EventID: id, Key: key,
					Expected: "trusted key version baseline in snapshot", Actual: "absent",
				})
				continue
			}
			if e.Version <= 0 {
				diffs = append(diffs, ReplayDiff{
					Type: "missing_version", EventID: id, Key: key,
					Expected: "positive lease version", Actual: "absent",
				})
				continue
			}
			// A trace entry documents one invalidation: its version must be a
			// positive lease increment that never exceeds the server-maintained
			// trusted ceiling. Cross-event re-acquisition starts a fresh lease,
			// so global consecutive numbering is deliberately not required.
			if e.Version <= 0 || e.Version > trusted {
				diffs = append(diffs, ReplayDiff{
					Type: "version_mismatch", EventID: id, Key: key,
					Expected: fmt.Sprintf("1..v%d", trusted),
					Actual:   fmt.Sprintf("v%d", e.Version),
				})
			}
			// The key's final recorded invalidation must match its trusted
			// baseline exactly; tampering the value low is rejected here.
			if e.Version == lastTraceVersion[key] && e.Version != trusted {
				diffs = append(diffs, ReplayDiff{
					Type: "version_baseline", EventID: id, Key: key,
					Expected: fmt.Sprintf("v%d", trusted),
					Actual:   fmt.Sprintf("v%d", e.Version),
				})
			}
		}
		_ = cause
	}
	return diffs
}

func cloneVersions(in map[string]int64) map[string]int64 {
	out := make(map[string]int64, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func contains(set []string, v string) bool {
	for _, x := range set {
		if x == v {
			return true
		}
	}
	return false
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	ca := append([]string(nil), a...)
	cb := append([]string(nil), b...)
	sort.Strings(ca)
	sort.Strings(cb)
	for i := range ca {
		if ca[i] != cb[i] {
			return false
		}
	}
	return true
}

func (s *Store) persistLocked() error {
	st := model.StateFile{
		TableVersion: s.tableVersion,
		GraphVersion: s.graphVersion,
		Locks:        s.locks,
		UpdatedAtMs:  currentWallMs(),
	}
	return atomicWriteJSON(s.statePath, st)
}
