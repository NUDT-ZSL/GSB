package engine

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"localdns/internal/biz"
	"localdns/internal/cache"
	"localdns/internal/clock"
	"localdns/internal/flight"
	"localdns/internal/model"
	"localdns/internal/zone"
)

type Forwarder interface {
	Query(ctx context.Context, q model.Question) (model.Resolution, error)
}

type Options struct {
	MaxCNAMEHops int
}

type Result struct {
	Question     model.Question
	Resolution   model.Resolution
	CacheHit     bool
	Coalesced    bool
	CacheVersion uint64
	Key          cache.Key
}

type Winner struct {
	Key       cache.Key
	Version   uint64
	Source    model.SourceKind
	SourceSeq uint64
	WrittenAt time.Time
}

type Stats struct {
	Resolutions uint64 `json:"resolutions"`
	Errors      uint64 `json:"errors"`
	Flights     uint64 `json:"flights"`
	Coalesced   uint64 `json:"coalesced"`
}

type Engine struct {
	zone      *zone.Zone
	cache     *cache.Cache
	forwarder Forwarder
	clock     clock.Clock
	opts      Options
	group     *flight.Group

	resolutions atomic.Uint64
	errors      atomic.Uint64
	flights     atomic.Uint64
	coalesced   atomic.Uint64

	winnerMu sync.RWMutex
	winners  map[cache.Key]Winner
}

func New(z *zone.Zone, c *cache.Cache, forwarder Forwarder, clk clock.Clock, opts Options) *Engine {
	if clk == nil {
		clk = clock.Real{}
	}
	if opts.MaxCNAMEHops <= 0 {
		opts.MaxCNAMEHops = z.MaxCNAMEHops()
	}
	return &Engine{
		zone: z, cache: c, forwarder: forwarder, clock: clk, opts: opts,
		group: flight.New(), winners: make(map[cache.Key]Winner),
	}
}

func (e *Engine) Resolve(ctx context.Context, q model.Question) (*Result, error) {
	name, err := model.CanonicalName(q.Name)
	if err != nil {
		return nil, biz.New(biz.CodeQuery, "invalid query name: "+err.Error())
	}
	q.Name = name
	if q.Class == 0 {
		q.Class = model.ClassIN
	}
	key := cache.Key{Name: q.Name, Type: q.Type, Class: q.Class}

	if stored, ok := e.cache.Lookup(key); ok {
		e.resolutions.Add(1)
		return &Result{
			Question: q, Resolution: resolutionFromEntry(stored), CacheHit: true,
			CacheVersion: stored.Version, Key: key,
		}, nil
	}

	value, resolveErr, shared := e.group.Do(ctx, key.String(), func() (any, error) {
		if stored, ok := e.cache.Lookup(key); ok {
			return flightValue{entry: &stored, cacheHit: true}, nil
		}
		e.flights.Add(1)
		fresh, freshErr := e.resolveFresh(ctx, q)
		if freshErr != nil {
			return nil, freshErr
		}
		entry := entryFromResolution(key, fresh)
		stored, ok := e.cache.Put(entry)
		if !ok {
			return flightValue{resolution: &fresh}, nil
		}
		e.recordWinner(stored)
		return flightValue{entry: &stored, stored: true}, nil
	})
	if resolveErr != nil {
		e.errors.Add(1)
		return nil, resolveErr
	}
	if shared {
		e.coalesced.Add(1)
	}
	e.resolutions.Add(1)
	fv := value.(flightValue)
	if fv.entry != nil {
		return &Result{
			Question: q, Resolution: resolutionFromEntry(*fv.entry), CacheHit: fv.cacheHit,
			Coalesced: shared, CacheVersion: fv.entry.Version, Key: key,
		}, nil
	}
	if fv.resolution == nil {
		return nil, biz.New(biz.CodeQuery, "internal resolution returned no value")
	}
	return &Result{Question: q, Resolution: *fv.resolution, Coalesced: shared, Key: key}, nil
}

