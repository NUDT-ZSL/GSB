package circuit

import (
	"sort"
	"sync"
	"time"

	"localmesh/internal/model"
	"localmesh/internal/rules"
)

type record struct {
	service             string
	instanceID          string
	state               string
	consecutiveFailures int
	threshold           int
	cooldownMS          int64
	openedNS            int64
	openedAt            string
	cooldownUntilNS     int64
	lastFailureAt       string
}

type Manager struct {
	mu      sync.Mutex
	records map[string]*record
	roundID string
}

func New(set *rules.RuleSet, roundID string) *Manager {
	m := &Manager{records: make(map[string]*record), roundID: roundID}
	for _, svc := range set.Config.Services {
		for _, inst := range svc.Instances {
			m.records[inst.ID] = &record{
				service:    svc.Name,
				instanceID: inst.ID,
				state:      "closed",
				threshold:  inst.FailureThreshold,
				cooldownMS: inst.CooldownMS,
			}
		}
	}
	return m
}

// Select returns the first candidate that is not cooling down. It may promote
// an expired open circuit to half-open; only the caller receiving ok=true can
// run that probe. If no candidate is available it returns all triggering open
// instance IDs for a fast-fail record.
func (m *Manager) Select(order []rules.Instance, requestID string, nowNS int64, wall time.Time) (rules.Instance, bool, []model.CircuitEvent, []string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var events []model.CircuitEvent
	for _, inst := range order {
		rec := m.records[inst.ID]
		if rec == nil {
			continue
		}
		switch rec.state {
		case "closed":
			return inst, true, events, nil
		case "open":
			if nowNS >= rec.cooldownUntilNS {
				rec.state = "half_open"
				events = append(events, model.CircuitEvent{
					Type:                "breaker_half_open",
					RoundID:             m.roundID,
					RequestID:           requestID,
					Service:             rec.service,
					InstanceID:          rec.instanceID,
					Reason:              "cooldown_elapsed_probe",
					MonoNS:              nowNS,
					TriggeredAt:         wall.UTC().Format(time.RFC3339Nano),
					CooldownMS:          rec.cooldownMS,
					CooldownUntilMonoMS: rec.cooldownUntilNS / int64(time.Millisecond),
					CooldownUntilNS:     rec.cooldownUntilNS,
				})
				return inst, true, events, nil
			}
		}
	}
	open := make([]string, 0)
	for _, inst := range order {
		if rec := m.records[inst.ID]; rec != nil && rec.state == "open" {
			open = append(open, inst.ID)
		}
	}
	sort.Strings(open)
	return rules.Instance{}, false, events, open
}

func (m *Manager) Success(instanceID, requestID string, nowNS int64, wall time.Time) []model.CircuitEvent {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec := m.records[instanceID]
	if rec == nil {
		return nil
	}
	if rec.state == "open" {
		return nil
	}
	if rec.state == "half_open" {
		rec.state = "closed"
		rec.consecutiveFailures = 0
		return []model.CircuitEvent{{
			Type:        "breaker_close",
			RoundID:     m.roundID,
			RequestID:   requestID,
			Service:     rec.service,
			InstanceID:  rec.instanceID,
			Reason:      "probe_succeeded",
			MonoNS:      nowNS,
			TriggeredAt: wall.UTC().Format(time.RFC3339Nano),
			CooldownMS:  rec.cooldownMS,
		}}
	}
	rec.consecutiveFailures = 0
	return nil
}

func (m *Manager) Failure(instanceID, requestID, reason string, nowNS int64, wall time.Time) []model.CircuitEvent {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec := m.records[instanceID]
	if rec == nil {
		return nil
	}
	if rec.state == "open" {
		return nil
	}
	wasHalfOpen := rec.state == "half_open"
	rec.consecutiveFailures++
	rec.lastFailureAt = wall.UTC().Format(time.RFC3339Nano)
	openNow := wasHalfOpen || rec.consecutiveFailures >= rec.threshold
	if !openNow {
		return nil
	}
	rec.state = "open"
	rec.openedNS = nowNS
	rec.openedAt = wall.UTC().Format(time.RFC3339Nano)
	rec.cooldownUntilNS = nowNS + rec.cooldownMS*int64(time.Millisecond)
	if wasHalfOpen {
		reason = "half_open_probe_failed"
	} else if reason == "" {
		reason = "failure_threshold_reached"
	}
	return []model.CircuitEvent{{
		Type:                "breaker_open",
		RoundID:             m.roundID,
		RequestID:           requestID,
		Service:             rec.service,
		InstanceID:          rec.instanceID,
		Reason:              reason,
		MonoNS:              nowNS,
		TriggeredAt:         rec.openedAt,
		CooldownMS:          rec.cooldownMS,
		CooldownUntilMonoMS: rec.cooldownUntilNS / int64(time.Millisecond),
		CooldownUntilNS:     rec.cooldownUntilNS,
	}}
}

func (m *Manager) Snapshot() map[string]model.CircuitState {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make(map[string]model.CircuitState, len(m.records))
	for id, rec := range m.records {
		out[id] = model.CircuitState{
			InstanceID:          id,
			Service:             rec.service,
			State:               rec.state,
			ConsecutiveFailures: rec.consecutiveFailures,
			FailureThreshold:    rec.threshold,
			CooldownMS:          rec.cooldownMS,
			OpenedMonoMS:        rec.openedNS / int64(time.Millisecond),
			OpenedAt:            rec.openedAt,
			CooldownUntilMonoMS: rec.cooldownUntilNS / int64(time.Millisecond),
			CooldownUntilNS:     rec.cooldownUntilNS,
			LastFailureAt:       rec.lastFailureAt,
		}
	}
	return out
}
