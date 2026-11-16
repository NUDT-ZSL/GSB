package cache

import (
	"sync"
	"testing"
	"time"

	"localdns/internal/clock"
	"localdns/internal/model"
)

func testKey(name string) Key {
	return Key{Name: name, Type: model.TypeA, Class: model.ClassIN}
}

func positiveEntry(key Key, ttl uint32, data string) Entry {
	return Entry{
		Key: key, RCode: model.RCodeNoError, PrimarySource: model.SourceAuthority, SourceSeq: 1,
		Answers: []model.AnswerRecord{{
			Record:     model.Record{Name: key.Name, Type: key.Type, Class: key.Class, TTL: ttl, Data: data},
			Provenance: model.Provenance{Source: model.SourceAuthority, Match: model.MatchExact, SourceSeq: 1},
		}},
	}
}

func TestTTLDecrementAndExpiry(t *testing.T) {
	base := time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC)
	clk := clock.NewManual(base)
	c := New(4, clk)
	key := testKey("www.demo.test")
	if _, ok := c.Put(positiveEntry(key, 10, "192.0.2.10")); !ok {
		t.Fatal("entry was not stored")
	}
	got, ok := c.Lookup(key)
	if !ok || got.Answers[0].Record.TTL != 10 {
		t.Fatalf("fresh lookup: %+v ok=%v", got, ok)
	}
	clk.Advance(3200 * time.Millisecond)
	got, ok = c.Lookup(key)
	if !ok || got.Answers[0].Record.TTL != 7 {
		t.Fatalf("lookup after 3.2s: %+v ok=%v", got, ok)
	}
	clk.Advance(6800 * time.Millisecond)
	if got, ok = c.Lookup(key); ok {
		t.Fatalf("expired entry was returned: %+v", got)
	}
	if c.Stats().Expired == 0 {
		t.Fatal("expiry was not counted")
	}
}

func TestNegativeTTL(t *testing.T) {
	base := time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC)
	clk := clock.NewManual(base)
	c := New(2, clk)
	key := Key{Name: "missing.demo.test", Type: model.TypeA, Class: model.ClassIN}
	entry := Entry{Key: key, RCode: model.RCodeNXDomain, Negative: true, NegativeTTL: 5, PrimarySource: model.SourceNegative, SourceSeq: 7}
	if _, ok := c.Put(entry); !ok {
		t.Fatal("negative entry not stored")
	}
	clk.Advance(2 * time.Second)
	got, ok := c.Lookup(key)
	if !ok || got.NegativeTTL != 3 {
		t.Fatalf("negative TTL not decremented: %+v ok=%v", got, ok)
	}
	clk.Advance(4 * time.Second)
	if _, ok := c.Lookup(key); ok {
		t.Fatal("expired negative entry hit")
	}
}

func TestDeterministicLRUEviction(t *testing.T) {
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	c := New(2, clk)
	a, b, cc := testKey("a.demo.test"), testKey("b.demo.test"), testKey("c.demo.test")
	c.Put(positiveEntry(a, 60, "192.0.2.1"))
	c.Put(positiveEntry(b, 60, "192.0.2.2"))
	if _, ok := c.Lookup(a); !ok {
		t.Fatal("A should be present")
	}
	c.Put(positiveEntry(cc, 60, "192.0.2.3"))
	if _, ok := c.Lookup(b); ok {
		t.Fatal("B should have been evicted as least recently used")
	}
	if _, ok := c.Lookup(a); !ok {
		t.Fatal("A should remain")
	}
	if _, ok := c.Lookup(cc); !ok {
		t.Fatal("C should remain")
	}
	if c.Stats().Evictions != 1 {
		t.Fatalf("evictions=%d", c.Stats().Evictions)
	}
}

func TestReplacementIsWholeEntry(t *testing.T) {
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	c := New(2, clk)
	key := testKey("same.demo.test")
	first := positiveEntry(key, 60, "192.0.2.1")
	first.SourceSeq = 1
	stored1, _ := c.Put(first)
	clk.Advance(time.Second)
	second := positiveEntry(key, 30, "198.51.100.2")
	second.PrimarySource = model.SourceUpstream
	second.SourceSeq = 2
	stored2, _ := c.Put(second)
	if stored2.Version <= stored1.Version {
		t.Fatalf("version did not advance: %d -> %d", stored1.Version, stored2.Version)
	}
	got, ok := c.Lookup(key)
	if !ok {
		t.Fatal("replacement missing")
	}
	if len(got.Answers) != 1 || got.Answers[0].Record.Data != "198.51.100.2" {
		t.Fatalf("old fields leaked into replacement: %+v", got.Answers)
	}
	if got.PrimarySource != model.SourceUpstream || got.SourceSeq != 2 {
		t.Fatalf("source attribution leaked: %+v", got)
	}
	if c.Stats().Replaced != 1 {
		t.Fatalf("replacement count=%d", c.Stats().Replaced)
	}
}

func TestConcurrentLookupNeverReturnsNegativeTTL(t *testing.T) {
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	c := New(8, clk)
	key := testKey("race.demo.test")
	c.Put(positiveEntry(key, 5, "192.0.2.9"))
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				entry, ok := c.Lookup(key)
				if ok && entry.Answers[0].Record.TTL > 5 {
					t.Errorf("TTL grew: %d", entry.Answers[0].Record.TTL)
				}
				if ok && entry.Answers[0].Record.TTL == 0 {
					t.Errorf("TTL zero should not be a hit")
				}
			}
		}()
	}
	wg.Wait()
	clk.Advance(5 * time.Second)
	if _, ok := c.Lookup(key); ok {
		t.Fatal("entry should have expired")
	}
}
