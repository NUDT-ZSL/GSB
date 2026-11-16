package snapshot

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"localmesh/internal/biz"
	"localmesh/internal/model"
)

type Store struct {
	Dir string
}

func New(dir string) *Store {
	if abs, err := filepath.Abs(dir); err == nil {
		dir = abs
	}
	return &Store{Dir: dir}
}

func (s *Store) Ensure() error {
	if err := os.MkdirAll(s.Dir, 0o755); err != nil {
		return biz.Wrap(biz.CodeSnapshot, "mkdir_snapshot", "cannot create snapshot directory", err)
	}
	if stale, err := filepath.Glob(filepath.Join(s.Dir, ".meshctl-*.tmp")); err == nil {
		for _, path := range stale {
			_ = os.Remove(path)
		}
	}
	return nil
}

func (s *Store) TracePath(roundID, versionSlug string) string {
	return filepath.Join(s.Dir, fmt.Sprintf("trace-%s-%s.jsonl", safe(roundID), safe(versionSlug)))
}

func (s *Store) SnapshotPath(roundID, versionSlug string) string {
	return filepath.Join(s.Dir, fmt.Sprintf("snapshot-%s-%s.json", safe(roundID), safe(versionSlug)))
}

func (s *Store) RecoveredSnapshotPath(roundID string) string {
	return filepath.Join(s.Dir, fmt.Sprintf("snapshot-%s-recovered.json", safe(roundID)))
}

func (s *Store) CheckpointPath(roundID string) string {
	return filepath.Join(s.Dir, fmt.Sprintf("checkpoint-%s.json", safe(roundID)))
}

func (s *Store) CurrentSnapshotPath() string { return filepath.Join(s.Dir, "current.json") }
func (s *Store) CurrentCheckpointPath() string {
	return filepath.Join(s.Dir, "checkpoint-current.json")
}

func (s *Store) WriteDocument(doc *model.Document, updateCurrent bool) (string, error) {
	path := s.SnapshotPath(doc.Round.ID, slugVersion(doc.Round.RulesVersion))
	raw, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return "", biz.Wrap(biz.CodeSnapshot, "encode_snapshot", "cannot encode snapshot", err)
	}
	raw = append(raw, '\n')
	if err := writeAtomic(path, raw); err != nil {
		return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_snapshot", "cannot write snapshot", err), map[string]any{"path": path})
	}
	if updateCurrent {
		if err := writeAtomic(s.CurrentSnapshotPath(), raw); err != nil {
			return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_current_snapshot", "cannot replace current snapshot", err), map[string]any{"path": s.CurrentSnapshotPath()})
		}
	}
	return path, nil
}

func (s *Store) WriteRecoveredDocument(doc *model.Document) (string, error) {
	path := s.RecoveredSnapshotPath(doc.Round.ID)
	raw, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return "", biz.Wrap(biz.CodeSnapshot, "encode_recovered_snapshot", "cannot encode recovered snapshot", err)
	}
	raw = append(raw, '\n')
	if err := writeAtomic(path, raw); err != nil {
		return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_recovered_snapshot", "cannot write recovered snapshot", err), map[string]any{"path": path})
	}
	if err := writeAtomic(s.CurrentSnapshotPath(), raw); err != nil {
		return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_current_snapshot", "cannot replace current snapshot", err), map[string]any{"path": s.CurrentSnapshotPath()})
	}
	return path, nil
}

func (s *Store) WriteTrace(roundID, versionSlug string, events []model.TraceEvent) (string, error) {
	path := s.TracePath(roundID, versionSlug)
	ordered := append([]model.TraceEvent(nil), events...)
	sort.SliceStable(ordered, func(i, j int) bool {
		a, b := ordered[i], ordered[j]
		if a.RequestID != b.RequestID {
			return a.RequestID < b.RequestID
		}
		if a.AttemptNo != b.AttemptNo {
			return a.AttemptNo < b.AttemptNo
		}
		ra, rb := eventRank(a.Type), eventRank(b.Type)
		if ra != rb {
			return ra < rb
		}
		if a.InstanceID != b.InstanceID {
			return a.InstanceID < b.InstanceID
		}
		return a.MonoNS < b.MonoNS
	})
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	for i := range ordered {
		if err := enc.Encode(&ordered[i]); err != nil {
			return "", biz.Wrap(biz.CodeSnapshot, "encode_trace", "cannot encode trace", err)
		}
	}
	if err := writeAtomic(path, buf.Bytes()); err != nil {
		return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_trace", "cannot write trace", err), map[string]any{"path": path})
	}
	return path, nil
}

func (s *Store) SaveCheckpoint(cp *model.Checkpoint) (string, error) {
	path := s.CheckpointPath(cp.RoundID)
	raw, err := json.MarshalIndent(cp, "", "  ")
	if err != nil {
		return "", biz.Wrap(biz.CodeSnapshot, "encode_checkpoint", "cannot encode checkpoint", err)
	}
	raw = append(raw, '\n')
	if err := writeAtomic(path, raw); err != nil {
		return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_checkpoint", "cannot write checkpoint", err), map[string]any{"path": path})
	}
	if err := writeAtomic(s.CurrentCheckpointPath(), raw); err != nil {
		return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_current_checkpoint", "cannot replace current checkpoint", err), map[string]any{"path": s.CurrentCheckpointPath()})
	}
	return path, nil
}

func (s *Store) LoadDocument(path string) (*model.Document, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeSnapshot, "read_snapshot", "cannot read snapshot", err), "file")
	}
	var doc model.Document
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeSnapshot, "parse_snapshot", "invalid snapshot JSON", err), "file")
	}
	return &doc, nil
}

