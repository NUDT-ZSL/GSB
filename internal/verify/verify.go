package verify

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"localmesh/internal/biz"
	"localmesh/internal/model"
)

type VerifyOptions struct {
	Document     *model.Document
	Events       []model.TraceEvent
	SnapshotPath string
	TracePath    string
}

// Run computes a deterministic report. It never stops at the first internal
// discrepancy: deviations are sorted so the reported first deviation and all
// diagnostics are stable across runs.
func Run(opts VerifyOptions) model.VerificationReport {
	doc := opts.Document
	report := model.VerificationReport{
		RoundID:          doc.Round.ID,
		RulesVersion:     doc.Round.RulesVersion,
		Total:            len(doc.Requests),
		Unknown:          0,
		TracePath:        opts.TracePath,
		SnapshotPath:     opts.SnapshotPath,
		QuotaPeakService: map[string]int{},
	}
	var deviations []model.Deviation
	add := func(requestID, field, format string, args ...any) {
		if requestID == "" {
			requestID = "summary"
		}
		deviations = append(deviations, model.Deviation{
			RequestID: requestID,
			Field:     field,
			Message:   fmt.Sprintf(format, args...),
		})
	}

	requests := append([]model.RequestResult(nil), doc.Requests...)
	sort.SliceStable(requests, func(i, j int) bool { return requests[i].ID < requests[j].ID })
	seen := make(map[string]bool)
	byRequest := make(map[string][]model.TraceEvent)
	for _, ev := range opts.Events {
		if ev.RequestID != "" {
			byRequest[ev.RequestID] = append(byRequest[ev.RequestID], ev)
		}
	}
	successCount, failureCount, unknownCount := 0, 0, 0
	for _, req := range requests {
		if seen[req.ID] {
			add(req.ID, "request_id", "duplicate request id in snapshot")
			continue
		}
		seen[req.ID] = true
		switch req.Outcome {
		case "success":
			successCount++
		case "failure":
			failureCount++
		case "unknown":
			unknownCount++
		default:
			add(req.ID, "outcome", "unknown final outcome %q", req.Outcome)
		}
		verifyRequest(req, byRequest[req.ID], add)
	}
	report.Success = successCount
	report.Failure = failureCount
	report.Unknown = unknownCount
	report.BreakerTriggers = countBreakerTriggers(opts.Events)

	globalPeak, servicePeak := verifyConcurrency(doc, opts.Events, add)
	report.QuotaPeakGlobal = globalPeak
	report.QuotaPeakService = servicePeak
	verifyCircuitCooldowns(opts.Events, add)

	if doc.Summary.Total != report.Total {
		add("", "summary.total", "summary.total=%d but requests=%d", doc.Summary.Total, report.Total)
	}
	if doc.Summary.Success != successCount {
		add("", "summary.success", "summary.success=%d but computed=%d", doc.Summary.Success, successCount)
	}
	if doc.Summary.Failure != failureCount {
		add("", "summary.failure", "summary.failure=%d but computed=%d", doc.Summary.Failure, failureCount)
	}
	if doc.Summary.Unknown != unknownCount {
		add("", "summary.unknown", "summary.unknown=%d but computed=%d", doc.Summary.Unknown, unknownCount)
	}
	if doc.Summary.BreakerTriggers != report.BreakerTriggers {
		add("", "summary.breaker_triggers", "summary.breaker_triggers=%d but trace=%d", doc.Summary.BreakerTriggers, report.BreakerTriggers)
	}
	if doc.Summary.QuotaPeakGlobal != globalPeak {
		add("", "summary.quota_peak_global", "summary quota peak=%d but trace=%d", doc.Summary.QuotaPeakGlobal, globalPeak)
	}
	for svc, computed := range servicePeak {
		if doc.Summary.QuotaPeakService[svc] != computed {
			add("", "summary.quota_peak_service", "service %s summary peak=%d but trace=%d", svc, doc.Summary.QuotaPeakService[svc], computed)
		}
	}
	if globalPeak > doc.Quota.GlobalLimit {
		add("", "quota_global", "global peak %d exceeds limit %d", globalPeak, doc.Quota.GlobalLimit)
	}
	for svc, computed := range servicePeak {
		if computed > doc.Quota.ServiceLimits[svc] {
			add("", "quota_service", "service %s peak %d exceeds quota %d", svc, computed, doc.Quota.ServiceLimits[svc])
		}
	}

	sort.SliceStable(deviations, func(i, j int) bool {
		if deviations[i].RequestID != deviations[j].RequestID {
			return deviations[i].RequestID < deviations[j].RequestID
		}
		if deviations[i].Field != deviations[j].Field {
			return deviations[i].Field < deviations[j].Field
		}
		return deviations[i].Message < deviations[j].Message
	})
	report.Deviations = deviations
	report.OK = len(deviations) == 0
	if report.OK {
		report.Code = ""
	} else {
		report.Code = biz.CodeVerify
		first := deviations[0]
		report.FirstDeviation = &first
	}
	return report
}

