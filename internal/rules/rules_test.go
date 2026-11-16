package rules

import (
	"encoding/json"
	"strings"
	"testing"

	"localmesh/internal/biz"
)

const minimalRules = `{
  "schema_version": 1,
  "version": "v1",
  "global_concurrency": 2,
  "services": [{"name":"svc","quota":1,"instances":[
    {"id":"a","address":"127.0.0.1:19001","weight":3,"failure_threshold":2,"cooldown_ms":100},
    {"id":"b","address":"127.0.0.1:19002","weight":0,"failure_threshold":1,"cooldown_ms":100}
  ]}],
  "routes": [
    {"prefix":"/api","service":"svc","timeout_ms":500,"retries":1},
    {"prefix":"/api/long","service":"svc","timeout_ms":700,"retries":0}
  ]
}`

func TestParseValidAndMatchLongestPrefix(t *testing.T) {
	set, err := Parse("rules.json", []byte(minimalRules))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if set.VersionID == "" || len(set.Hash) != 64 {
		t.Fatalf("version metadata missing: %+v", set.VersionID)
	}
	route, ok := set.Match("/api/long/items")
	if !ok || route.Prefix != "/api/long" {
		t.Fatalf("match = %+v, %v", route, ok)
	}
}

func TestValidationRejectsWholeFile(t *testing.T) {
	tests := []struct {
		name   string
		field  string
		mutate func(map[string]any)
	}{
		{name: "duplicate service", field: "services[1].name", mutate: func(m map[string]any) {
			services := m["services"].([]any)
			services = append(services, services[0])
			m["services"] = services
		}},
		{name: "non-loopback address", field: "services[0].instances[0].address", mutate: func(m map[string]any) {
			instance(m, 0, 0)["address"] = "10.0.0.1:19001"
		}},
		{name: "port zero", field: "services[0].instances[0].address", mutate: func(m map[string]any) {
			instance(m, 0, 0)["address"] = "127.0.0.1:0"
		}},
		{name: "negative weight", field: "services[0].instances[0].weight", mutate: func(m map[string]any) {
			instance(m, 0, 0)["weight"] = -1
		}},
		{name: "zero weight sum", field: "services[0].instances", mutate: func(m map[string]any) {
			instance(m, 0, 0)["weight"] = 0
			instance(m, 0, 1)["weight"] = 0
		}},
		{name: "bad route prefix", field: "routes[0].prefix", mutate: func(m map[string]any) {
			route(m, 0)["prefix"] = "api"
		}},
		{name: "zero timeout", field: "routes[0].timeout_ms", mutate: func(m map[string]any) {
			route(m, 0)["timeout_ms"] = 0
		}},
		{name: "negative retries", field: "routes[0].retries", mutate: func(m map[string]any) {
			route(m, 0)["retries"] = -1
		}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var value map[string]any
			if err := json.Unmarshal([]byte(minimalRules), &value); err != nil {
				t.Fatal(err)
			}
			tc.mutate(value)
			raw, _ := json.Marshal(value)
			set, err := Parse("bad.json", raw)
			if err == nil || set != nil {
				t.Fatal("expected a rejected parse with no partial rule set")
			}
			if biz.CodeOf(err) != biz.CodeRule {
				t.Fatalf("code=%s", biz.CodeOf(err))
			}
			if !strings.Contains(err.Error(), tc.field) {
				t.Fatalf("error %q does not locate field %s", err, tc.field)
			}
		})
	}
}

func TestNonIntegerBudgetRejected(t *testing.T) {
	raw := strings.Replace(minimalRules, `"timeout_ms":500`, `"timeout_ms":500.5`, 1)
	set, err := Parse("bad.json", []byte(raw))
	if err == nil || set != nil {
		t.Fatal("decimal timeout must be rejected")
	}
	if biz.CodeOf(err) != biz.CodeRule {
		t.Fatalf("code=%s", biz.CodeOf(err))
	}
}

func TestTrailingContentRejected(t *testing.T) {
	for _, suffix := range []string{"\n{}", "\ngarbage"} {
		set, err := Parse("bad.json", []byte(minimalRules+suffix))
		if err == nil || set != nil {
			t.Fatalf("suffix %q was accepted", suffix)
		}
		if biz.CodeOf(err) != biz.CodeRule {
			t.Fatalf("code=%s", biz.CodeOf(err))
		}
	}
}

func instance(m map[string]any, serviceIndex, instanceIndex int) map[string]any {
	return m["services"].([]any)[serviceIndex].(map[string]any)["instances"].([]any)[instanceIndex].(map[string]any)
}

func route(m map[string]any, index int) map[string]any {
	return m["routes"].([]any)[index].(map[string]any)
}