type flightValue struct {
	entry      *cache.Entry
	resolution *model.Resolution
	cacheHit   bool
	stored     bool
}

func (e *Engine) resolveFresh(ctx context.Context, q model.Question) (model.Resolution, error) {
	if q.Class != model.ClassIN || !e.zone.Covers(q.Name) {
		if e.forwarder == nil {
			return model.Resolution{}, biz.New(biz.CodeUpstream, "no local upstream forwarder is configured")
		}
		up, err := e.forwarder.Query(ctx, q)
		if err != nil {
			return model.Resolution{}, err
		}
		return e.applyLocalArbitration(q, nil, up)
	}

	local, err := e.zone.Resolve(q)
	if err != nil {
		return model.Resolution{}, err
	}
	localAnswers := localHitsToAnswerRecords(local.Answers, e.zone.Seq())
	if local.NeedForward == "" {
		return e.resolveLocalOnly(q, local, localAnswers)
	}

	if e.forwarder == nil {
		return model.Resolution{}, biz.New(biz.CodeUpstream, "no local upstream forwarder is configured")
	}
	targetQuestion := q
	targetQuestion.Name = local.NeedForward
	up, err := e.forwarder.Query(ctx, targetQuestion)
	if err != nil {
		return model.Resolution{}, err
	}
	return e.applyLocalArbitration(q, localAnswers, up)
}

func (e *Engine) resolveLocalOnly(q model.Question, local zone.Resolution, answers []model.AnswerRecord) (model.Resolution, error) {
	authorities := localHitsToNegativeRecords(local.Authorities, e.zone.Seq())
	source := model.SourceAuthority
	if local.Negative {
		source = model.SourceNegative
	}
	res := model.Resolution{
		RCode: local.RCode, Answers: answers, Authorities: authorities,
		Negative: local.Negative, NegativeTTL: local.NegativeTTL,
		CNAMEChain:    append([]string(nil), local.CNAMEChain...),
		PrimarySource: source, SourceSeq: e.zone.Seq(),
	}
	if local.Negative && local.NegativeTTL == 0 {
		// A non-cacheable negative is still a valid response.
		return res, nil
	}
	if _, err := rebuildChain(q.Name, q.Type, res.Answers, local.Negative, e.opts.MaxCNAMEHops); err != nil {
		return model.Resolution{}, err
	}
	return res, nil
}

func (e *Engine) applyLocalArbitration(q model.Question, localPrefix []model.AnswerRecord, up model.Resolution) (model.Resolution, error) {
	arbitrated, conflict, err := e.arbitrateAnswers(up.Answers)
	if err != nil {
		return model.Resolution{}, err
	}
	combined := make([]model.AnswerRecord, 0, len(localPrefix)+len(arbitrated))
	combined = append(combined, localPrefix...)
	combined = append(combined, arbitrated...)
	chain, err := rebuildChain(q.Name, q.Type, combined, up.Negative, e.opts.MaxCNAMEHops)
	if err != nil {
		return model.Resolution{}, err
	}
	primary := up.PrimarySource
	sourceSeq := up.SourceSeq
	if len(localPrefix) == 0 && up.Negative {
		primary = model.SourceNegative
	}
	if conflict != nil {
		primary = model.SourceAuthority
		sourceSeq = e.zone.Seq()
	}
	if len(localPrefix) > 0 && primary == "" {
		primary = model.SourceAuthority
		sourceSeq = e.zone.Seq()
	}
	res := model.Resolution{
		RCode: up.RCode, Answers: combined, Authorities: up.Authorities,
		Negative: up.Negative, NegativeTTL: up.NegativeTTL, CNAMEChain: chain,
		PrimarySource: primary, SourceSeq: sourceSeq, Conflict: conflict,
	}
	if res.Negative && res.NegativeTTL == 0 && len(localPrefix) == 0 {
		return res, nil
	}
	return res, nil
}

