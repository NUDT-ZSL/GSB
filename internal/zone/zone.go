package zone

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strconv"
	"strings"

	"localdns/internal/biz"
	"localdns/internal/dnswire"
	"localdns/internal/model"
)

const (
	DefaultMaxTTL         uint32 = 604800
	DefaultNegativeTTLCap uint32 = 300
	DefaultMaxCNAMEHops          = 16
)

const (
	OriginJSON       = "json"
	OriginWireSample = "wire-sample"
)

type Options struct {
	MaxTTL         uint32
	NegativeTTLCap uint32
	MaxCNAMEHops   int
	Seq            uint64
}

func (o Options) withDefaults() Options {
	if o.MaxTTL == 0 {
		o.MaxTTL = DefaultMaxTTL
	}
	if o.NegativeTTLCap == 0 {
		o.NegativeTTLCap = DefaultNegativeTTLCap
	}
	if o.MaxCNAMEHops <= 0 {
		o.MaxCNAMEHops = DefaultMaxCNAMEHops
	}
	if o.Seq == 0 {
		o.Seq = 1
	}
	return o
}

type ZoneRecord struct {
	Record  model.Record
	Origins []string
}

func (r ZoneRecord) OriginString() string {
	origins := append([]string(nil), r.Origins...)
	sort.Strings(origins)
	return strings.Join(origins, "+")
}

type Hit struct {
	Record model.Record
	Match  model.MatchMode
	Origin string
}

type Resolution struct {
	RCode       int
	Answers     []Hit
	Authorities []Hit
	Negative    bool
	NegativeTTL uint32
	CNAMEChain  []string
	NeedForward string
}

type Zone struct {
	apex    string
	seq     uint64
	opts    Options
	records map[string]map[model.Type][]ZoneRecord
	soa     *ZoneRecord
}

type jsonFile struct {
	Apex    string       `json:"apex"`
	Records []jsonRecord `json:"records"`
}

type jsonRecord struct {
	Name  string          `json:"name"`
	Type  string          `json:"type"`
	TTL   json.RawMessage `json:"ttl"`
	RDATA string          `json:"rdata"`
}

func LoadJSON(path string, opts Options) (*Zone, error) {
	opts = opts.withDefaults()
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, &biz.Error{Code: biz.CodeZone, Detail: "cannot read zone file", Cause: err}
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var doc jsonFile
	if err := dec.Decode(&doc); err != nil {
		return nil, &biz.Error{Code: biz.CodeZone, Detail: "invalid zone JSON: " + err.Error(), Cause: err}
	}
	var trailing any
	if err := dec.Decode(&trailing); err != io.EOF {
		return nil, biz.New(biz.CodeZone, "zone JSON contains trailing data")
	}
	apex, err := model.CanonicalHostName(doc.Apex)
	if err != nil {
		return nil, biz.New(biz.CodeZone, "invalid apex: "+err.Error())
	}
	records := make([]ZoneRecord, 0, len(doc.Records))
	for i, raw := range doc.Records {
		rec, recErr := normalizeJSONRecord(raw, opts.MaxTTL)
		if recErr != nil {
			return nil, biz.New(biz.CodeZone, fmt.Sprintf("record[%d]: %s", i, recErr.Error()))
		}
		if !model.IsSubdomain(rec.Name, apex) {
			return nil, biz.New(biz.CodeZone, fmt.Sprintf("record[%d]: owner %s is outside apex %s", i, rec.Name, apex))
		}
		records = append(records, ZoneRecord{Record: rec, Origins: []string{OriginJSON}})
	}
	z := &Zone{apex: apex, seq: opts.Seq, opts: opts}
	if err := z.install(records); err != nil {
		return nil, err
	}
	return z, nil
}

func normalizeJSONRecord(raw jsonRecord, maxTTL uint32) (model.Record, error) {
	if len(raw.TTL) == 0 || string(raw.TTL) == "null" {
		return model.Record{}, fmt.Errorf("ttl is required and must be an integer")
	}
	ttlText := string(raw.TTL)
	if strings.ContainsAny(ttlText, ".eE+-") {
		return model.Record{}, fmt.Errorf("ttl must be a non-negative integer, got %s", ttlText)
	}
	n, err := strconv.ParseUint(ttlText, 10, 32)
	if err != nil {
		return model.Record{}, fmt.Errorf("ttl must be a non-negative integer, got %s", ttlText)
	}
	if uint32(n) > maxTTL {
		return model.Record{}, fmt.Errorf("ttl %d exceeds maximum %d", n, maxTTL)
	}
	t, err := model.ParseAuthorityType(raw.Type)
	if err != nil {
		return model.Record{}, err
	}
	rec, err := model.NormalizeRecord(raw.Name, t, model.ClassIN, uint32(n), raw.RDATA)
	if err != nil {
		return model.Record{}, err
	}
	return rec, nil
}

