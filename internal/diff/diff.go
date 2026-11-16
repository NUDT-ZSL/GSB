package diff

import (
	"encoding/json"
	"sort"

	"localmesh/internal/model"
	"localmesh/internal/rules"
)

type RouteChange struct {
	Prefix string       `json:"prefix"`
	Change string       `json:"change"` // added, removed, modified
	Before *rules.Route `json:"before,omitempty"`
	After  *rules.Route `json:"after,omitempty"`
}

type InstanceChange struct {
	InstanceID string          `json:"instance_id"`
	Service    string          `json:"service"`
	Change     string          `json:"change"` // added, removed, modified
	Before     *rules.Instance `json:"before,omitempty"`
	After      *rules.Instance `json:"after,omitempty"`
}

type Report struct {
	FromRound        string           `json:"from_round"`
	ToRound          string           `json:"to_round"`
	FromVersion      string           `json:"from_version"`
	ToVersion        string           `json:"to_version"`
	VersionChanged   bool             `json:"version_changed"`
	ChangedRoutes    []RouteChange    `json:"changed_routes"`
	ChangedInstances []InstanceChange `json:"changed_instances"`
}

func Compare(from, to *model.Document) Report {
	out := Report{
		FromRound:      from.Round.ID,
		ToRound:        to.Round.ID,
		FromVersion:    from.Round.RulesVersion,
		ToVersion:      to.Round.RulesVersion,
		VersionChanged: from.Round.RulesVersion != to.Round.RulesVersion,
	}
	fromRoutes := routeMap(from.Rules.Routes)
	toRoutes := routeMap(to.Rules.Routes)
	prefixes := unionKeys(fromRoutes, toRoutes)
	for _, prefix := range prefixes {
		a, aok := fromRoutes[prefix]
		b, bok := toRoutes[prefix]
		switch {
		case !aok:
			bb := b
			out.ChangedRoutes = append(out.ChangedRoutes, RouteChange{Prefix: prefix, Change: "added", After: &bb})
		case !bok:
			aa := a
			out.ChangedRoutes = append(out.ChangedRoutes, RouteChange{Prefix: prefix, Change: "removed", Before: &aa})
		case !jsonEqual(a, b) || from.RouteFingerprints[prefix] != to.RouteFingerprints[prefix]:
			aa, bb := a, b
			out.ChangedRoutes = append(out.ChangedRoutes, RouteChange{Prefix: prefix, Change: "modified", Before: &aa, After: &bb})
		}
	}
	fromInstances := instanceMap(from.Rules.Services)
	toInstances := instanceMap(to.Rules.Services)
	ids := unionKeys(fromInstances, toInstances)
	for _, id := range ids {
		a, aok := fromInstances[id]
		b, bok := toInstances[id]
		switch {
		case !aok:
			bb := b.instance
			out.ChangedInstances = append(out.ChangedInstances, InstanceChange{InstanceID: id, Service: b.service, Change: "added", After: &bb})
		case !bok:
			aa := a.instance
			out.ChangedInstances = append(out.ChangedInstances, InstanceChange{InstanceID: id, Service: a.service, Change: "removed", Before: &aa})
		case a.service != b.service || !jsonEqual(a.instance, b.instance) || from.InstanceFingerprints[id] != to.InstanceFingerprints[id]:
			aa, bb := a.instance, b.instance
			out.ChangedInstances = append(out.ChangedInstances, InstanceChange{InstanceID: id, Service: b.service, Change: "modified", Before: &aa, After: &bb})
		}
	}
	return out
}

type instanceEntry struct {
	service  string
	instance rules.Instance
}

func routeMap(routes []rules.Route) map[string]rules.Route {
	out := make(map[string]rules.Route, len(routes))
	for _, route := range routes {
		out[route.Prefix] = route
	}
	return out
}

func instanceMap(services []rules.Service) map[string]instanceEntry {
	out := make(map[string]instanceEntry)
	for _, svc := range services {
		for _, inst := range svc.Instances {
			out[inst.ID] = instanceEntry{service: svc.Name, instance: inst}
		}
	}
	return out
}

func unionKeys[V any](a, b map[string]V) []string {
	set := make(map[string]bool, len(a)+len(b))
	for k := range a {
		set[k] = true
	}
	for k := range b {
		set[k] = true
	}
	keys := make([]string, 0, len(set))
	for k := range set {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func jsonEqual(a, b any) bool {
	ab, _ := json.Marshal(a)
	bb, _ := json.Marshal(b)
	return string(ab) == string(bb)
}
