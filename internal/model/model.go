package model

import (
	"fmt"
	"net"
	"sort"
	"strconv"
	"strings"

	"localdns/internal/biz"
)

type Type uint16

const (
	TypeA     Type = 1
	TypeNS    Type = 2
	TypeCNAME Type = 5
	TypeSOA   Type = 6
	TypeAAAA  Type = 28
	TypeOPT   Type = 41
)

func (t Type) String() string {
	switch t {
	case TypeA:
		return "A"
	case TypeNS:
		return "NS"
	case TypeCNAME:
		return "CNAME"
	case TypeSOA:
		return "SOA"
	case TypeAAAA:
		return "AAAA"
	case TypeOPT:
		return "OPT"
	default:
		return fmt.Sprintf("TYPE%d", uint16(t))
	}
}

func ParseType(s string) (Type, error) {
	switch strings.ToUpper(strings.TrimSpace(s)) {
	case "A":
		return TypeA, nil
	case "NS":
		return TypeNS, nil
	case "CNAME":
		return TypeCNAME, nil
	case "SOA":
		return TypeSOA, nil
	case "AAAA":
		return TypeAAAA, nil
	case "OPT":
		return TypeOPT, nil
	}
	upper := strings.ToUpper(strings.TrimSpace(s))
	if strings.HasPrefix(upper, "TYPE") {
		n, err := strconv.ParseUint(strings.TrimPrefix(upper, "TYPE"), 10, 16)
		if err == nil {
			return Type(n), nil
		}
	}
	return 0, biz.New(biz.CodeZone, "unsupported record type "+strconv.Quote(s))
}

func ParseAuthorityType(s string) (Type, error) {
	t, err := ParseType(s)
	if err != nil {
		return 0, err
	}
	switch t {
	case TypeA, TypeAAAA, TypeCNAME, TypeNS, TypeSOA:
		return t, nil
	default:
		return 0, biz.New(biz.CodeZone, "record type is not supported by local authority: "+s)
	}
}

type Class uint16

const ClassIN Class = 1

func (c Class) String() string {
	if c == ClassIN {
		return "IN"
	}
	return fmt.Sprintf("CLASS%d", uint16(c))
}

type SourceKind string

const (
	SourceAuthority SourceKind = "authority"
	SourceNegative  SourceKind = "negative"
	SourceUpstream  SourceKind = "upstream"
)

type MatchMode string

const (
	MatchExact             MatchMode = "exact"
	MatchWildcard          MatchMode = "wildcard"
	MatchForward           MatchMode = "forward"
	MatchNegative          MatchMode = "negative"
	MatchConflictAuthority MatchMode = "conflict-authority"
)

type Record struct {
	Name  string `json:"name"`
	Type  Type   `json:"type"`
	Class Class  `json:"class"`
	TTL   uint32 `json:"ttl"`
	Data  string `json:"data"`
}

type Question struct {
	Name  string `json:"name"`
	Type  Type   `json:"type"`
	Class Class  `json:"class"`
}

func (r Record) TypeString() string { return r.Type.String() }

type Provenance struct {
	Source    SourceKind `json:"source"`
	Origin    string     `json:"origin,omitempty"`
	Match     MatchMode  `json:"match"`
	SourceSeq uint64     `json:"source_seq"`
}

type AnswerRecord struct {
	Record     Record     `json:"record"`
	Provenance Provenance `json:"provenance"`
}

type ConflictSide struct {
	Source    SourceKind `json:"source"`
	SourceSeq uint64     `json:"source_seq"`
	Origin    string     `json:"origin,omitempty"`
	Records   []Record   `json:"records"`
}

type Conflict struct {
	Name     string       `json:"name"`
	Type     Type         `json:"type"`
	Local    ConflictSide `json:"local"`
	Upstream ConflictSide `json:"upstream"`
	Winner   SourceKind   `json:"winner"`
}

// Resolution is the transport-independent answer produced by the engine.
type Resolution struct {
	RCode         int            `json:"rcode"`
	Answers       []AnswerRecord `json:"answers"`
	Authorities   []AnswerRecord `json:"authorities,omitempty"`
	Negative      bool           `json:"negative"`
	NegativeTTL   uint32         `json:"negative_ttl,omitempty"`
	CNAMEChain    []string       `json:"cname_chain,omitempty"`
	PrimarySource SourceKind     `json:"primary_source"`
	SourceSeq     uint64         `json:"source_seq"`
	Conflict      *Conflict      `json:"conflict,omitempty"`
}

const (
	RCodeNoError  = 0
	RCodeFormErr  = 1
	RCodeServFail = 2
	RCodeNXDomain = 3
	RCodeNotImp   = 4
)

func RCodeName(rcode int) string {
	switch rcode {
	case RCodeNoError:
		return "NOERROR"
	case RCodeFormErr:
		return "FORMERR"
	case RCodeServFail:
		return "SERVFAIL"
	case RCodeNXDomain:
		return "NXDOMAIN"
	case RCodeNotImp:
		return "NOTIMP"
	default:
		return fmt.Sprintf("RCODE%d", rcode)
	}
}

func ASCIILower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + ('a' - 'A')
		}
	}
	return string(b)
}

// CanonicalName validates and normalizes a presentation-format DNS name. The
// normalized form is lower-case ASCII and has no trailing dot; root is ".".
func CanonicalName(s string) (string, error) {
	return canonicalName(s, true)
}

func CanonicalHostName(s string) (string, error) {
	return canonicalName(s, false)
}

