package engine

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
)

type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func newFakeClock() *fakeClock {
	return &fakeClock{now: time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)}
}

func (f *fakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *fakeClock) Advance(delta time.Duration) {
	f.mu.Lock()
	f.now = f.now.Add(delta)
	f.mu.Unlock()
}

func testOptions(t *testing.T, dir string, clock Clock) Options {
	t.Helper()
	return Options{
		DBPath:               filepath.Join(dir, "queue.db"),
		RecoveryReportPath:   filepath.Join(dir, "recovery.json"),
		Clock:                clock,
		MaxRetries:           3,
		DefaultWindowTimeout: 2 * time.Second,
		MaxBodyBytes:         32,
		DisableSweeper:       true,
	}
}

func openTestEngine(t *testing.T, opts Options) *Engine {
	t.Helper()
	engine, err := Open(context.Background(), opts)
	if err != nil {
		t.Fatalf("open engine: %v", err)
	}
	t.Cleanup(func() { _ = engine.Close() })
	return engine
}

func mustAppend(t *testing.T, engine *Engine, partition, producer string, seq int64, body string) AppendResult {
	t.Helper()
	result, err := engine.Append(context.Background(), "append", partition, producer, seq, body)
	if err != nil {
		t.Fatalf("append %s/%d: %v", partition, seq, err)
	}
	return result
}

func bizCode(err error) string {
	var biz *protocol.BizError
	if errors.As(err, &biz) {
		return biz.Code
	}
	return ""
}

