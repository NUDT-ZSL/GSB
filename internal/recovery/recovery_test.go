package recovery

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"localmesh/internal/biz"
	"localmesh/internal/model"
	"localmesh/internal/rules"
	"localmesh/internal/scheduler"
	"localmesh/internal/snapshot"
)

func TestRecoverMarksUnfinishedUnknownAndWritesRerun(t *testing.T) {
	dir := t.TempDir()
	store := snapshot.New(dir)
	started := time.Now().UTC().Add(-time.Second).Format(time.RFC3339Nano)
	cfg := rules.Config{
		Version: "v1", GlobalConcurrency: 2,
		Services: []rules.Service{{Name: "svc", Quota: 1, Instances: []rules.Instance{{ID: "a", Address: "127.0.0.1:19001", Weight: 1, FailureThreshold: 2, CooldownMS: 100}}}},
		Routes:   []rules.Route{{Prefix: "/x", Service: "svc", TimeoutMS: 500, Retries: 0}},
	}
	req := model.Request{ID: "done", Method: "GET", Path: "/x"}
	cp := &model.Checkpoint{
		SchemaVersion: 1,
		RoundID:       "crash",
		Status:        "running",
		StartedAt:     started,
		RulesPath:     "rules.json",
		RulesVersion:  "v1@sha256:deadbeef",
		RulesHash:     "deadbeef",
		Rules:         cfg,
		Requests: []model.RequestState{
			{Request: req, RoutePrefix: "/x", Service: "svc", TimeoutMS: 500, AttemptBudget: 1, Status: "done", Outcome: "success", Attempts: []model.Attempt{{RequestID: "done", AttemptNo: 1, InstanceID: "a", Service: "svc", Outcome: "success"}}},
			{Request: model.Request{ID: "running", Method: "GET", Path: "/x"}, RoutePrefix: "/x", Service: "svc", TimeoutMS: 500, AttemptBudget: 1, Status: "running", Outcome: "pending", CurrentAttempt: &model.Attempt{RequestID: "running", AttemptNo: 1, InstanceID: "a", Service: "svc", StartMonoNS: 10}},
			{Request: model.Request{ID: "pending", Method: "GET", Path: "/x"}, RoutePrefix: "/x", Service: "svc", TimeoutMS: 500, AttemptBudget: 1, Status: "pending", Outcome: "pending"},
		},
		ActiveGlobal:  1,
		ActiveService: map[string]int{"svc": 1},
		Quota:         model.QuotaStats{GlobalLimit: 2, GlobalPeak: 1, ServiceLimits: map[string]int{"svc": 1}, ServicePeaks: map[string]int{"svc": 1}},
		Breakers:      map[string]model.CircuitState{},
	}
	if _, err := store.SaveCheckpoint(cp); err != nil {
		t.Fatal(err)
	}
	result, err := Recover(Options{CheckpointPath: filepath.Join(dir, "checkpoint-crash.json"), Store: store})
	if err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if len(result.UnknownRequestIDs) != 2 || result.Document.Summary.Unknown != 2 || result.Document.Summary.Success != 1 {
		t.Fatalf("recovered result=%+v summary=%+v", result.UnknownRequestIDs, result.Document.Summary)
	}
	if result.Document.Round.ExitReportAvailable {
		t.Fatal("recovered document must not claim an exit report")
	}
	if result.RerunPath == "" {
		t.Fatal("missing rerun file")
	}
	if result.Document.Quota.RecoveredActiveGlobal != 1 || result.Document.Quota.RecoveredActiveService["svc"] != 1 {
		t.Fatalf("quota snapshot was not preserved: %+v", result.Document.Quota)
	}
}

func TestRecoverReplayMismatchReturnsEReplay(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))
	defer srv.Close()
	dir := t.TempDir()
	rulesPath := filepath.Join(dir, "rules.json")
	body := fmt.Sprintf(`{
      "version":"v1","global_concurrency":1,
      "services":[{"name":"svc","quota":1,"instances":[{"id":"a","address":"%s","weight":1,"failure_threshold":2,"cooldown_ms":100}]}],
      "routes":[{"prefix":"/x","service":"svc","timeout_ms":500,"retries":0}]
    }`, strings.TrimPrefix(srv.URL, "http://"))
	if err := os.WriteFile(rulesPath, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	set, err := rules.Load(rulesPath)
	if err != nil {
		t.Fatal(err)
	}
	store := snapshot.New(dir)
	result, err := scheduler.New(scheduler.Options{
		Rules: set, Requests: []model.Request{{ID: "r1", Method: "GET", Path: "/x"}}, RoundID: "completed", Store: store,
	}).Run(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Corrupt the trace so replay verification must reject it.
	if err := os.WriteFile(result.TracePath, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cp := &model.Checkpoint{RoundID: "crash", Status: "running", StartedAt: time.Now().UTC().Format(time.RFC3339Nano), Rules: set.Config, RulesVersion: set.VersionID}
	if _, err := store.SaveCheckpoint(cp); err != nil {
		t.Fatal(err)
	}
	_, err = Recover(Options{CheckpointPath: filepath.Join(dir, "checkpoint-crash.json"), Store: store})
	if biz.CodeOf(err) != biz.CodeReplay {
		t.Fatalf("expected E_REPLAY, got %v", err)
	}
}
