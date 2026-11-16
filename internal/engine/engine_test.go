package engine

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"localdns/internal/biz"
	"localdns/internal/cache"
	"localdns/internal/clock"
	"localdns/internal/model"
	"localdns/internal/zone"
)

type countingForwarder struct {
	mu      sync.Mutex
	calls   int
	queries []model.Question
	delay   time.Duration
	fn      func(model.Question) (model.Resolution, error)
}

func (f *countingForwarder) Query(ctx context.Context, q model.Question) (model.Resolution, error) {
	if f.delay > 0 {
		select {
		case <-time.After(f.delay):
		case <-ctx.Done():
			return model.Resolution{}, ctx.Err()
		}
	}
	f.mu.Lock()
	f.calls++
	f.queries = append(f.queries, q)
	fn := f.fn
	f.mu.Unlock()
	if fn == nil {
		return model.Resolution{}, biz.New(biz.CodeUpstream, "not configured")
	}
	return fn(q)
}

func (f *countingForwarder) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func sampleZone(t *testing.T) *zone.Zone {
	t.Helper()
	z, err := zone.LoadJSON(filepath.Join("../../samples", "zone.json"), zone.Options{})
	if err != nil {
		t.Fatal(err)
	}
	_, records, err := zone.LoadWireMessage(filepath.Join("../../samples", "answer.wire"))
	if err != nil {
		t.Fatal(err)
	}
	if err := z.AddWireRecords(records, zone.OriginWireSample); err != nil {
		t.Fatal(err)
	}
	return z
}

func newEngine(z *zone.Zone, f Forwarder, clk clock.Clock) (*Engine, *cache.Cache) {
	c := cache.New(64, clk)
	return New(z, c, f, clk, Options{MaxCNAMEHops: 16}), c
}

func upstreamA(name, data string, seq uint64) model.Resolution {
	return model.Resolution{
		RCode: model.RCodeNoError,
		Answers: []model.AnswerRecord{{
			Record:     model.Record{Name: name, Type: model.TypeA, Class: model.ClassIN, TTL: 90, Data: data},
			Provenance: model.Provenance{Source: model.SourceUpstream, Origin: "tcp-upstream", Match: model.MatchForward, SourceSeq: seq},
		}},
		PrimarySource: model.SourceUpstream, SourceSeq: seq,
	}
}

func TestTTLCacheHitExpiryAndVersion(t *testing.T) {
	base := time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC)
	clk := clock.NewManual(base)
	fwd := &countingForwarder{}
	eng, c := newEngine(sampleZone(t), fwd, clk)
	q := model.Question{Name: "SAME.DEMO.TEST.", Type: model.TypeA, Class: model.ClassIN}
	first, err := eng.Resolve(context.Background(), q)
	if err != nil {
		t.Fatal(err)
	}
	if first.CacheHit || first.CacheVersion == 0 || first.Resolution.PrimarySource != model.SourceAuthority {
		t.Fatalf("unexpected first result: %+v", first)
	}
	if first.Resolution.Answers[0].Record.TTL != 45 {
		t.Fatalf("initial TTL=%d", first.Resolution.Answers[0].Record.TTL)
	}
	clk.Advance(3 * time.Second)
	second, err := eng.Resolve(context.Background(), q)
	if err != nil {
		t.Fatal(err)
	}
	if !second.CacheHit || second.CacheVersion != first.CacheVersion {
		t.Fatalf("cache hit/version mismatch: first=%+v second=%+v", first, second)
	}
	if second.Resolution.Answers[0].Record.TTL != 42 {
		t.Fatalf("decremented TTL=%d", second.Resolution.Answers[0].Record.TTL)
	}
	clk.Advance(42 * time.Second)
	third, err := eng.Resolve(context.Background(), q)
	if err != nil {
		t.Fatal(err)
	}
	if third.CacheHit || third.CacheVersion == first.CacheVersion {
		t.Fatalf("expired entry should be a fresh fill with a new version: %+v", third)
	}
	if third.Resolution.Answers[0].Record.TTL != 45 {
		t.Fatalf("refilled TTL=%d", third.Resolution.Answers[0].Record.TTL)
	}
	if fwd.count() != 0 {
		t.Fatalf("authority queries called upstream %d times", fwd.count())
	}
	if c.Stats().Expired == 0 {
		t.Fatal("expiry was not recorded")
	}
}

