package rules

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"sort"
	"strconv"
	"strings"

	"localmesh/internal/biz"
)

type Config struct {
	SchemaVersion     int       `json:"schema_version"`
	Version           string    `json:"version"`
	GlobalConcurrency int       `json:"global_concurrency"`
	Services          []Service `json:"services"`
	Routes            []Route   `json:"routes"`
}

type Service struct {
	Name      string     `json:"name"`
	Quota     int        `json:"quota"`
	Instances []Instance `json:"instances"`
}

type Instance struct {
	ID               string `json:"id"`
	Address          string `json:"address"`
	Weight           int    `json:"weight"`
	FailureThreshold int    `json:"failure_threshold"`
	CooldownMS       int64  `json:"cooldown_ms"`
}

type Route struct {
	Prefix    string `json:"prefix"`
	Service   string `json:"service"`
	TimeoutMS int64  `json:"timeout_ms"`
	Retries   int    `json:"retries"`
}

func (r Route) AttemptBudget() int { return r.Retries + 1 }

// RuleSet is an immutable, fully validated rule snapshot.
type RuleSet struct {
	Path            string
	Raw             []byte
	Hash            string
	VersionID       string
	DeclaredVersion string
	Config          Config
	ServiceByName   map[string]Service
	routeOrder      []Route
}

type rawFile struct {
	SchemaVersion     *int         `json:"schema_version"`
	Version           *string      `json:"version"`
	GlobalConcurrency *int         `json:"global_concurrency"`
	Services          []rawService `json:"services"`
	Routes            []rawRoute   `json:"routes"`
}

type rawService struct {
	Name      *string       `json:"name"`
	Quota     *int          `json:"quota"`
	Instances []rawInstance `json:"instances"`
}

type rawInstance struct {
	ID               *string `json:"id"`
	Address          *string `json:"address"`
	Weight           *int    `json:"weight"`
	FailureThreshold *int    `json:"failure_threshold"`
	CooldownMS       *int64  `json:"cooldown_ms"`
}

type rawRoute struct {
	Prefix    *string `json:"prefix"`
	Service   *string `json:"service"`
	TimeoutMS *int64  `json:"timeout_ms"`
	Retries   *int    `json:"retries"`
}

func Load(path string) (*RuleSet, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, biz.WithField(biz.Wrap(biz.CodeRule, "load_rules", "cannot read rules file", err), "file")
	}
	rs, err := Parse(path, raw)
	if err != nil {
		return nil, err
	}
	return rs, nil
}