// arbitrateAnswers implements the conflict rule: an exact local authoritative
// RRset always wins over a forwarded response for the same owner and type. The
// winning RRset wholesale replaces the forwarded RRset; fields are never merged.
func (e *Engine) arbitrateAnswers(upAnswers []model.AnswerRecord) ([]model.AnswerRecord, *model.Conflict, error) {
	type groupKey struct {
		name string
		typ  model.Type
	}
	handled := make(map[groupKey]bool)
	out := make([]model.AnswerRecord, 0, len(upAnswers))
	var conflict *model.Conflict
	for _, ar := range upAnswers {
		gk := groupKey{name: ar.Record.Name, typ: ar.Record.Type}
		local, origins, found := e.zone.ExactRRSet(gk.name, gk.typ)
		if !found || model.RRSetEqual(local, recordsFromAnswersForGroup(upAnswers, gk.name, gk.typ)) {
			out = append(out, ar)
			continue
		}
		if handled[gk] {
			continue
		}
		handled[gk] = true
		upRecords := recordsFromAnswersForGroup(upAnswers, gk.name, gk.typ)
		localSeq := e.zone.Seq()
		upSeq := ar.Provenance.SourceSeq
		upOrigin := ar.Provenance.Origin
		winnerRecords := make([]model.AnswerRecord, 0, len(local))
		for _, rec := range local {
			winnerRecords = append(winnerRecords, model.AnswerRecord{
				Record: rec,
				Provenance: model.Provenance{
					Source: model.SourceAuthority, Origin: joinOrigins(origins), Match: model.MatchConflictAuthority, SourceSeq: localSeq,
				},
			})
		}
		out = append(out, winnerRecords...)
		current := &model.Conflict{
			Name: gk.name, Type: gk.typ, Winner: model.SourceAuthority,
			Local:    model.ConflictSide{Source: model.SourceAuthority, SourceSeq: localSeq, Origin: joinOrigins(origins), Records: append([]model.Record(nil), local...)},
			Upstream: model.ConflictSide{Source: model.SourceUpstream, SourceSeq: upSeq, Origin: upOrigin, Records: append([]model.Record(nil), upRecords...)},
		}
		if conflict == nil {
			conflict = current
		} else {
			return nil, nil, biz.New(biz.CodeChain, "multiple conflicting RRsets are not supported in one forwarded chain")
		}
	}
	return out, conflict, nil
}

func recordsFromAnswersForGroup(answers []model.AnswerRecord, name string, typ model.Type) []model.Record {
	var out []model.Record
	for _, ar := range answers {
		if ar.Record.Name == name && ar.Record.Type == typ {
			out = append(out, ar.Record)
		}
	}
	return out
}

func joinOrigins(origins []string) string {
	if len(origins) == 0 {
		return ""
	}
	if len(origins) == 1 {
		return origins[0]
	}
	return fmt.Sprintf("%s", join(origins, "+"))
}

func join(values []string, sep string) string {
	out := ""
	for i, value := range values {
		if i > 0 {
			out += sep
		}
		out += value
	}
	return out
}

func localHitsToAnswerRecords(hits []zone.Hit, seq uint64) []model.AnswerRecord {
	out := make([]model.AnswerRecord, 0, len(hits))
	for _, hit := range hits {
		out = append(out, model.AnswerRecord{
			Record: hit.Record,
			Provenance: model.Provenance{
				Source: model.SourceAuthority, Origin: hit.Origin, Match: hit.Match, SourceSeq: seq,
			},
		})
	}
	return out
}

func localHitsToNegativeRecords(hits []zone.Hit, seq uint64) []model.AnswerRecord {
	out := make([]model.AnswerRecord, 0, len(hits))
	for _, hit := range hits {
		out = append(out, model.AnswerRecord{
			Record: hit.Record,
			Provenance: model.Provenance{
				Source: model.SourceNegative, Origin: hit.Origin, Match: model.MatchNegative, SourceSeq: seq,
			},
		})
	}
	return out
}