func TestLocalAuthorityWinsConflictAndIsCachedWholly(t *testing.T) {
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	fwd := &countingForwarder{fn: func(q model.Question) (model.Resolution, error) {
		return model.Resolution{
			RCode: model.RCodeNoError,
			Answers: []model.AnswerRecord{
				{
					Record:     model.Record{Name: "portal.external.test", Type: model.TypeCNAME, Class: model.ClassIN, TTL: 120, Data: "clash.demo.test"},
					Provenance: model.Provenance{Source: model.SourceUpstream, Origin: "tcp-upstream", Match: model.MatchForward, SourceSeq: 9},
				},
				{
					Record:     model.Record{Name: "clash.demo.test", Type: model.TypeA, Class: model.ClassIN, TTL: 120, Data: "198.51.100.99"},
					Provenance: model.Provenance{Source: model.SourceUpstream, Origin: "tcp-upstream", Match: model.MatchForward, SourceSeq: 9},
				},
			},
			CNAMEChain:    []string{"portal.external.test -> clash.demo.test"},
			PrimarySource: model.SourceUpstream, SourceSeq: 9,
		}, nil
	}}
	eng, _ := newEngine(sampleZone(t), fwd, clk)
	q := model.Question{Name: "portal.external.test", Type: model.TypeA, Class: model.ClassIN}
	first, err := eng.Resolve(context.Background(), q)
	if err != nil {
		t.Fatal(err)
	}
	if len(first.Resolution.Answers) != 2 {
		t.Fatalf("answer count=%d", len(first.Resolution.Answers))
	}
	terminal := first.Resolution.Answers[1]
	if terminal.Record.Data != "192.0.2.44" || terminal.Provenance.Match != model.MatchConflictAuthority {
		t.Fatalf("local authority did not win: %+v", terminal)
	}
	if first.Resolution.Conflict == nil || first.Resolution.Conflict.Winner != model.SourceAuthority {
		t.Fatalf("conflict not recorded: %+v", first.Resolution.Conflict)
	}
	if first.Resolution.Conflict.Local.SourceSeq != 1 || first.Resolution.Conflict.Upstream.SourceSeq != 9 {
		t.Fatalf("conflict provenance mismatch: %+v", first.Resolution.Conflict)
	}
	if len(first.Resolution.Conflict.Local.Records) != 1 || first.Resolution.Conflict.Local.Records[0].Data != "192.0.2.44" {
		t.Fatal("local conflict side missing")
	}
	if first.Resolution.PrimarySource != model.SourceAuthority || first.Resolution.SourceSeq != 1 {
		t.Fatalf("primary source=%s seq=%d", first.Resolution.PrimarySource, first.Resolution.SourceSeq)
	}
	second, err := eng.Resolve(context.Background(), q)
	if err != nil {
		t.Fatal(err)
	}
	if !second.CacheHit || second.Resolution.Answers[1].Record.Data != "192.0.2.44" {
		t.Fatalf("conflict cache view mismatch: %+v", second)
	}
	if fwd.count() != 1 {
		t.Fatalf("upstream called %d times", fwd.count())
	}
}

func TestErrorsAreNotCached(t *testing.T) {
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	var fail = true
	fwd := &countingForwarder{fn: func(q model.Question) (model.Resolution, error) {
		if fail {
			return model.Resolution{}, biz.New(biz.CodeChain, "bad chain")
		}
		return upstreamA(q.Name, "203.0.113.9", 3), nil
	}}
	eng, _ := newEngine(sampleZone(t), fwd, clk)
	q := model.Question{Name: "flaky.external.test", Type: model.TypeA, Class: model.ClassIN}
	_, err := eng.Resolve(context.Background(), q)
	if biz.CodeOf(err) != biz.CodeChain {
		t.Fatalf("want E_CHAIN, got %v", err)
	}
	fail = false
	res, err := eng.Resolve(context.Background(), q)
	if err != nil {
		t.Fatal(err)
	}
	if res.CacheHit || res.CacheVersion == 0 || res.Resolution.Answers[0].Record.TTL != 90 {
		t.Fatalf("error contaminated cache: %+v", res)
	}
	if fwd.count() != 2 {
		t.Fatalf("upstream calls=%d", fwd.count())
	}
}