// Parse validates all rules before returning anything. A failed parse never
// exposes a partially built routing table.
func Parse(path string, raw []byte) (*RuleSet, error) {
	var rf rawFile
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&rf); err != nil {
		return nil, decodeError(err)
	}
	var trailing any
	if err := dec.Decode(&trailing); err != io.EOF {
		if err == nil {
			return nil, biz.WithField(biz.New(biz.CodeRule, "parse_rules", "rules file contains more than one JSON value"), "file")
		}
		return nil, biz.WithField(biz.Wrap(biz.CodeRule, "parse_rules", "rules file has invalid trailing content", err), "file")
	}

	var violations []string
	add := func(field, msg string) { violations = append(violations, field+": "+msg) }

	schemaVersion := 1
	if rf.SchemaVersion != nil {
		schemaVersion = *rf.SchemaVersion
		if schemaVersion != 1 {
			add("schema_version", "must be 1")
		}
	}
	if rf.Version == nil || strings.TrimSpace(*rf.Version) == "" {
		add("version", "must be a non-empty string")
	}
	if rf.GlobalConcurrency == nil || *rf.GlobalConcurrency <= 0 {
		add("global_concurrency", "must be an integer greater than zero")
	}
	if len(rf.Services) == 0 {
		add("services", "must contain at least one service")
	}
	if len(rf.Routes) == 0 {
		add("routes", "must contain at least one route")
	}

	cfg := Config{SchemaVersion: schemaVersion}
	if rf.Version != nil {
		cfg.Version = strings.TrimSpace(*rf.Version)
	}
	if rf.GlobalConcurrency != nil {
		cfg.GlobalConcurrency = *rf.GlobalConcurrency
	}
	serviceNames := make(map[string]bool)
	instanceIDs := make(map[string]bool)
	for i, svc := range rf.Services {
		base := fmt.Sprintf("services[%d]", i)
		if svc.Name == nil || strings.TrimSpace(*svc.Name) == "" {
			add(base+".name", "must be a non-empty string")
			continue
		}
		name := strings.TrimSpace(*svc.Name)
		if serviceNames[name] {
			add(base+".name", "duplicate service name "+strconv.Quote(name))
		}
		serviceNames[name] = true
		if svc.Quota == nil || *svc.Quota <= 0 {
			add(base+".quota", "must be an integer greater than zero")
		}
		if len(svc.Instances) == 0 {
			add(base+".instances", "must contain at least one instance")
		}
		weightSum := 0
		normalized := Service{Name: name}
		if svc.Quota != nil {
			normalized.Quota = *svc.Quota
		}
		for j, inst := range svc.Instances {
			ibase := fmt.Sprintf("%s.instances[%d]", base, j)
			if inst.ID == nil || strings.TrimSpace(*inst.ID) == "" {
				add(ibase+".id", "must be a non-empty string")
				continue
			}
			id := strings.TrimSpace(*inst.ID)
			if instanceIDs[id] {
				add(ibase+".id", "duplicate instance id "+strconv.Quote(id))
			}
			instanceIDs[id] = true
			if inst.Address == nil {
				add(ibase+".address", "is required")
			} else if err := validateAddress(*inst.Address); err != nil {
				add(ibase+".address", err.Error())
			}
			weight := 0
			if inst.Weight == nil {
				add(ibase+".weight", "is required")
			} else {
				weight = *inst.Weight
				if weight < 0 {
					add(ibase+".weight", "must be a non-negative integer")
				}
			}
			if inst.FailureThreshold == nil || *inst.FailureThreshold <= 0 {
				add(ibase+".failure_threshold", "must be an integer greater than zero")
			}
			if inst.CooldownMS == nil || *inst.CooldownMS <= 0 {
				add(ibase+".cooldown_ms", "must be an integer greater than zero")
			}
			weightSum += weight
			addr := ""
			if inst.Address != nil {
				addr = *inst.Address
			}
			ni := Instance{ID: id, Address: addr, Weight: weight}
			if inst.FailureThreshold != nil {
				ni.FailureThreshold = *inst.FailureThreshold
			}
			if inst.CooldownMS != nil {
				ni.CooldownMS = *inst.CooldownMS
			}
			normalized.Instances = append(normalized.Instances, ni)
		}
		if weightSum == 0 {
			add(base+".instances", "instance weight sum must not be zero")
		}
		cfg.Services = append(cfg.Services, normalized)
	}

	prefixes := make(map[string]bool)
	for i, rt := range rf.Routes {
		base := fmt.Sprintf("routes[%d]", i)
		if rt.Prefix == nil || !strings.HasPrefix(*rt.Prefix, "/") {
			add(base+".prefix", "must start with /")
		} else if prefixes[*rt.Prefix] {
			add(base+".prefix", "duplicate route prefix "+strconv.Quote(*rt.Prefix))
		} else {
			prefixes[*rt.Prefix] = true
		}
		if rt.Service == nil || strings.TrimSpace(*rt.Service) == "" {
			add(base+".service", "must name a declared service")
		} else if !serviceNames[strings.TrimSpace(*rt.Service)] {
			add(base+".service", "unknown service "+strconv.Quote(strings.TrimSpace(*rt.Service)))
		}
		if rt.TimeoutMS == nil || *rt.TimeoutMS <= 0 {
			add(base+".timeout_ms", "must be an integer greater than zero")
		}
		if rt.Retries == nil || *rt.Retries < 0 {
			add(base+".retries", "must be a non-negative integer")
		}
		nr := Route{}
		if rt.Prefix != nil {
			nr.Prefix = *rt.Prefix
		}
		if rt.Service != nil {
			nr.Service = strings.TrimSpace(*rt.Service)
		}
		if rt.TimeoutMS != nil {
			nr.TimeoutMS = *rt.TimeoutMS
		}
		if rt.Retries != nil {
			nr.Retries = *rt.Retries
		}
		cfg.Routes = append(cfg.Routes, nr)
	}

	if len(violations) > 0 {
		first := strings.SplitN(violations[0], ":", 2)
		field := first[0]
		msg := violations[0]
		if len(first) == 2 {
			msg = strings.TrimSpace(first[1])
		}
		return nil, biz.WithFields(
			biz.WithField(biz.New(biz.CodeRule, "validate_rules", msg), field),
			map[string]any{"violations": violations, "file": path},
		)
	}

	sum := sha256.Sum256(raw)
	hash := hex.EncodeToString(sum[:])
	versionID := cfg.Version + "@sha256:" + hash[:16]
	rs := &RuleSet{
		Path:            path,
		Raw:             append([]byte(nil), raw...),
		Hash:            hash,
		VersionID:       versionID,
		DeclaredVersion: cfg.Version,
		Config:          cfg,
		ServiceByName:   make(map[string]Service, len(cfg.Services)),
		routeOrder:      append([]Route(nil), cfg.Routes...),
	}
	for _, svc := range cfg.Services {
		rs.ServiceByName[svc.Name] = svc
	}
	sort.SliceStable(rs.routeOrder, func(i, j int) bool {
		if len(rs.routeOrder[i].Prefix) == len(rs.routeOrder[j].Prefix) {
			return false
		}
		return len(rs.routeOrder[i].Prefix) > len(rs.routeOrder[j].Prefix)
	})
	return rs, nil
}

