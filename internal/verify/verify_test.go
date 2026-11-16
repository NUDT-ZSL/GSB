package verify

import (
	"testing"

	"localmesh/internal/model"
)

func TestCircuitCooldownSelectionDetected(t *testing.T) {
	events := []model.TraceEvent{
		{Type: "breaker_open", RequestID: "r1", InstanceID: "a", MonoNS: 100, CooldownUntilNS: 500},
		{Type: "instance_selected", RequestID: "r2", InstanceID: "a", MonoNS: 200},
		{Type: "instance_selected", RequestID: "r3", InstanceID: "a", MonoNS: 600},
	}
	var deviations []model.Deviation
	add := func(requestID, field, format string, args ...any) {
		deviations = append(deviations, model.Deviation{RequestID: requestID, Field: field})
	}
	verifyCircuitCooldowns(events, add)
	if len(deviations) != 1 || deviations[0].RequestID != "r2" || deviations[0].Field != "circuit_cooldown" {
		t.Fatalf("deviations=%+v", deviations)
	}
}

func TestConcurrencySweepDetectsQuotaOverrun(t *testing.T) {
	doc := &model.Document{Quota: model.QuotaStats{GlobalLimit: 1, ServiceLimits: map[string]int{"svc": 1}}}
	events := []concurrencyEvent{
		{ns: 100, kind: 1, request: "r1", service: "svc"},
		{ns: 110, kind: 1, request: "r2", service: "svc"},
		{ns: 200, kind: 0, request: "r2", service: "svc"},
		{ns: 210, kind: 0, request: "r1", service: "svc"},
	}
	var deviations []model.Deviation
	add := func(requestID, field, format string, args ...any) {
		deviations = append(deviations, model.Deviation{RequestID: requestID, Field: field})
	}
	globalPeak, servicePeak := sweep(events, doc, "request", add)
	if globalPeak != 2 || servicePeak["svc"] != 2 {
		t.Fatalf("peaks global=%d service=%v", globalPeak, servicePeak)
	}
	if len(deviations) < 2 {
		t.Fatalf("expected global and service deviations, got %+v", deviations)
	}
}

func TestFirstDeviationIsStableByRequestID(t *testing.T) {
	doc := &model.Document{
		Quota: model.QuotaStats{GlobalLimit: 2, ServiceLimits: map[string]int{"svc": 2}},
		Requests: []model.RequestResult{
			{Request: model.Request{ID: "b"}, Outcome: "success"},
			{Request: model.Request{ID: "a"}, Outcome: "success"},
		},
	}
	report := Run(VerifyOptions{Document: doc, Events: nil})
	if report.OK || report.FirstDeviation == nil {
		t.Fatalf("expected verification failure: %+v", report)
	}
	if report.FirstDeviation.RequestID != "a" {
		t.Fatalf("first deviation=%+v", report.FirstDeviation)
	}
}