func TestLocalCNAMEToExternalTargetCombinesForwardChain(t *testing.T) {
	body := `{"apex":"demo.test","records":[
      {"name":"demo.test","type":"SOA","ttl":300,"rdata":"ns1.demo.test hostmaster.demo.test 1 3600 600 86400 30"},
      {"name":"ext.demo.test","type":"CNAME","ttl":30,"rdata":"target.external.test"}
    ]}`
	path := filepath.Join(t.TempDir(), "zone.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	z, err := zone.LoadJSON(path, zone.Options{})
	if err != nil {
		t.Fatal(err)
	}
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	fwd := &countingForwarder{fn: func(q model.Question) (model.Resolution, error) {
		if q.Name != "target.external.test" {
			t.Errorf("forwarded target=%s", q.Name)
		}
		return upstreamA(q.Name, "203.0.113.42", 5), nil
	}}
	eng, _ := newEngine(z, fwd, clk)
	res, err := eng.Resolve(context.Background(), model.Question{Name: "ext.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Resolution.Answers) != 2 || res.Resolution.Answers[0].Record.Type != model.TypeCNAME {
		t.Fatalf("combined chain mismatch: %+v", res.Resolution.Answers)
	}
	if res.Resolution.PrimarySource != model.SourceUpstream {
		t.Fatalf("primary source=%s", res.Resolution.PrimarySource)
	}
}

func TestCNAMELoopIsECNAMEAndNotCached(t *testing.T) {
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	eng, c := newEngine(sampleZone(t), &countingForwarder{}, clk)
	_, err := eng.Resolve(context.Background(), model.Question{Name: "loop-a.demo.test", Type: model.TypeA, Class: model.ClassIN})
	if biz.CodeOf(err) != biz.CodeCNAME {
		t.Fatalf("want E_CNAME, got %v", err)
	}
	if c.Len() != 0 {
		t.Fatalf("E_CNAME wrote %d cache entries", c.Len())
	}
}

func TestConcurrentSameKeyConvergesToOneFill(t *testing.T) {
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	fwd := &countingForwarder{
		delay: 25 * time.Millisecond,
		fn: func(q model.Question) (model.Resolution, error) {
			return upstreamA(q.Name, "203.0.113.77", 77), nil
		},
	}
	eng, c := newEngine(sampleZone(t), fwd, clk)
	q := model.Question{Name: "concurrent.external.test", Type: model.TypeA, Class: model.ClassIN}
	const workers = 40
	start := make(chan struct{})
	results := make([]*Result, workers)
	errs := make([]error, workers)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			results[i], errs[i] = eng.Resolve(context.Background(), q)
		}(i)
	}
	close(start)
	wg.Wait()
	version := uint64(0)
	for i := 0; i < workers; i++ {
		if errs[i] != nil {
			t.Fatalf("worker %d: %v", i, errs[i])
		}
		if results[i].Resolution.Answers[0].Record.Data != "203.0.113.77" {
			t.Fatalf("worker %d bad data", i)
		}
		if results[i].CacheVersion == 0 {
			t.Fatalf("worker %d has no version (uncached result)", i)
		}
		if version == 0 {
			version = results[i].CacheVersion
		} else if results[i].CacheVersion != version {
			t.Fatalf("multiple versions: %d and %d", version, results[i].CacheVersion)
		}
	}
	if fwd.count() != 1 {
		t.Fatalf("upstream calls=%d want 1", fwd.count())
	}
	if c.Stats().Fills != 1 {
		t.Fatalf("cache fills=%d", c.Stats().Fills)
	}
	winner, ok := eng.Winner(cache.Key{Name: q.Name, Type: q.Type, Class: q.Class})
	if !ok || winner.Version != version || winner.Source != model.SourceUpstream {
		t.Fatalf("winner mismatch: %+v", winner)
	}
}