func countEvents(t *testing.T, db *sql.DB, partition, kind string) int {
	t.Helper()
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM events WHERE partition_key=? AND kind=?`, partition, kind).Scan(&count); err != nil {
		t.Fatalf("count events: %v", err)
	}
	return count
}

func TestAppendValidationIsAtomic(t *testing.T) {
	engine := openTestEngine(t, testOptions(t, t.TempDir(), newFakeClock()))
	ctx := context.Background()
	if _, err := engine.Append(ctx, "bad-key", "bad key", "p", 1, "x"); err == nil || err.Code != protocol.CodeAppend {
		t.Fatalf("expected E_APPEND for invalid key, got %#v", err)
	}
	if _, err := engine.Append(ctx, "bad-seq", "p1", "p", 0, "x"); err == nil || err.Code != protocol.CodeAppend {
		t.Fatalf("expected E_APPEND for invalid sequence, got %#v", err)
	}
	if _, err := engine.Append(ctx, "bad-body", "p1", "p", 1, "0123456789abcdef0123456789abcdefX"); err == nil || err.Code != protocol.CodeAppend {
		t.Fatalf("expected E_APPEND for oversized body, got %#v", err)
	}
	first := mustAppend(t, engine, "bad-body", "p", 1, "one")
	if first.Offset != 1 || first.NextOffset != 2 {
		t.Fatalf("unexpected first offset: %#v", first)
	}
	if _, err := engine.Append(ctx, "duplicate", "bad-body", "p", 1, "duplicate"); err == nil || err.Code != protocol.CodeAppend {
		t.Fatalf("expected duplicate producer sequence rejection, got %#v", err)
	}
	second := mustAppend(t, engine, "bad-body", "p", 2, "two")
	if second.Offset != 2 || second.NextOffset != 3 {
		t.Fatalf("unexpected second offset: %#v", second)
	}
	status, err := engine.Status(ctx)
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	if len(status.Partitions) != 1 || status.Partitions[0].NextOffset != 3 {
		t.Fatalf("invalid append was not supposed to consume an offset: %#v", status.Partitions)
	}
}

func TestFenceCommitAndWindowTightening(t *testing.T) {
	engine := openTestEngine(t, testOptions(t, t.TempDir(), newFakeClock()))
	ctx := context.Background()
	for seq := int64(1); seq <= 5; seq++ {
		mustAppend(t, engine, "orders", "producer-a", seq, "m")
	}
	first, err := engine.Pull(ctx, "pull-1", "orders", "consumer-a", 3, 5000)
	if err != nil {
		t.Fatalf("first pull: %v", err)
	}
	if len(first.Messages) != 3 || first.Messages[0].Offset != 1 || first.Messages[2].Offset != 3 {
		t.Fatalf("unexpected pull result: %#v", first)
	}
	if _, err := engine.Pull(ctx, "pull-2", "orders", "consumer-b", 2, 5000); err == nil || err.Code != protocol.CodeFence || err.Fields["holder"] != "consumer-a" {
		t.Fatalf("expected E_FENCE with holder, got %#v", err)
	}
	committed, err := engine.Commit(ctx, "commit-1", "orders", "consumer-a", 1)
	if err != nil {
		t.Fatalf("commit partial: %v", err)
	}
	if committed.Window == nil || committed.Window.StartOffset != 2 || committed.Window.EndOffset != 3 {
		t.Fatalf("window did not tighten: %#v", committed)
	}
	committed, err = engine.Commit(ctx, "commit-3", "orders", "consumer-a", 3)
	if err != nil {
		t.Fatalf("commit to window end: %v", err)
	}
	if committed.Window != nil || committed.CommittedOffset != 3 {
		t.Fatalf("window was not released: %#v", committed)
	}
	second, err := engine.Pull(ctx, "pull-3", "orders", "consumer-b", 3, 5000)
	if err != nil {
		t.Fatalf("second pull: %v", err)
	}
	if second.Window.StartOffset != 4 || second.Window.EndOffset != 5 {
		t.Fatalf("unexpected second window: %#v", second.Window)
	}
	if _, err := engine.Commit(ctx, "commit-jump", "orders", "consumer-b", 7); err == nil || err.Code != protocol.CodeCommit {
		t.Fatalf("expected E_COMMIT for out-of-window commit, got %#v", err)
	}
	if _, err := engine.Commit(ctx, "commit-back", "orders", "consumer-b", 3); err == nil || err.Code != protocol.CodeCommit {
		t.Fatalf("expected E_COMMIT for backwards commit, got %#v", err)
	}
	if _, err := engine.Commit(ctx, "commit-4", "orders", "consumer-b", 4); err != nil {
		t.Fatalf("commit 4: %v", err)
	}
	if _, err := engine.Commit(ctx, "commit-5", "orders", "consumer-b", 5); err != nil {
		t.Fatalf("commit 5: %v", err)
	}
}

func TestFailureRedeliversFromEarliestUnconfirmed(t *testing.T) {
	engine := openTestEngine(t, testOptions(t, t.TempDir(), newFakeClock()))
	ctx := context.Background()
	for seq := int64(1); seq <= 5; seq++ {
		mustAppend(t, engine, "ordered", "producer", seq, "m")
	}
	if _, err := engine.Pull(ctx, "p1", "ordered", "c1", 4, 5000); err != nil {
		t.Fatalf("pull: %v", err)
	}
	if _, err := engine.Commit(ctx, "c1", "ordered", "c1", 1); err != nil {
		t.Fatalf("commit 1: %v", err)
	}
	if _, err := engine.Fail(ctx, "f1", "ordered", "c1", 3, "injected"); err != nil {
		t.Fatalf("fail: %v", err)
	}
	replayed, err := engine.Pull(ctx, "p2", "ordered", "c2", 3, 5000)
	if err != nil {
		t.Fatalf("redelivery pull: %v", err)
	}
	if replayed.Messages[0].Offset != 2 || replayed.Messages[1].Offset != 3 || replayed.Messages[2].Offset != 4 {
		t.Fatalf("redelivery skipped an earlier unconfirmed offset: %#v", replayed.Messages)
	}
	if replayed.Messages[0].RetryCount != 1 || replayed.Messages[2].RetryCount != 1 {
		t.Fatalf("retry counters were not recorded per offset: %#v", replayed.Messages)
	}
	if _, err := engine.Commit(ctx, "c2", "ordered", "c2", 4); err != nil {
		t.Fatalf("commit redelivered prefix: %v", err)
	}
	status, err := engine.Status(ctx)
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	partition := status.Partitions[0]
	if partition.CommittedOffset != 4 || partition.ConfirmedOffset != 4 || partition.RetryCounts["2"] != 1 || partition.RetryCounts["3"] != 1 {
		t.Fatalf("unexpected ledger: %#v", partition)
	}
}

func TestTimeoutAndCommitHaveSingleWinner(t *testing.T) {
	clock := newFakeClock()
	engine := openTestEngine(t, testOptions(t, t.TempDir(), clock))
	ctx := context.Background()
	mustAppend(t, engine, "timeout", "producer", 1, "m")
	mustAppend(t, engine, "timeout", "producer", 2, "m")
	if _, err := engine.Pull(ctx, "p1", "timeout", "c1", 2, 100); err != nil {
		t.Fatalf("pull: %v", err)
	}
	clock.Advance(99 * time.Millisecond)
	if _, err := engine.Commit(ctx, "c1", "timeout", "c1", 1); err != nil {
		t.Fatalf("commit before deadline: %v", err)
	}
	if countEvents(t, engine.store.DB(), "timeout", "timeout") != 0 {
		t.Fatalf("commit before deadline should not produce a timeout event")
	}
	clock.Advance(2 * time.Second)
	if _, err := engine.Commit(ctx, "c2", "timeout", "c1", 2); err == nil || err.Code != protocol.CodeFence {
		t.Fatalf("expected timeout to win the commit race, got %#v", err)
	}
	if countEvents(t, engine.store.DB(), "timeout", "timeout") != 1 {
		t.Fatalf("expected exactly one timeout event")
	}
	if countEvents(t, engine.store.DB(), "timeout", "commit") != 1 {
		t.Fatalf("timeout must not create a second commit event")
	}
	status, err := engine.Status(ctx)
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	if status.Partitions[0].Window != nil || status.Partitions[0].RetryCounts["2"] != 1 {
		t.Fatalf("timeout state not reflected in ledger: %#v", status.Partitions[0])
	}
}

func TestPoisonBlocksAndManualUnblockResets(t *testing.T) {
	opts := testOptions(t, t.TempDir(), newFakeClock())
	opts.MaxRetries = 1
	engine := openTestEngine(t, opts)
	ctx := context.Background()
	for seq := int64(1); seq <= 3; seq++ {
		mustAppend(t, engine, "poison", "producer", seq, "m")
	}
	if _, err := engine.Pull(ctx, "p1", "poison", "c1", 3, 5000); err != nil {
		t.Fatalf("pull: %v", err)
	}
	if _, err := engine.Fail(ctx, "f1", "poison", "c1", 1, "first"); err != nil {
		t.Fatalf("first failure: %v", err)
	}
	if _, err := engine.Pull(ctx, "p2", "poison", "c2", 3, 5000); err != nil {
		t.Fatalf("second pull: %v", err)
	}
	_, err := engine.Fail(ctx, "f2", "poison", "c2", 1, "second")
	if err == nil || err.Code != protocol.CodePoison || err.Fields["blocked_offset"] != int64(1) || err.Fields["blocked_retry_count"] != 2 {
		t.Fatalf("expected E_POISON with offset and count, got %#v", err)
	}
	if _, err := engine.Pull(ctx, "p3", "poison", "c3", 1, 5000); err == nil || err.Code != protocol.CodePoison {
		t.Fatalf("blocked partition must reject pulls, got %#v", err)
	}
	if _, err := engine.Unblock(ctx, "u1", "poison", "operator", 1); err != nil {
		t.Fatalf("unblock: %v", err)
	}
	pulled, err := engine.Pull(ctx, "p4", "poison", "c4", 3, 5000)
	if err != nil {
		t.Fatalf("pull after unblock: %v", err)
	}
	if pulled.Messages[0].RetryCount != 0 {
		t.Fatalf("manual unblock did not reset retry counters: %#v", pulled.Messages[0])
	}
}

func TestRecoveryRedeliversUnconfirmedWindow(t *testing.T) {
	dir := t.TempDir()
	clock := newFakeClock()
	opts := testOptions(t, dir, clock)
	engine := openTestEngine(t, opts)
	ctx := context.Background()
	for seq := int64(1); seq <= 5; seq++ {
		mustAppend(t, engine, "recover", "producer", seq, "m")
	}
	if _, err := engine.Pull(ctx, "p1", "recover", "c1", 3, 5000); err != nil {
		t.Fatalf("pull: %v", err)
	}
	if _, err := engine.Commit(ctx, "c1", "recover", "c1", 1); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if err := engine.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	reopened, err := Open(ctx, opts)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer reopened.Close()
	if reopened.recoveryReport.RecoveredPartitions != 1 || len(reopened.recoveryReport.RecoveredWindows) != 1 {
		t.Fatalf("unexpected recovery report: %#v", reopened.recoveryReport)
	}
	redelivered, biz := reopened.Pull(ctx, "p2", "recover", "c2", 3, 5000)
	if biz != nil {
		t.Fatalf("pull after recovery: %v", biz)
	}
	if redelivered.Messages[0].Offset != 2 || redelivered.Messages[0].RetryCount != 1 || redelivered.Messages[1].Offset != 3 || redelivered.Messages[1].RetryCount != 1 {
		t.Fatalf("recovery did not redeliver from the earliest unconfirmed offset: %#v", redelivered.Messages)
	}
}

func TestVersionChangeRebuildsWholePartition(t *testing.T) {
	dir := t.TempDir()
	clock := newFakeClock()
	opts := testOptions(t, dir, clock)
	engine := openTestEngine(t, opts)
	ctx := context.Background()
	for seq := int64(1); seq <= 4; seq++ {
		mustAppend(t, engine, "versioned", "producer", seq, "m")
	}
	if _, err := engine.Pull(ctx, "p1", "versioned", "c1", 3, 5000); err != nil {
		t.Fatalf("pull: %v", err)
	}
	if _, err := engine.Commit(ctx, "c1", "versioned", "c1", 1); err != nil {
		t.Fatalf("commit: %v", err)
	}
	if _, err := engine.Fail(ctx, "f1", "versioned", "c1", 2, "injected"); err != nil {
		t.Fatalf("fail: %v", err)
	}
	if err := engine.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	db, err := sql.Open("sqlite3", filepath.Join(dir, "queue.db"))
	if err != nil {
		t.Fatalf("open corruptor: %v", err)
	}
	if _, err := db.Exec(`UPDATE meta SET value='0' WHERE key='ledger_format_version'`); err != nil {
		t.Fatalf("change meta version: %v", err)
	}
	if _, err := db.Exec(`UPDATE partitions SET ledger_version=0, committed_offset=0, confirmed_offset=0`); err != nil {
		t.Fatalf("corrupt ledger for rebuild: %v", err)
	}
	db.Close()
	reopened, err := Open(ctx, opts)
	if err != nil {
		t.Fatalf("reopen after version change: %v", err)
	}
	defer reopened.Close()
	if !reopened.recoveryReport.VersionRebuild.Rebuilt || reopened.recoveryReport.VersionRebuild.OriginalLedgerVersion != 0 {
		t.Fatalf("version rebuild was not reported: %#v", reopened.recoveryReport.VersionRebuild)
	}
	status, biz := reopened.Status(ctx)
	if biz != nil {
		t.Fatalf("status: %v", biz)
	}
	if status.Partitions[0].CommittedOffset != 1 || status.Partitions[0].RetryCounts["2"] != 1 || status.Partitions[0].RetryCounts["3"] != 1 {
		t.Fatalf("journal replay did not restore ledger: %#v", status.Partitions[0])
	}
}

func TestLedgerCorruptionReturnsELedger(t *testing.T) {
	dir := t.TempDir()
	opts := testOptions(t, dir, newFakeClock())
	engine := openTestEngine(t, opts)
	mustAppend(t, engine, "corrupt", "producer", 1, "m")
	if err := engine.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	db, err := sql.Open("sqlite3", filepath.Join(dir, "queue.db"))
	if err != nil {
		t.Fatalf("open corruptor: %v", err)
	}
	if _, err := db.Exec(`PRAGMA ignore_check_constraints=ON`); err != nil {
		t.Fatalf("disable checks: %v", err)
	}
	if _, err := db.Exec(`UPDATE partitions SET committed_offset=9, confirmed_offset=1 WHERE partition_key='corrupt'`); err != nil {
		t.Fatalf("corrupt ledger: %v", err)
	}
	db.Close()
	_, err = Open(context.Background(), opts)
	if err == nil {
		t.Fatalf("expected E_LEDGER")
	}
	var biz *protocol.BizError
	if !errors.As(err, &biz) || biz.Code != protocol.CodeLedger || biz.Fields["field"] != "confirmed_offset" {
		t.Fatalf("unexpected ledger error: %#v", err)
	}
}

func TestDifferentPartitionsOperateConcurrently(t *testing.T) {
	engine := openTestEngine(t, testOptions(t, t.TempDir(), newFakeClock()))
	ctx := context.Background()
	const partitions = 6
	const messages = 8
	var wg sync.WaitGroup
	errs := make(chan error, partitions)
	for partition := 0; partition < partitions; partition++ {
		key := "partition-" + string(rune('a'+partition))
		wg.Add(1)
		go func() {
			defer wg.Done()
			for seq := int64(1); seq <= messages; seq++ {
				if _, err := engine.Append(ctx, "append", key, "producer-"+key, seq, "m"); err != nil {
					errs <- err
					return
				}
			}
			session := "consumer-" + key
			for {
				result, err := engine.Pull(ctx, "pull", key, session, 3, 5000)
				if err != nil {
					errs <- err
					return
				}
				if len(result.Messages) == 0 {
					return
				}
				for _, message := range result.Messages {
					if _, err := engine.Commit(ctx, "commit", key, session, message.Offset); err != nil {
						errs <- err
						return
					}
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent partition operation: %v", err)
	}
	status, err := engine.Status(ctx)
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	if len(status.Partitions) != partitions {
		t.Fatalf("expected %d partitions, got %d", partitions, len(status.Partitions))
	}
	for _, partition := range status.Partitions {
		if partition.CommittedOffset != messages {
			t.Fatalf("partition did not commit all messages: %#v", partition)
		}
	}
}
