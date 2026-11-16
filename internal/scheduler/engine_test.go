package scheduler

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"localmesh/internal/biz"
	"localmesh/internal/diff"
	"localmesh/internal/model"
	"localmesh/internal/rules"
	"localmesh/internal/snapshot"
	"localmesh/internal/verify"
)

type serverStats struct {
	active atomic.Int64
	peak   atomic.Int64
}

func (s *serverStats) track() func() {
	now := s.active.Add(1)
	for {
		peak := s.peak.Load()
		if now <= peak || s.peak.CompareAndSwap(peak, now) {
			break
		}
	}
	return func() { s.active.Add(-1) }
}

func handler(status int, delay time.Duration, stats *serverStats) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if stats != nil {
			done := stats.track()
			defer done()
		}
		if delay > 0 {
			time.Sleep(delay)
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}
}

func address(server *httptest.Server) string {
	return strings.TrimPrefix(server.URL, "http://")
}

func writeRuleFile(t *testing.T, dir, name, body string) (*rules.RuleSet, string) {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	set, err := rules.Load(path)
	if err != nil {
		t.Fatalf("load rules: %v", err)
	}
	return set, path
}

func requests(prefix string, n int) []model.Request {
	out := make([]model.Request, n)
	for i := range out {
		out[i] = model.Request{ID: fmt.Sprintf("%s-%03d", strings.Trim(prefix, "/"), i+1), Method: "GET", Path: prefix + "/" + fmt.Sprint(i+1)}
	}
	return out
}

func runEngine(t *testing.T, set *rules.RuleSet, reqs []model.Request, round, inject string) (*Result, error, *snapshot.Store) {
	t.Helper()
	store := snapshot.New(t.TempDir())
	engine := New(Options{Rules: set, Requests: reqs, RoundID: round, Store: store, InjectFailure: inject})
	result, err := engine.Run(context.Background())
	return result, err, store
}

func assertVerified(t *testing.T, store *snapshot.Store, result *Result) {
	t.Helper()
	events, err := store.LoadTrace(result.TracePath)
	if err != nil {
		t.Fatal(err)
	}
	report := verify.Run(verify.VerifyOptions{Document: result.Document, Events: events, SnapshotPath: result.SnapshotPath, TracePath: result.TracePath})
	if !report.OK {
		t.Fatalf("verification failed: %+v", report.FirstDeviation)
	}
}

func TestConcurrentBudgetsAndVerification(t *testing.T) {
	statsA, statsB := &serverStats{}, &serverStats{}
	srvA := httptest.NewServer(handler(http.StatusOK, 35*time.Millisecond, statsA))
	defer srvA.Close()
	srvB := httptest.NewServer(handler(http.StatusOK, 35*time.Millisecond, statsB))
	defer srvB.Close()
	dir := t.TempDir()
	body := fmt.Sprintf(`{
      "version":"q1","global_concurrency":3,
      "services":[
        {"name":"a","quota":2,"instances":[{"id":"a1","address":"%s","weight":1,"failure_threshold":2,"cooldown_ms":100}]},
        {"name":"b","quota":2,"instances":[{"id":"b1","address":"%s","weight":1,"failure_threshold":2,"cooldown_ms":100}]}
      ],
      "routes":[
        {"prefix":"/a","service":"a","timeout_ms":1000,"retries":0},
        {"prefix":"/b","service":"b","timeout_ms":1000,"retries":0}
      ]
    }`, address(srvA), address(srvB))
	set, _ := writeRuleFile(t, dir, "rules.json", body)
	reqs := append(requests("/a", 8), requests("/b", 8)...)
	result, err, store := runEngine(t, set, reqs, "quota", "none")
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	assertVerified(t, store, result)
	if result.Document.Summary.Success != 16 {
		t.Fatalf("success=%d", result.Document.Summary.Success)
	}
	if result.Document.Quota.GlobalPeak > 3 || result.Document.Quota.GlobalPeak < 2 {
		t.Fatalf("global peak=%d", result.Document.Quota.GlobalPeak)
	}
	for name, peak := range result.Document.Quota.ServicePeaks {
		if peak > 2 {
			t.Fatalf("service %s peak=%d exceeds quota", name, peak)
		}
	}
	if statsA.peak.Load() > 2 || statsB.peak.Load() > 2 {
		t.Fatalf("upstream observed quota violation: a=%d b=%d", statsA.peak.Load(), statsB.peak.Load())
	}
}