func (s *Store) LoadCheckpoint(path string) (*model.Checkpoint, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeSnapshot, "read_checkpoint", "cannot read checkpoint", err), "file")
	}
	var cp model.Checkpoint
	if err := json.Unmarshal(raw, &cp); err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeSnapshot, "parse_checkpoint", "invalid checkpoint JSON", err), "file")
	}
	return &cp, nil
}

// LoadTrace accepts either JSON Lines (the native format) or a JSON array.
func (s *Store) LoadTrace(path string) ([]model.TraceEvent, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeVerify, "read_trace", "cannot read trace", err), "file")
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, biz.WithField(biz.New(biz.CodeVerify, "parse_trace", "trace is empty"), "file")
	}
	trimmed := bytes.TrimSpace(raw)
	if trimmed[0] == '[' {
		var events []model.TraceEvent
		if err := json.Unmarshal(trimmed, &events); err != nil {
			return nil, biz.WithField(biz.Wrap(biz.CodeVerify, "parse_trace", "invalid trace JSON array", err), "file")
		}
		return events, nil
	}
	var events []model.TraceEvent
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	line := 0
	for scanner.Scan() {
		line++
		b := bytes.TrimSpace(scanner.Bytes())
		if len(b) == 0 {
			continue
		}
		var ev model.TraceEvent
		if err := json.Unmarshal(b, &ev); err != nil {
			return nil, biz.WithField(biz.Wrap(biz.CodeVerify, "parse_trace", fmt.Sprintf("invalid trace line %d", line), err), "file")
		}
		events = append(events, ev)
	}
	if err := scanner.Err(); err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeVerify, "parse_trace", "cannot scan trace", err), "file")
	}
	return events, nil
}

func (s *Store) WriteReport(name string, report any) (string, error) {
	path := filepath.Join(s.Dir, safe(name))
	raw, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return "", biz.Wrap(biz.CodeSnapshot, "encode_report", "cannot encode report", err)
	}
	raw = append(raw, '\n')
	if err := writeAtomic(path, raw); err != nil {
		return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_report", "cannot write report", err), map[string]any{"path": path})
	}
	return path, nil
}

func (s *Store) WriteRerunRequests(roundID string, requests []model.Request) (string, error) {
	path := filepath.Join(s.Dir, fmt.Sprintf("rerun-%s.json", safe(roundID)))
	payload := struct {
		Round    string          `json:"round"`
		Requests []model.Request `json:"requests"`
	}{Round: roundID + "-retry", Requests: requests}
	raw, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return "", biz.Wrap(biz.CodeSnapshot, "encode_rerun", "cannot encode rerun requests", err)
	}
	raw = append(raw, '\n')
	if err := writeAtomic(path, raw); err != nil {
		return "", biz.WithFields(biz.Wrap(biz.CodeSnapshot, "write_rerun", "cannot write rerun requests", err), map[string]any{"path": path})
	}
	return path, nil
}

// LatestCompleted returns the most recently finished completed snapshot. It
// intentionally ignores recovered and invalidation snapshots.
func (s *Store) LatestCompleted() (*model.Document, string, error) {
	paths, err := filepath.Glob(filepath.Join(s.Dir, "snapshot-*.json"))
	if err != nil {
		return nil, "", biz.Wrap(biz.CodeSnapshot, "scan_snapshots", "cannot scan snapshots", err)
	}
	type entry struct {
		doc  *model.Document
		path string
	}
	var entries []entry
	for _, path := range paths {
		base := filepath.Base(path)
		if base == "current.json" || strings.Contains(base, "recovered") {
			continue
		}
		doc, err := s.LoadDocument(path)
		if err != nil {
			return nil, "", biz.WithFields(biz.Wrap(biz.CodeReplay, "latest_completed", "completed snapshot history is unreadable", err), map[string]any{"path": path})
		}
		if doc.Round.Status != "completed" || !doc.Round.ExitReportAvailable {
			continue
		}
		entries = append(entries, entry{doc: doc, path: path})
	}
	if len(entries) == 0 {
		return nil, "", nil
	}
	sort.Slice(entries, func(i, j int) bool {
		if entries[i].doc.Round.FinishedAt == entries[j].doc.Round.FinishedAt {
			return entries[i].path < entries[j].path
		}
		return entries[i].doc.Round.FinishedAt < entries[j].doc.Round.FinishedAt
	})
	last := entries[len(entries)-1]
	return last.doc, last.path, nil
}

func writeAtomic(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".meshctl-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() {
		_ = os.Remove(tmpName)
	}()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return err
	}
	_ = os.Chmod(path, 0o644)
	return nil
}

func eventRank(event string) int {
	switch event {
	case "request_queued":
		return 0
	case "request_start":
		return 1
	case "instance_selected":
		return 2
	case "attempt":
		return 3
	case "fast_fail":
		return 4
	case "breaker_half_open":
		return 5
	case "breaker_open":
		return 6
	case "breaker_close":
		return 7
	case "attempt_unknown":
		return 8
	case "request_unknown":
		return 9
	case "request_end":
		return 10
	default:
		return 11
	}
}

func safe(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return "unnamed"
	}
	var b strings.Builder
	for _, r := range s {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '-' || r == '_' || r == '.' {
			b.WriteRune(r)
		} else {
			b.WriteByte('-')
		}
	}
	return strings.Trim(b.String(), "-")
}

func slugVersion(version string) string {
	if i := strings.LastIndex(version, "@sha256:"); i >= 0 {
		hash := version[i+len("@sha256:"):]
		if len(hash) > 12 {
			hash = hash[:12]
		}
		declared := version[:i]
		return safe(declared) + "-" + hash
	}
	return safe(version)
}