// AddWireRecords merges answer records decoded from an RFC 1035 sample. Equal
// logical records are coalesced and retain both origins.
func (z *Zone) AddWireRecords(records []model.Record, origin string) error {
	if origin == "" {
		origin = OriginWireSample
	}
	toAdd := make([]ZoneRecord, 0, len(records))
	for i, rec := range records {
		if rec.Class != model.ClassIN {
			return biz.New(biz.CodeZone, fmt.Sprintf("wire record[%d]: only class IN is supported", i))
		}
		if rec.TTL > z.opts.MaxTTL {
			return biz.New(biz.CodeZone, fmt.Sprintf("wire record[%d]: ttl %d exceeds maximum %d", i, rec.TTL, z.opts.MaxTTL))
		}
		typ, err := model.ParseAuthorityType(rec.Type.String())
		if err != nil {
			return biz.New(biz.CodeZone, fmt.Sprintf("wire record[%d]: %s", i, err.Error()))
		}
		normalized, err := model.NormalizeRecord(rec.Name, typ, rec.Class, rec.TTL, rec.Data)
		if err != nil {
			return biz.New(biz.CodeZone, fmt.Sprintf("wire record[%d]: %s", i, err.Error()))
		}
		if !model.IsSubdomain(normalized.Name, z.apex) {
			return biz.New(biz.CodeZone, fmt.Sprintf("wire record[%d]: owner %s is outside apex %s", i, normalized.Name, z.apex))
		}
		toAdd = append(toAdd, ZoneRecord{Record: normalized, Origins: []string{origin}})
	}
	return z.install(toAdd)
}

func (z *Zone) install(incoming []ZoneRecord) error {
	merged := make(map[string]map[model.Type][]ZoneRecord)
	for name, byType := range z.records {
		merged[name] = make(map[model.Type][]ZoneRecord)
		for typ, recs := range byType {
			merged[name][typ] = append([]ZoneRecord(nil), recs...)
		}
	}
	for _, candidate := range incoming {
		if merged[candidate.Record.Name] == nil {
			merged[candidate.Record.Name] = make(map[model.Type][]ZoneRecord)
		}
		existing := merged[candidate.Record.Name][candidate.Record.Type]
		duplicate := false
		for i := range existing {
			if sameLogicalRecord(existing[i].Record, candidate.Record) {
				if existing[i].Record.TTL != candidate.Record.TTL {
					return biz.New(biz.CodeZone, fmt.Sprintf("duplicate logical record %s/%s has conflicting TTL values", candidate.Record.Name, candidate.Record.Type.String()))
				}
				existing[i].Origins = mergeOrigins(existing[i].Origins, candidate.Origins)
				duplicate = true
				break
			}
		}
		if !duplicate {
			merged[candidate.Record.Name][candidate.Record.Type] = append(existing, candidate)
		}
	}
	if err := validateMerged(merged, z.apex); err != nil {
		return err
	}
	z.records = merged
	z.soa = nil
	for _, rec := range z.records[z.apex][model.TypeSOA] {
		copied := rec
		z.soa = &copied
		break
	}
	return nil
}

func sameLogicalRecord(a, b model.Record) bool {
	return a.Name == b.Name && a.Type == b.Type && a.Class == b.Class && a.Data == b.Data
}

func mergeOrigins(a, b []string) []string {
	set := make(map[string]bool)
	for _, s := range append(append([]string(nil), a...), b...) {
		set[s] = true
	}
	out := make([]string, 0, len(set))
	for s := range set {
		out = append(out, s)
	}
	sort.Strings(out)
	return out
}

func validateMerged(records map[string]map[model.Type][]ZoneRecord, apex string) error {
	soaCount := 0
	for name, byType := range records {
		for typ, recs := range byType {
			if len(recs) == 0 {
				continue
			}
			ttl := recs[0].Record.TTL
			for _, rec := range recs {
				if rec.Record.TTL != ttl {
					return biz.New(biz.CodeZone, fmt.Sprintf("owner %s type %s has inconsistent TTL values", name, typ.String()))
				}
			}
			if typ == model.TypeSOA {
				soaCount += len(recs)
				if name != apex || len(recs) != 1 {
					return biz.New(biz.CodeZone, "exactly one SOA record at the apex is required")
				}
			}
		}
		if cnames := byType[model.TypeCNAME]; len(cnames) > 0 {
			if len(cnames) != 1 || ownerTypeCount(byType) != 1 {
				return biz.New(biz.CodeZone, fmt.Sprintf("owner %s has CNAME plus other data or multiple CNAMEs", name))
			}
		}
	}
	if soaCount != 1 {
		return biz.New(biz.CodeZone, "exactly one SOA record at the apex is required")
	}
	return nil
}

