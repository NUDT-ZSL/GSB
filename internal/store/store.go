package store

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"locklease/internal/graph"
	"locklease/internal/model"
)

// Business error codes shared by the TCP protocol layer.
const (
	EDepend     = "E_DEPEND"
	ELocked     = "E_LOCKED"
	EVersion    = "E_VERSION"
	ENotFound   = "E_NOT_FOUND"
	ESnapshot   = "E_SNAPSHOT"
	EReplay     = "E_REPLAY"
	EBadRequest = "E_BAD_REQUEST"
)

// BizError is a stable, code-bearing business error.
type BizError struct {
	Code    string
	Message string
	Detail  map[string]any
}

func (e *BizError) Error() string {
	if e.Detail != nil {
		return fmt.Sprintf("%s: %s %v", e.Code, e.Message, e.Detail)
	}
	return fmt.Sprintf("%s: %s", e.Code, e.Message)
}

func bizErr(code, msg string, detail map[string]any) *BizError {
	return &BizError{Code: code, Message: msg, Detail: detail}
}

// Store is the single authoritative, mutex-serialized lock state machine.
type Store struct {
	mu sync.Mutex

	dir       string
	statePath string
	graphPath string

	graph        *graph.Graph
	graphFile    model.GraphFile
	locks        map[string]*model.Lock
	keyVersions  map[string]int64
	tableVersion int64
	graphVersion int64
	traces       []model.TraceEntry
	reclaims     []model.ReclaimRecord
	eventSeq     int64
	traceSeq     int64
}

// New creates a store rooted at dataDir and loads/validates the graph file.
func New(dataDir, graphPath string) (*Store, error) {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return nil, err
	}
	s := &Store{
		dir:         dataDir,
		statePath:   filepath.Join(dataDir, "lock-state.json"),
		graphPath:   graphPath,
		locks:       map[string]*model.Lock{},
		keyVersions: map[string]int64{},
	}
	if err := s.loadGraph(graphPath); err != nil {
		return nil, err
	}
	if err := s.loadState(); err != nil {
		return nil, err
	}
	return s, nil
}

func atomicWriteJSON(path string, v any) error {
	tmp := path + ".tmp"
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (s *Store) loadGraph(path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var gf model.GraphFile
	if err := json.Unmarshal(raw, &gf); err != nil {
		return bizErr(EDepend, "graph file is not valid JSON: "+err.Error(), nil)
	}
	g, err := graph.Build(gf)
	if err != nil {
		return err
	}
	s.graph = g
	s.graphFile = gf
	s.graphVersion = gf.Version
	return nil
}

func (s *Store) loadState() error {
	raw, err := os.ReadFile(s.statePath)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var st model.StateFile
	if err := json.Unmarshal(raw, &st); err != nil {
		return bizErr(ESnapshot, "state file is corrupt: "+err.Error(), nil)
	}
	if st.Locks == nil {
		st.Locks = map[string]*model.Lock{}
	}
	for key, lk := range st.Locks {
		if !s.graph.Has(key) {
			return bizErr(EDepend, "state references unknown lock key "+key, nil)
		}
		for _, d := range lk.DependsOn {
			if !s.graph.Has(d) {
				return bizErr(EDepend, fmt.Sprintf("state lock %s depends on unknown key %s", key, d), nil)
			}
		}
	}
	s.locks = st.Locks
	s.tableVersion = st.TableVersion
	s.graphVersion = st.GraphVersion
	return nil
}