func canonicalName(s string, allowWildcard bool) (string, error) {
	if s == "" {
		return "", fmt.Errorf("name is empty")
	}
	if s == "." {
		return ".", nil
	}
	if strings.HasSuffix(s, ".") {
		s = s[:len(s)-1]
		if s == "" {
			return ".", nil
		}
	}
	if len(s) > 253 {
		return "", fmt.Errorf("name exceeds 253 octets")
	}
	labels := strings.Split(s, ".")
	for i, label := range labels {
		if label == "" {
			return "", fmt.Errorf("name contains an empty label")
		}
		if len(label) > 63 {
			return "", fmt.Errorf("label %q exceeds 63 octets", label)
		}
		for _, c := range []byte(label) {
			if c < 0x21 || c > 0x7e || c == '.' {
				return "", fmt.Errorf("label contains non-ASCII or control byte 0x%02x", c)
			}
			if c == '*' {
				if !allowWildcard || i != 0 || label != "*" {
					return "", fmt.Errorf("wildcard is only allowed as a complete leftmost label")
				}
			}
		}
	}
	return ASCIILower(strings.Join(labels, ".")), nil
}

func IsSubdomain(name, apex string) bool {
	if name == apex {
		return true
	}
	if apex == "." {
		return true
	}
	return strings.HasSuffix(name, "."+apex)
}

func ParentName(name string) string {
	if name == "." {
		return "."
	}
	i := strings.IndexByte(name, '.')
	if i < 0 {
		return "."
	}
	return name[i+1:]
}

func WildcardCandidate(name string) (string, bool) {
	if name == "." {
		return "", false
	}
	parent := ParentName(name)
	if parent == "." {
		return "*", true
	}
	return "*." + parent, true
}

func NormalizeRDATA(t Type, text string) (string, error) {
	text = strings.TrimSpace(text)
	switch t {
	case TypeA:
		ip := net.ParseIP(text)
		if ip == nil || ip.To4() == nil {
			return "", fmt.Errorf("invalid IPv4 address %q", text)
		}
		return ip.To4().String(), nil
	case TypeAAAA:
		ip := net.ParseIP(text)
		if ip == nil || ip.To16() == nil || ip.To4() != nil {
			return "", fmt.Errorf("invalid IPv6 address %q", text)
		}
		return ip.To16().String(), nil
	case TypeCNAME, TypeNS:
		name, err := CanonicalHostName(text)
		if err != nil {
			return "", fmt.Errorf("invalid target name: %w", err)
		}
		return name, nil
	case TypeSOA:
		fields := strings.Fields(text)
		if len(fields) != 7 {
			return "", fmt.Errorf("SOA requires 7 fields: mname rname serial refresh retry expire minimum")
		}
		mname, err := CanonicalHostName(fields[0])
		if err != nil {
			return "", fmt.Errorf("invalid SOA mname: %w", err)
		}
		rname, err := CanonicalHostName(fields[1])
		if err != nil {
			return "", fmt.Errorf("invalid SOA rname: %w", err)
		}
		nums := make([]uint32, 5)
		for i := range nums {
			n, err := strconv.ParseUint(fields[i+2], 10, 32)
			if err != nil {
				return "", fmt.Errorf("invalid SOA numeric field %q", fields[i+2])
			}
			nums[i] = uint32(n)
		}
		return fmt.Sprintf("%s %s %d %d %d %d %d", mname, rname, nums[0], nums[1], nums[2], nums[3], nums[4]), nil
	default:
		return "", fmt.Errorf("unsupported record type %s", t.String())
	}
}

func NormalizeRecord(name string, t Type, class Class, ttl uint32, rdata string) (Record, error) {
	canonical, err := CanonicalName(name)
	if err != nil {
		return Record{}, fmt.Errorf("invalid owner name: %w", err)
	}
	data, err := NormalizeRDATA(t, rdata)
	if err != nil {
		return Record{}, err
	}
	return Record{Name: canonical, Type: t, Class: class, TTL: ttl, Data: data}, nil
}

type SOAData struct {
	MName   string
	RName   string
	Serial  uint32
	Refresh uint32
	Retry   uint32
	Expire  uint32
	Minimum uint32
}

func ParseSOAData(text string) (SOAData, error) {
	fields := strings.Fields(text)
	if len(fields) != 7 {
		return SOAData{}, fmt.Errorf("SOA requires 7 fields")
	}
	nums := make([]uint32, 5)
	for i := range nums {
		n, err := strconv.ParseUint(fields[i+2], 10, 32)
		if err != nil {
			return SOAData{}, err
		}
		nums[i] = uint32(n)
	}
	return SOAData{
		MName: fields[0], RName: fields[1], Serial: nums[0], Refresh: nums[1],
		Retry: nums[2], Expire: nums[3], Minimum: nums[4],
	}, nil
}

func SortRecords(records []Record) {
	sort.Slice(records, func(i, j int) bool {
		a, b := records[i], records[j]
		if a.Name != b.Name {
			return a.Name < b.Name
		}
		if a.Type != b.Type {
			return a.Type < b.Type
		}
		if a.Class != b.Class {
			return a.Class < b.Class
		}
		if a.Data != b.Data {
			return a.Data < b.Data
		}
		return a.TTL < b.TTL
	})
}

func RRSetEqual(a, b []Record) bool {
	if len(a) != len(b) {
		return false
	}
	ac := append([]Record(nil), a...)
	bc := append([]Record(nil), b...)
	SortRecords(ac)
	SortRecords(bc)
	for i := range ac {
		if ac[i].Name != bc[i].Name || ac[i].Type != bc[i].Type || ac[i].Class != bc[i].Class || ac[i].Data != bc[i].Data {
			return false
		}
	}
	return true
}