func ownerTypeCount(byType map[model.Type][]ZoneRecord) int {
	n := 0
	for _, recs := range byType {
		if len(recs) > 0 {
			n++
		}
	}
	return n
}

func (z *Zone) Apex() string           { return z.apex }
func (z *Zone) Seq() uint64            { return z.seq }
func (z *Zone) MaxCNAMEHops() int      { return z.opts.MaxCNAMEHops }
func (z *Zone) NegativeTTLCap() uint32 { return z.opts.NegativeTTLCap }

func (z *Zone) RecordCount() int {
	n := 0
	for _, byType := range z.records {
		for _, recs := range byType {
			n += len(recs)
		}
	}
	return n
}

func (z *Zone) Origins() []string {
	set := make(map[string]bool)
	for _, byType := range z.records {
		for _, recs := range byType {
			for _, rec := range recs {
				for _, origin := range rec.Origins {
					set[origin] = true
				}
			}
		}
	}
	out := make([]string, 0, len(set))
	for origin := range set {
		out = append(out, origin)
	}
	sort.Strings(out)
	return out
}

func (z *Zone) Covers(name string) bool {
	return model.IsSubdomain(name, z.apex)
}

// ExactRRSet returns only records whose owner equals name exactly. Wildcards do
// not participate in conflict arbitration.
func (z *Zone) ExactRRSet(name string, typ model.Type) ([]model.Record, []string, bool) {
	recs, ok := z.records[name][typ]
	if !ok || len(recs) == 0 {
		return nil, nil, false
	}
	out := make([]model.Record, 0, len(recs))
	origins := make([]string, 0)
	for _, rec := range recs {
		out = append(out, rec.Record)
		origins = append(origins, rec.Origins...)
	}
	model.SortRecords(out)
	origins = mergeOrigins(origins, nil)
	return out, origins, true
}

func (z *Zone) SOA() (model.Record, uint32, bool) {
	if z.soa == nil {
		return model.Record{}, 0, false
	}
	soa, err := model.ParseSOAData(z.soa.Record.Data)
	if err != nil {
		return model.Record{}, 0, false
	}
	return z.soa.Record, soa.Minimum, true
}

type lookupOne struct {
	QTypeRecords []ZoneRecord
	CNAME        []ZoneRecord
	OwnerFound   bool
	Wildcard     bool
}

func (z *Zone) lookupOne(name string, typ model.Type) lookupOne {
	if byType, ok := z.records[name]; ok {
		return lookupFromNode(name, byType, typ, false)
	}
	candidate, ok := model.WildcardCandidate(name)
	if !ok {
		return lookupOne{}
	}
	if byType, exists := z.records[candidate]; exists {
		return lookupFromNode(name, byType, typ, true)
	}
	return lookupOne{}
}

func lookupFromNode(effectiveName string, byType map[model.Type][]ZoneRecord, typ model.Type, wildcard bool) lookupOne {
	result := lookupOne{OwnerFound: ownerTypeCount(byType) > 0, Wildcard: wildcard}
	if typ == model.TypeCNAME {
		result.QTypeRecords = copyHits(effectiveName, byType[model.TypeCNAME], wildcard)
		return result
	}
	result.CNAME = copyHits(effectiveName, byType[model.TypeCNAME], wildcard)
	if len(result.CNAME) > 0 {
		return result
	}
	result.QTypeRecords = copyHits(effectiveName, byType[typ], wildcard)
	return result
}

func copyHits(effectiveName string, recs []ZoneRecord, wildcard bool) []ZoneRecord {
	out := make([]ZoneRecord, 0, len(recs))
	for _, rec := range recs {
		copied := rec
		copied.Record.Name = effectiveName
		copied.Origins = append([]string(nil), rec.Origins...)
		out = append(out, copied)
	}
	return out
}

func hitMode(wildcard bool) model.MatchMode {
	if wildcard {
		return model.MatchWildcard
	}
	return model.MatchExact
}

func toHits(recs []ZoneRecord, wildcard bool) []Hit {
	out := make([]Hit, 0, len(recs))
	for _, rec := range recs {
		out = append(out, Hit{Record: rec.Record, Match: hitMode(wildcard), Origin: rec.OriginString()})
	}
	return out
}