func verifyRequest(req model.RequestResult, events []model.TraceEvent, add func(string, string, string, ...any)) {
	starts, ends, fastFails, traceAttempts := 0, 0, 0, make(map[int]model.TraceEvent)
	var startNS, endNS int64
	for _, ev := range events {
		switch ev.Type {
		case "request_start":
			starts++
			startNS = ev.MonoNS
			if ev.Service != "" && req.Service != "" && ev.Service != req.Service {
				add(req.ID, "service", "trace service=%s snapshot service=%s", ev.Service, req.Service)
			}
		case "request_end":
			ends++
			endNS = ev.MonoNS
		case "fast_fail":
			fastFails++
		case "attempt":
			if _, exists := traceAttempts[ev.AttemptNo]; exists {
				add(req.ID, "attempts", "duplicate trace attempt_no=%d", ev.AttemptNo)
			}
			traceAttempts[ev.AttemptNo] = ev
		}
	}
	if starts != 1 {
		add(req.ID, "request_start", "expected one request_start event, got %d", starts)
	}
	if ends != 1 {
		add(req.ID, "request_end", "expected one request_end event, got %d", ends)
	}
	if starts == 1 && ends == 1 && endNS < startNS {
		add(req.ID, "request_interval", "request_end precedes request_start")
	}
	if len(traceAttempts) != len(req.Attempts) {
		add(req.ID, "attempts", "result.attempts=%d but trace has %d attempt events", len(req.Attempts), len(traceAttempts))
	}
	if req.AttemptBudget <= 0 {
		add(req.ID, "attempt_budget", "attempt budget must be positive")
	} else if len(req.Attempts) > req.AttemptBudget {
		add(req.ID, "attempt_budget", "used %d attempts but budget is %d", len(req.Attempts), req.AttemptBudget)
	}
	for i, attempt := range req.Attempts {
		if attempt.AttemptNo != i+1 {
			add(req.ID, "attempt_no", "attempt[%d].attempt_no=%d, want %d", i, attempt.AttemptNo, i+1)
		}
		ev, ok := traceAttempts[attempt.AttemptNo]
		if !ok {
			add(req.ID, "attempts", "snapshot attempt_no=%d has no trace event", attempt.AttemptNo)
			continue
		}
		if ev.InstanceID != attempt.InstanceID {
			add(req.ID, "instance_id", "attempt %d snapshot instance=%s trace instance=%s", attempt.AttemptNo, attempt.InstanceID, ev.InstanceID)
		}
		if ev.Outcome != attempt.Outcome {
			add(req.ID, "attempt_outcome", "attempt %d snapshot=%s trace=%s", attempt.AttemptNo, attempt.Outcome, ev.Outcome)
		}
		if ev.DurationMS != attempt.DurationMS {
			add(req.ID, "duration_ms", "attempt %d snapshot=%d trace=%d", attempt.AttemptNo, attempt.DurationMS, ev.DurationMS)
		}
		if starts == 1 && ev.StartMonoNS < startNS {
			add(req.ID, "attempt_interval", "attempt %d starts before request_start", attempt.AttemptNo)
		}
		if ends == 1 && ev.EndMonoNS > endNS {
			add(req.ID, "attempt_interval", "attempt %d ends after request_end", attempt.AttemptNo)
		}
	}

	lastOutcome := ""
	if len(req.Attempts) > 0 {
		lastOutcome = req.Attempts[len(req.Attempts)-1].Outcome
	}
	switch req.Outcome {
	case "success":
		if lastOutcome != "success" {
			add(req.ID, "outcome", "success result must end with a successful attempt")
		}
		if req.ErrorCode != "" {
			add(req.ID, "error_code", "successful request has error_code %s", req.ErrorCode)
		}
	case "failure":
		for _, attempt := range req.Attempts {
			if attempt.Outcome == "success" {
				add(req.ID, "outcome", "request has a successful attempt but final outcome is failure")
				break
			}
		}
		if req.ErrorCode == "" {
			add(req.ID, "error_code", "failed request is missing error_code")
		}
		if fastFails == 0 && len(req.Attempts) == 0 {
			add(req.ID, "fast_fail", "failed request has neither attempts nor a fast_fail event")
		}
		if fastFails == 0 && len(req.Attempts) < req.AttemptBudget {
			add(req.ID, "attempt_budget", "failed request used %d of %d attempts without a fast-fail event", len(req.Attempts), req.AttemptBudget)
		}
		if fastFails == 0 && len(req.Attempts) > 0 {
			last := req.Attempts[len(req.Attempts)-1]
			if last.ErrorCode != "" && req.ErrorCode != last.ErrorCode {
				add(req.ID, "error_code", "final error_code=%s but last attempt error_code=%s", req.ErrorCode, last.ErrorCode)
			}
		}
		if req.ErrorCode == "E_CIRCUIT_OPEN" && fastFails == 0 {
			add(req.ID, "error_code", "E_CIRCUIT_OPEN result has no fast_fail event")
		}
	case "unknown":
		if !req.NeedsRerun {
			add(req.ID, "needs_rerun", "unknown result must be marked for rerun")
		}
	}
}

