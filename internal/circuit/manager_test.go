package circuit

import (
	"testing"
	"time"

	"localmesh/internal/rules"
)

func TestBreakerOpenSkipHalfOpenProbeAndClose(t *testing.T) {
	set := &rules.RuleSet{Config: rules.Config{Services: []rules.Service{{
		Name: "svc",
		Instances: []rules.Instance{
			{ID: "a", Weight: 1, FailureThreshold: 1, CooldownMS: 50},
			{ID: "b", Weight: 1, FailureThreshold: 1, CooldownMS: 50},
		},
	}}}}
	order := []rules.Instance{set.Config.Services[0].Instances[0], set.Config.Services[0].Instances[1]}
	m := New(set, "round")
	wall := time.Unix(0, 0)
	got, ok, events, _ := m.Select(order, "r1", 0, wall)
	if !ok || got.ID != "a" || len(events) != 0 {
		t.Fatalf("initial select got=%s ok=%t events=%v", got.ID, ok, events)
	}
	openEvents := m.Failure("a", "r1", "E_HTTP", 10*int64(time.Millisecond), wall)
	if len(openEvents) != 1 || openEvents[0].Type != "breaker_open" {
		t.Fatalf("open events=%v", openEvents)
	}
	got, ok, _, _ = m.Select(order, "r2", 20*int64(time.Millisecond), wall)
	if !ok || got.ID != "b" {
		t.Fatalf("open circuit was selected or healthy instance skipped: %s %t", got.ID, ok)
	}
	got, ok, events, _ = m.Select(order, "r3", 70*int64(time.Millisecond), wall)
	if !ok || got.ID != "a" || len(events) != 1 || events[0].Type != "breaker_half_open" {
		t.Fatalf("half-open probe = %s %t %v", got.ID, ok, events)
	}
	closeEvents := m.Success("a", "r3", 80*int64(time.Millisecond), wall)
	if len(closeEvents) != 1 || closeEvents[0].Type != "breaker_close" {
		t.Fatalf("close events=%v", closeEvents)
	}
	if state := m.Snapshot()["a"]; state.State != "closed" || state.ConsecutiveFailures != 0 {
		t.Fatalf("state=%+v", state)
	}
}

func TestFastFailListsOnlyCandidateOpenCircuits(t *testing.T) {
	set := &rules.RuleSet{Config: rules.Config{Services: []rules.Service{
		{Name: "a", Instances: []rules.Instance{{ID: "a1", Weight: 1, FailureThreshold: 1, CooldownMS: 100}}},
		{Name: "b", Instances: []rules.Instance{{ID: "b1", Weight: 1, FailureThreshold: 1, CooldownMS: 100}}},
	}}}
	m := New(set, "round")
	wall := time.Unix(0, 0)
	_, _, _, _ = m.Select(set.Config.Services[0].Instances, "r1", 0, wall)
	_ = m.Failure("a1", "r1", "E_HTTP", 1, wall)
	_, _, _, _ = m.Select(set.Config.Services[1].Instances, "r2", 0, wall)
	_ = m.Failure("b1", "r2", "E_HTTP", 1, wall)
	_, ok, _, open := m.Select(set.Config.Services[0].Instances, "r3", 2, wall)
	if ok || len(open) != 1 || open[0] != "a1" {
		t.Fatalf("fast fail ok=%t open=%v", ok, open)
	}
}