func (z *Zone) Resolve(q model.Question) (Resolution, error) {
	name, err := model.CanonicalName(q.Name)
	if err != nil {
		return Resolution{}, biz.New(biz.CodeQuery, "invalid question name: "+err.Error())
	}
	q.Name = name
	if q.Class == 0 {
		q.Class = model.ClassIN
	}
	if q.Class != model.ClassIN {
		return Resolution{NeedForward: q.Name}, nil
	}
	current := q.Name
	seen := map[string]bool{current: true}
	answers := make([]Hit, 0, 4)
	chain := make([]string, 0, 4)
	hops := 0
	for {
		if !z.Covers(current) {
			return Resolution{Answers: answers, CNAMEChain: chain, NeedForward: current}, nil
		}
		one := z.lookupOne(current, q.Type)
		if len(one.CNAME) > 0 && q.Type != model.TypeCNAME {
			if len(one.CNAME) != 1 {
				return Resolution{}, biz.New(biz.CodeCNAME, "CNAME owner has multiple CNAME records")
			}
			cname := one.CNAME[0]
			target, err := model.CanonicalHostName(cname.Record.Data)
			if err != nil {
				return Resolution{}, biz.New(biz.CodeCNAME, "invalid CNAME target: "+err.Error())
			}
			answers = append(answers, toHits(one.CNAME, one.Wildcard)...)
			chain = append(chain, current+" -> "+target)
			hops++
			if hops > z.opts.MaxCNAMEHops {
				return Resolution{}, biz.New(biz.CodeCNAME, fmt.Sprintf("CNAME chain exceeds %d hops", z.opts.MaxCNAMEHops))
			}
			if seen[target] {
				return Resolution{}, biz.New(biz.CodeCNAME, "CNAME loop detected at "+target)
			}
			seen[target] = true
			current = target
			continue
		}
		if len(one.QTypeRecords) > 0 {
			answers = append(answers, toHits(one.QTypeRecords, one.Wildcard)...)
			return Resolution{Answers: answers, CNAMEChain: chain}, nil
		}
		// The owner exists (exactly or through a wildcard) but has no requested
		// type: NODATA. Otherwise this is NXDOMAIN.
		rcode := model.RCodeNXDomain
		if one.OwnerFound {
			rcode = model.RCodeNoError
		}
		negativeTTL, soaHit := z.negativeSOA()
		authorities := []Hit(nil)
		if soaHit != nil {
			authorities = append(authorities, *soaHit)
		}
		return Resolution{
			RCode:       rcode,
			Answers:     answers,
			Authorities: authorities,
			Negative:    true,
			NegativeTTL: negativeTTL,
			CNAMEChain:  chain,
		}, nil
	}
}

func (z *Zone) negativeSOA() (uint32, *Hit) {
	if z.soa == nil {
		return 0, nil
	}
	soaData, err := model.ParseSOAData(z.soa.Record.Data)
	if err != nil {
		return 0, nil
	}
	ttl := z.soa.Record.TTL
	if soaData.Minimum < ttl {
		ttl = soaData.Minimum
	}
	if z.opts.NegativeTTLCap < ttl {
		ttl = z.opts.NegativeTTLCap
	}
	rec := z.soa.Record
	rec.TTL = ttl
	return ttl, &Hit{Record: rec, Match: model.MatchNegative, Origin: z.soa.OriginString()}
}

// LastLabelMatchesWildcard reports whether the qname can be matched by the
// single leftmost-label wildcard candidate. Used by tests and diagnostics.
func (z *Zone) LastLabelMatchesWildcard(qname string) bool {
	candidate, ok := model.WildcardCandidate(qname)
	if !ok {
		return false
	}
	_, exists := z.records[candidate]
	return exists
}

// LoadWireMessage loads a sample in plain DNS wire format or with the two-byte
// TCP length prefix. It returns every answer record in normalized form.
func LoadWireMessage(path string) (dnswire.Message, []model.Record, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return dnswire.Message{}, nil, &biz.Error{Code: biz.CodeWire, Detail: "cannot read wire sample", Cause: err}
	}
	payload := data
	offsetBase := 0
	if len(data) >= 2 {
		prefixedLen := int(binary.BigEndian.Uint16(data[:2]))
		if prefixedLen == len(data)-2 {
			payload = data[2:]
			offsetBase = 2
		}
	}
	msg, wireErr := dnswire.ParseMessage(payload)
	if wireErr != nil {
		if wireErr.HasOffset {
			wireErr.Offset += offsetBase
		}
		return dnswire.Message{}, nil, wireErr
	}
	records := make([]model.Record, 0, len(msg.Answers))
	for i, rr := range msg.Answers {
		typ, err := model.ParseAuthorityType(rr.Type.String())
		if err != nil {
			return dnswire.Message{}, nil, biz.New(biz.CodeZone, fmt.Sprintf("wire answer[%d]: %s", i, err.Error()))
		}
		rec, err := model.NormalizeRecord(rr.Name, typ, model.Class(rr.Class), rr.TTL, rr.Data)
		if err != nil {
			return dnswire.Message{}, nil, biz.New(biz.CodeZone, fmt.Sprintf("wire answer[%d]: %s", i, err.Error()))
		}
		records = append(records, rec)
	}
	return msg, records, nil
}