type concurrencyEvent struct {
	ns      int64
	kind    int // 0=end, 1=start
	request string
	service string
}

func verifyConcurrency(doc *model.Document, events []model.TraceEvent, add func(string, string, string, ...any)) (int, map[string]int) {
	requestEvents := make([]concurrencyEvent, 0)
	attemptEvents := make([]concurrencyEvent, 0)
	for _, ev := range events {
		switch ev.Type {
		case "request_start":
			requestEvents = append(requestEvents, concurrencyEvent{ns: ev.MonoNS, kind: 1, request: ev.RequestID, service: ev.Service})
		case "request_end":
			requestEvents = append(requestEvents, concurrencyEvent{ns: ev.MonoNS, kind: 0, request: ev.RequestID, service: ev.Service})
		case "attempt":
			attemptEvents = append(attemptEvents, concurrencyEvent{ns: ev.StartMonoNS, kind: 1, request: ev.RequestID, service: ev.Service})
			attemptEvents = append(attemptEvents, concurrencyEvent{ns: ev.EndMonoNS, kind: 0, request: ev.RequestID, service: ev.Service})
		}
	}
	globalPeak, servicePeak := sweep(requestEvents, doc, "request", add)
	attemptGlobalPeak, attemptServicePeak := sweep(attemptEvents, doc, "attempt", add)
	if attemptGlobalPeak > globalPeak {
		globalPeak = attemptGlobalPeak
	}
	for svc, peak := range attemptServicePeak {
		if peak > servicePeak[svc] {
			servicePeak[svc] = peak
		}
	}
	return globalPeak, servicePeak
}