func TestRetryBudgetAndBreakerFastFail(t *testing.T) {
	srv := httptest.NewServer(handler(http.StatusInternalServerError, 5*time.Millisecond, nil))
	defer srv.Close()
	dir := t.TempDir()
	body := fmt.Sprintf(`{
      "version":"retry","global_concurrency":1,
      "services":[{"name":"svc","quota":1,"instances":[{"id":"bad","address":"%s","weight":1,"failure_threshold":10,"cooldown_ms":200}]}],
      "routes":[{"prefix":"/retry","service":"svc","timeout_ms":500,"retries":2}]
    }`, address(srv))
	set, _ := writeRuleFile(t, dir, "rules.json", body)
	result, err, store := runEngine(t, set, requests("/retry", 1), "retry", "none")
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	assertVerified(t, store, result)
	if got := len(result.Document.Requests[0].Attempts); got != 3 {
		t.Fatalf("attempts=%d, want retry budget 3", got)
	}
	if result.Document.CircuitEvents != nil {
		t.Fatalf("breaker opened before threshold: %+v", result.Document.CircuitEvents)
	}

	body = fmt.Sprintf(`{
      "version":"open","global_concurrency":1,
      "services":[{"name":"svc","quota":1,"instances":[{"id":"bad","address":"%s","weight":1,"failure_threshold":1,"cooldown_ms":10000}]}],
      "routes":[{"prefix":"/open","service":"svc","timeout_ms":500,"retries":4}]
    }`, address(srv))
	set, _ = writeRuleFile(t, dir, "rules-open.json", body)
	result, err, store = runEngine(t, set, requests("/open", 1), "open", "none")
	if err != nil {
		t.Fatalf("Run open: %v", err)
	}
	assertVerified(t, store, result)
	req := result.Document.Requests[0]
	if len(req.Attempts) != 1 || req.ErrorCode != "E_CIRCUIT_OPEN" {
		t.Fatalf("fast fail result=%+v", req)
	}
	if result.Document.Summary.BreakerTriggers != 1 {
		t.Fatalf("breaker triggers=%d", result.Document.Summary.BreakerTriggers)
	}
}

func TestInjectedFailureCodesStillWriteSnapshot(t *testing.T) {
	srv := httptest.NewServer(handler(http.StatusOK, time.Millisecond, nil))
	defer srv.Close()
	dir := t.TempDir()
	body := fmt.Sprintf(`{
      "version":"inject","global_concurrency":1,
      "services":[{"name":"svc","quota":1,"instances":[{"id":"ok","address":"%s","weight":1,"failure_threshold":5,"cooldown_ms":100}]}],
      "routes":[{"prefix":"/i","service":"svc","timeout_ms":500,"retries":0}]
    }`, address(srv))
	set, _ := writeRuleFile(t, dir, "rules.json", body)
	result, err, store := runEngine(t, set, requests("/i", 1), "upstream", "upstream")
	if biz.CodeOf(err) != biz.CodeUpstream || result == nil {
		t.Fatalf("expected E_UPSTREAM with snapshot, got result=%v err=%v", result, err)
	}
	assertVerified(t, store, result)
	if result.Document.Requests[0].Attempts[0].InjectedFault != "upstream_unreachable" {
		t.Fatalf("injection marker missing: %+v", result.Document.Requests[0].Attempts[0])
	}

	result, err, store = runEngine(t, set, requests("/i", 1), "snapshot", "snapshot")
	if biz.CodeOf(err) != biz.CodeSnapshot || result != nil {
		t.Fatalf("expected E_SNAPSHOT, result=%v err=%v", result, err)
	}
	matches, _ := filepath.Glob(filepath.Join(store.Dir, "snapshot-snapshot-*.json"))
	if len(matches) != 0 {
		t.Fatalf("injected snapshot failure wrote %v", matches)
	}
}