func rebuildChain(qname string, qtype model.Type, answers []model.AnswerRecord, negative bool, maxHops int) ([]string, error) {
	expected := qname
	terminal := false
	chain := make([]string, 0, 4)
	seen := map[string]bool{expected: true}
	hops := 0
	for i, ar := range answers {
		if ar.Record.Name != expected {
			return nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] owner %s does not continue chain at %s", i, ar.Record.Name, expected))
		}
		if terminal {
			if ar.Record.Type != qtype {
				return nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] type %s appears after terminal RRset", i, ar.Record.Type.String()))
			}
			continue
		}
		if ar.Record.Type == model.TypeCNAME && qtype != model.TypeCNAME {
			target, err := model.CanonicalHostName(ar.Record.Data)
			if err != nil {
				return nil, biz.New(biz.CodeCNAME, "invalid CNAME target: "+err.Error())
			}
			hops++
			if hops > maxHops {
				return nil, biz.New(biz.CodeCNAME, fmt.Sprintf("CNAME chain exceeds %d hops", maxHops))
			}
			if seen[target] {
				return nil, biz.New(biz.CodeCNAME, "CNAME loop detected at "+target)
			}
			seen[target] = true
			chain = append(chain, expected+" -> "+target)
			expected = target
			continue
		}
		if ar.Record.Type != qtype {
			return nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] type %s does not match question type %s", i, ar.Record.Type.String(), qtype.String()))
		}
		terminal = true
	}
	if !terminal && !negative {
		return nil, biz.New(biz.CodeChain, "answer chain has no terminal RRset")
	}
	return chain, nil
}

func entryFromResolution(key cache.Key, res model.Resolution) cache.Entry {
	return cache.Entry{
		Key: key, RCode: res.RCode,
		Answers:     append([]model.AnswerRecord(nil), res.Answers...),
		Authorities: append([]model.AnswerRecord(nil), res.Authorities...),
		Negative:    res.Negative, NegativeTTL: res.NegativeTTL,
		CNAMEChain:    append([]string(nil), res.CNAMEChain...),
		PrimarySource: res.PrimarySource, SourceSeq: res.SourceSeq,
		Conflict: res.Conflict,
	}
}

func resolutionFromEntry(e cache.Entry) model.Resolution {
	var conflict *model.Conflict
	if e.Conflict != nil {
		conflict = e.Conflict
	}
	return model.Resolution{
		RCode: e.RCode, Answers: e.Answers, Authorities: e.Authorities,
		Negative: e.Negative, NegativeTTL: e.NegativeTTL,
		CNAMEChain: e.CNAMEChain, PrimarySource: e.PrimarySource,
		SourceSeq: e.SourceSeq, Conflict: conflict,
	}
}

func (e *Engine) recordWinner(entry cache.Entry) {
	e.winnerMu.Lock()
	defer e.winnerMu.Unlock()
	e.winners[entry.Key] = Winner{
		Key: entry.Key, Version: entry.Version, Source: entry.PrimarySource,
		SourceSeq: entry.SourceSeq, WrittenAt: entry.WrittenAt,
	}
}

func (e *Engine) Winner(key cache.Key) (Winner, bool) {
	e.winnerMu.RLock()
	defer e.winnerMu.RUnlock()
	w, ok := e.winners[key]
	return w, ok
}

func (e *Engine) Winners() []Winner {
	e.winnerMu.RLock()
	defer e.winnerMu.RUnlock()
	out := make([]Winner, 0, len(e.winners))
	for _, w := range e.winners {
		out = append(out, w)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key.String() < out[j].Key.String() })
	return out
}

func (e *Engine) Stats() Stats {
	return Stats{
		Resolutions: e.resolutions.Load(), Errors: e.errors.Load(),
		Flights: e.flights.Load(), Coalesced: e.coalesced.Load(),
	}
}

func (e *Engine) CacheStats() cache.Stats { return e.cache.Stats() }
func (e *Engine) Zone() *zone.Zone        { return e.zone }