func sweep(events []concurrencyEvent, doc *model.Document, scope string, add func(string, string, string, ...any)) (int, map[string]int) {
	sort.SliceStable(events, func(i, j int) bool {
		if events[i].ns == events[j].ns {
			return events[i].kind < events[j].kind
		}
		return events[i].ns < events[j].ns
	})
	activeGlobal := 0
	activeService := map[string]int{}
	peakGlobal := 0
	peakService := map[string]int{}
	reportedGlobal := map[string]bool{}
	reportedService := map[string]bool{}
	for _, ev := range events {
		if ev.kind == 1 {
			activeGlobal++
			activeService[ev.service]++
			if activeGlobal > peakGlobal {
				peakGlobal = activeGlobal
			}
			if activeService[ev.service] > peakService[ev.service] {
				peakService[ev.service] = activeService[ev.service]
			}
			if activeGlobal > doc.Quota.GlobalLimit && !reportedGlobal[ev.request] {
				add(ev.request, scope+"_concurrency_global", "%s concurrency %d exceeds global limit %d", scope, activeGlobal, doc.Quota.GlobalLimit)
				reportedGlobal[ev.request] = true
			}
			key := ev.request + "\x00" + ev.service
			if activeService[ev.service] > doc.Quota.ServiceLimits[ev.service] && !reportedService[key] {
				add(ev.request, scope+"_concurrency_service", "service %s concurrency %d exceeds quota %d", ev.service, activeService[ev.service], doc.Quota.ServiceLimits[ev.service])
				reportedService[key] = true
			}
		} else {
			if activeGlobal > 0 {
				activeGlobal--
			}
			if activeService[ev.service] > 0 {
				activeService[ev.service]--
			}
		}
	}
	return peakGlobal, peakService
}

func verifyCircuitCooldowns(events []model.TraceEvent, add func(string, string, string, ...any)) {
	type interval struct {
		instance string
		from     int64
		until    int64
	}
	var opens []interval
	for _, ev := range events {
		if ev.Type == "breaker_open" && ev.InstanceID != "" {
			opens = append(opens, interval{instance: ev.InstanceID, from: ev.MonoNS, until: ev.CooldownUntilNS})
		}
	}
	for _, ev := range events {
		if ev.Type != "instance_selected" {
			continue
		}
		for _, open := range opens {
			if open.instance != ev.InstanceID {
				continue
			}
			selectedNS := ev.MonoNS
			if selectedNS == 0 {
				selectedNS = ev.StartMonoNS
			}
			if open.until > open.from && selectedNS >= open.from && selectedNS < open.until {
				add(ev.RequestID, "circuit_cooldown", "instance %s was selected at %d while cooling down until %d", ev.InstanceID, selectedNS, open.until)
				break
			}
		}
	}
}

func countBreakerTriggers(events []model.TraceEvent) int {
	n := 0
	for _, ev := range events {
		if ev.Type == "breaker_open" {
			n++
		}
	}
	return n
}

func Required(report model.VerificationReport) error {
	if report.OK {
		return nil
	}
	field := "verification"
	requestID := ""
	if report.FirstDeviation != nil {
		field = report.FirstDeviation.Field
		requestID = report.FirstDeviation.RequestID
	}
	msg := "verification failed"
	if report.FirstDeviation != nil {
		msg = report.FirstDeviation.Message
	}
	return biz.WithFields(biz.WithField(biz.New(biz.CodeVerify, "verify", msg), field), map[string]any{
		"request_id": requestID,
		"round":      report.RoundID,
		"deviations": len(report.Deviations),
	})
}

func HumanSummary(report model.VerificationReport) string {
	services := make([]string, 0, len(report.QuotaPeakService))
	for svc := range report.QuotaPeakService {
		services = append(services, svc)
	}
	sort.Strings(services)
	parts := make([]string, 0, len(services))
	for _, svc := range services {
		parts = append(parts, fmt.Sprintf("%s=%d", svc, report.QuotaPeakService[svc]))
	}
	return fmt.Sprintf("round=%s rules_version=%s total=%d success=%d failure=%d unknown=%d breaker_triggers=%d quota_peak_global=%d quota_peak_service={%s}",
		report.RoundID, report.RulesVersion, report.Total, report.Success, report.Failure, report.Unknown,
		report.BreakerTriggers, report.QuotaPeakGlobal, strings.Join(parts, ","))
}

func Timestamp() string { return time.Now().UTC().Format(time.RFC3339Nano) }