func TestRuleVersionReplacementRetainsOldSnapshot(t *testing.T) {
	srv := httptest.NewServer(handler(http.StatusOK, 2*time.Millisecond, nil))
	defer srv.Close()
	dir := t.TempDir()
	v1 := fmt.Sprintf(`{
      "version":"v1","global_concurrency":1,
      "services":[{"name":"svc","quota":1,"instances":[{"id":"old","address":"%s","weight":1,"failure_threshold":2,"cooldown_ms":100}]}],
      "routes":[{"prefix":"/v","service":"svc","timeout_ms":500,"retries":0}]
    }`, address(srv))
	set, path := writeRuleFile(t, dir, "rules.json", v1)
	result1, err, store := runEngine(t, set, requests("/v", 1), "r1", "none")
	if err != nil {
		t.Fatal(err)
	}
	v2 := fmt.Sprintf(`{
      "version":"v2","global_concurrency":2,
      "services":[{"name":"svc","quota":1,"instances":[{"id":"new","address":"%s","weight":2,"failure_threshold":3,"cooldown_ms":200}]}],
      "routes":[{"prefix":"/v","service":"svc","timeout_ms":700,"retries":1}]
    }`, address(srv))
	if err := os.WriteFile(path, []byte(v2), 0o644); err != nil {
		t.Fatal(err)
	}
	set2, err := rules.Load(path)
	if err != nil {
		t.Fatal(err)
	}
	result2, err, store := runEngineWithStore(t, set2, requests("/v", 1), "r2", store)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(result1.SnapshotPath); err != nil {
		t.Fatalf("old snapshot was removed: %v", err)
	}
	current, err := store.LoadDocument(store.CurrentSnapshotPath())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(current.Round.RulesVersion, "v2") {
		t.Fatalf("current snapshot version=%s", current.Round.RulesVersion)
	}
	report := diff.Compare(result1.Document, result2.Document)
	if !report.VersionChanged || len(report.ChangedRoutes) != 1 || len(report.ChangedInstances) != 2 {
		t.Fatalf("diff=%+v", report)
	}
}

func runEngineWithStore(t *testing.T, set *rules.RuleSet, reqs []model.Request, round string, store *snapshot.Store) (*Result, error, *snapshot.Store) {
	t.Helper()
	result, err := New(Options{Rules: set, Requests: reqs, RoundID: round, Store: store, InjectFailure: "none"}).Run(context.Background())
	return result, err, store
}

func TestRulesChangedDuringRoundIsRejected(t *testing.T) {
	srv := httptest.NewServer(handler(http.StatusOK, 80*time.Millisecond, nil))
	defer srv.Close()
	dir := t.TempDir()
	body := fmt.Sprintf(`{
      "version":"run-v1","global_concurrency":1,
      "services":[{"name":"svc","quota":1,"instances":[{"id":"ok","address":"%s","weight":1,"failure_threshold":2,"cooldown_ms":100}]}],
      "routes":[{"prefix":"/slow","service":"svc","timeout_ms":500,"retries":0}]
    }`, address(srv))
	set, path := writeRuleFile(t, dir, "rules.json", body)
	store := snapshot.New(t.TempDir())
	errCh := make(chan error, 1)
	go func() {
		_, err := New(Options{Rules: set, Requests: requests("/slow", 1), RoundID: "changed", Store: store}).Run(context.Background())
		errCh <- err
	}()
	time.Sleep(20 * time.Millisecond)
	if err := os.WriteFile(path, []byte(strings.Replace(body, `"run-v1"`, `"run-v2"`, 1)), 0o644); err != nil {
		t.Fatal(err)
	}
	err := <-errCh
	if biz.CodeOf(err) != biz.CodeRule {
		t.Fatalf("expected E_RULE, got %v", err)
	}
	if _, statErr := os.Stat(store.CurrentSnapshotPath()); !os.IsNotExist(statErr) {
		t.Fatalf("mixed round replaced current snapshot: %v", statErr)
	}
}