func decodeError(err error) error {
	field := "file"
	var syntax *json.SyntaxError
	var typeErr *json.UnmarshalTypeError
	msg := "invalid rules JSON"
	switch {
	case errorAs(err, &syntax):
		field = fmt.Sprintf("json@byte:%d", syntax.Offset)
		msg = syntax.Error()
	case errorAs(err, &typeErr):
		field = typeErr.Field
		if field == "" {
			field = fmt.Sprintf("json@byte:%d", typeErr.Offset)
		}
		msg = "invalid type: expected " + typeErr.Type.String()
	default:
		msg = err.Error()
	}
	return biz.WithField(biz.Wrap(biz.CodeRule, "parse_rules", msg, err), field)
}

// errorAs is a tiny indirection that keeps decodeError easy to test.
var errorAs = func(err error, target any) bool {
	switch t := target.(type) {
	case **json.SyntaxError:
		v, ok := err.(*json.SyntaxError)
		if ok {
			*t = v
		}
		return ok
	case **json.UnmarshalTypeError:
		v, ok := err.(*json.UnmarshalTypeError)
		if ok {
			*t = v
		}
		return ok
	default:
		return false
	}
}

func validateAddress(address string) error {
	host, portText, err := net.SplitHostPort(address)
	if err != nil {
		return fmt.Errorf("must be a literal host:port: %w", err)
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return fmt.Errorf("host %q is not an IP literal", host)
	}
	if !ip.IsLoopback() {
		return fmt.Errorf("host %q is not a loopback address", host)
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 1 || port > 65535 {
		return fmt.Errorf("port must be an integer from 1 to 65535")
	}
	return nil
}

func (rs *RuleSet) Match(path string) (Route, bool) {
	for _, rt := range rs.routeOrder {
		if strings.HasPrefix(path, rt.Prefix) {
			return rt, true
		}
	}
	return Route{}, false
}

func (rs *RuleSet) InstanceByID(id string) (Service, Instance, bool) {
	for _, svc := range rs.Config.Services {
		for _, inst := range svc.Instances {
			if inst.ID == id {
				return svc, inst, true
			}
		}
	}
	return Service{}, Instance{}, false
}

func (rs *RuleSet) RouteFingerprints() map[string]string {
	out := make(map[string]string, len(rs.Config.Routes))
	for _, rt := range rs.Config.Routes {
		b, _ := json.Marshal(rt)
		sum := sha256.Sum256(b)
		out[rt.Prefix] = hex.EncodeToString(sum[:])
	}
	return out
}

func (rs *RuleSet) InstanceFingerprints() map[string]string {
	out := make(map[string]string)
	for _, svc := range rs.Config.Services {
		for _, inst := range svc.Instances {
			b, _ := json.Marshal(struct {
				Service  string   `json:"service"`
				Instance Instance `json:"instance"`
			}{svc.Name, inst})
			sum := sha256.Sum256(b)
			out[inst.ID] = hex.EncodeToString(sum[:])
		}
	}
	return out
}

// Rehash detects changes that happen while a round is running.
func Rehash(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", biz.WithField(biz.Wrap(biz.CodeRule, "rehash_rules", "cannot reread rules file", err), "file")
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func (rs *RuleSet) VersionSlug() string {
	hash := rs.Hash
	if len(hash) > 12 {
		hash = hash[:12]
	}
	clean := strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			return r
		default:
			return '-'
		}
	}, rs.DeclaredVersion)
	clean = strings.Trim(clean, "-")
	if clean == "" {
		clean = "rules"
	}
	return clean + "-" + hash
}
