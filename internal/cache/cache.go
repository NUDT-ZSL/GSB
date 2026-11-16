package cache

import (
	"fmt"
	"sort"
	"sync"
	"time"

	"localdns/internal/clock"
	"localdns/internal/model"
)

type Key struct {
	Name  string
	Type  model.Type
	Class model.Class
}

func (k Key) String() string {
	return fmt.Sprintf("%s/%s/%s", k.Name, k.Type.String(), k.Class.String())
}

type Entry struct {
	Key           Key
	RCode         int
	Answers       []model.AnswerRecord
	Authorities   []model.AnswerRecord
	Negative      bool
	NegativeTTL   uint32
	CNAMEChain    []string
	PrimarySource model.SourceKind
	SourceSeq     uint64
	Conflict      *model.Conflict
	WrittenAt     time.Time
	Version       uint64
}

type Stats struct {
	Hits      uint64 `json:"hits"`
	Misses    uint64 `json:"misses"`
	Expired   uint64 `json:"expired"`
	Evictions uint64 `json:"evictions"`
	Fills     uint64 `json:"fills"`
	Replaced  uint64 `json:"replaced"`
	Entries   int    `json:"entries"`
}

type storedEntry struct {
	entry    Entry
	access   uint64
	inserted uint64
}

type Cache struct {
	mu       sync.Mutex
	entries  map[Key]storedEntry
	capacity int
	clock    clock.Clock
	access   uint64
	inserted uint64
	version  uint64
	stats    Stats
}

func New(capacity int, c clock.Clock) *Cache {
	if c == nil {
		c = clock.Real{}
	}
	return &Cache{entries: make(map[Key]storedEntry), capacity: capacity, clock: c}
}

func (c *Cache) KeyFor(name string, typ model.Type, class model.Class) Key {
	return Key{Name: name, Type: typ, Class: class}
}

func entryTTL(e Entry) uint32 {
	if e.Negative {
		return e.NegativeTTL
	}
	var ttl uint32
	first := true
	for _, ar := range e.Answers {
		if first || ar.Record.TTL < ttl {
			ttl = ar.Record.TTL
			first = false
		}
	}
	for _, ar := range e.Authorities {
		if first || ar.Record.TTL < ttl {
			ttl = ar.Record.TTL
			first = false
		}
	}
	if first {
		return 0
	}
	return ttl
}

func (c *Cache) Put(e Entry) (Entry, bool) {
	if entryTTL(e) == 0 || c.capacity <= 0 {
		return Entry{}, false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.clock.Now()
	e.WrittenAt = now
	c.access++
	e.Version = 0
	if _, exists := c.entries[e.Key]; exists {
		c.stats.Replaced++
	}
	if len(c.entries) >= c.capacity {
		if _, replacing := c.entries[e.Key]; !replacing {
			c.evictOneLocked()
		}
	}
	c.version++
	e.Version = c.version
	c.inserted++
	c.entries[e.Key] = storedEntry{entry: cloneEntry(e), access: c.access, inserted: c.inserted}
	c.stats.Fills++
	c.stats.Entries = len(c.entries)
	return cloneEntry(e), true
}

func (c *Cache) Lookup(key Key) (Entry, bool) {
	return c.LookupAt(key, c.clock.Now())
}

func (c *Cache) LookupAt(key Key, now time.Time) (Entry, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	stored, ok := c.entries[key]
	if !ok {
		c.stats.Misses++
		return Entry{}, false
	}
	ttl := entryTTL(stored.entry)
	elapsed := elapsedSeconds(now, stored.entry.WrittenAt)
	if ttl == 0 || elapsed >= ttl {
		delete(c.entries, key)
		c.stats.Expired++
		c.stats.Misses++
		c.stats.Entries = len(c.entries)
		return Entry{}, false
	}
	c.access++
	stored.access = c.access
	c.entries[key] = stored
	c.stats.Hits++
	out := adjustEntry(stored.entry, elapsed)
	return out, true
}

func elapsedSeconds(now, then time.Time) uint32 {
	if !now.After(then) {
		return 0
	}
	secs := now.Sub(then) / time.Second
	if secs <= 0 {
		return 0
	}
	if secs > time.Duration(^uint32(0))*time.Second {
		return ^uint32(0)
	}
	return uint32(secs)
}

func adjustTTL(ttl uint32, elapsed uint32) uint32 {
	if elapsed >= ttl {
		return 0
	}
	return ttl - elapsed
}

func adjustEntry(e Entry, elapsed uint32) Entry {
	out := cloneEntry(e)
	for i := range out.Answers {
		out.Answers[i].Record.TTL = adjustTTL(out.Answers[i].Record.TTL, elapsed)
	}
	for i := range out.Authorities {
		out.Authorities[i].Record.TTL = adjustTTL(out.Authorities[i].Record.TTL, elapsed)
	}
	out.NegativeTTL = adjustTTL(out.NegativeTTL, elapsed)
	return out
}

func (c *Cache) Sweep() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.clock.Now()
	removed := 0
	for key, stored := range c.entries {
		ttl := entryTTL(stored.entry)
		if ttl == 0 || elapsedSeconds(now, stored.entry.WrittenAt) >= ttl {
			delete(c.entries, key)
			removed++
		}
	}
	if removed > 0 {
		c.stats.Expired += uint64(removed)
	}
	c.stats.Entries = len(c.entries)
	return removed
}

func (c *Cache) Len() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.entries)
}

func (c *Cache) Stats() Stats {
	c.mu.Lock()
	defer c.mu.Unlock()
	stats := c.stats
	stats.Entries = len(c.entries)
	return stats
}

type VersionInfo struct {
	Key       Key
	Version   uint64
	Source    model.SourceKind
	SourceSeq uint64
	WrittenAt time.Time
	TTL       uint32
}

func (c *Cache) Versions() []VersionInfo {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]VersionInfo, 0, len(c.entries))
	for key, stored := range c.entries {
		out = append(out, VersionInfo{
			Key: key, Version: stored.entry.Version, Source: stored.entry.PrimarySource,
			SourceSeq: stored.entry.SourceSeq, WrittenAt: stored.entry.WrittenAt,
			TTL: entryTTL(stored.entry),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Key.String() != out[j].Key.String() {
			return out[i].Key.String() < out[j].Key.String()
		}
		return out[i].Version < out[j].Version
	})
	return out
}

func (c *Cache) evictOneLocked() {
	var victimKey Key
	found := false
	var victim storedEntry
	for key, stored := range c.entries {
		if !found || stored.access < victim.access ||
			(stored.access == victim.access && stored.inserted < victim.inserted) ||
			(stored.access == victim.access && stored.inserted == victim.inserted && key.String() < victimKey.String()) {
			found = true
			victim = stored
			victimKey = key
		}
	}
	if found {
		delete(c.entries, victimKey)
		c.stats.Evictions++
	}
}

func cloneEntry(e Entry) Entry {
	out := e
	out.Answers = append([]model.AnswerRecord(nil), e.Answers...)
	out.Authorities = append([]model.AnswerRecord(nil), e.Authorities...)
	out.CNAMEChain = append([]string(nil), e.CNAMEChain...)
	if e.Conflict != nil {
		conflict := *e.Conflict
		conflict.Local.Records = append([]model.Record(nil), e.Conflict.Local.Records...)
		conflict.Upstream.Records = append([]model.Record(nil), e.Conflict.Upstream.Records...)
		out.Conflict = &conflict
	}
	return out
}
