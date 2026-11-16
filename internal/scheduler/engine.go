package scheduler

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"localmesh/internal/biz"
	"localmesh/internal/circuit"
	"localmesh/internal/logx"
	"localmesh/internal/model"
	"localmesh/internal/rules"
	"localmesh/internal/selection"
	"localmesh/internal/snapshot"
)

type Options struct {
	Rules         *rules.RuleSet
	Requests      []model.Request
	RoundID       string
	Store         *snapshot.Store
	Logger        *logx.Logger
	InjectFailure string
}

type Engine struct {
	opts    Options
	client  *http.Client
	anchor  time.Time
	limiter *limiter
	breaker *circuit.Manager

	mu            sync.Mutex
	cp            *model.Checkpoint
	events        []model.TraceEvent
	circuitEvents []model.CircuitEvent
	fatal         error
	requestIndex  map[string]int
}

type Result struct {
	Document     *model.Document
	SnapshotPath string
	TracePath    string
}

func New(opts Options) *Engine {
	transport := &http.Transport{
		Proxy:                 nil,
		DisableKeepAlives:     true,
		DisableCompression:    true,
		MaxIdleConns:          16,
		IdleConnTimeout:       5 * time.Second,
		TLSHandshakeTimeout:   2 * time.Second,
		ExpectContinueTimeout: 100 * time.Millisecond,
	}
	dialer := &net.Dialer{Timeout: 3 * time.Second}
	dialFn := dialer.DialContext
	if opts.InjectFailure == "upstream" {
		dialer = &net.Dialer{Timeout: 150 * time.Millisecond}
		dialFn = func(ctx context.Context, network, _ string) (net.Conn, error) {
			return dialer.DialContext(ctx, network, "127.0.0.1:1")
		}
	}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		conn, err := dialFn(ctx, network, address)
		if err == nil {
			if phase, ok := ctx.Value(attemptPhaseKey{}).(*attemptPhase); ok {
				phase.connected.Store(true)
			}
		}
		return conn, err
	}
	return &Engine{
		opts: opts,
		client: &http.Client{
			Transport: transport,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		requestIndex: make(map[string]int, len(opts.Requests)),
	}
}

func (e *Engine) Run(ctx context.Context) (*Result, error) {
	if e.opts.Rules == nil || e.opts.Store == nil {
		return nil, biz.New(biz.CodeRule, "run", "rules and snapshot store are required")
	}
	if strings.TrimSpace(e.opts.RoundID) == "" {
		return nil, biz.WithField(biz.New(biz.CodeRule, "run", "round id must be non-empty"), "round")
	}
	if err := e.opts.Store.Ensure(); err != nil {
		return nil, err
	}
	if err := e.prepare(); err != nil {
		return nil, err
	}
	e.anchor = time.Now()
	e.limiter = newLimiter(e.opts.Rules.Config.GlobalConcurrency, quotaLimits(e.opts.Rules))
	e.breaker = circuit.New(e.opts.Rules, e.opts.RoundID)
	e.initCheckpoint()
	if err := e.saveCheckpoint(); err != nil {
		return nil, err
	}
	e.log().Info(e.opts.RoundID, "round_start", map[string]any{
		"rules_version":      e.opts.Rules.VersionID,
		"rules_hash":         e.opts.Rules.Hash,
		"requests":           len(e.opts.Requests),
		"global_concurrency": e.opts.Rules.Config.GlobalConcurrency,
		"service_quotas":     quotaLimits(e.opts.Rules),
	})

	var wg sync.WaitGroup
	for i := range e.opts.Requests {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			e.processRequest(ctx, index)
		}(i)
	}
	wg.Wait()

	if e.fatal != nil {
		return nil, e.fatal
	}
	hash, err := rules.Rehash(e.opts.Rules.Path)
	if err != nil {
		return nil, err
	}
	if hash != e.opts.Rules.Hash {
		e.finishInvalidated()
		be := biz.WithFields(biz.New(biz.CodeRule, "rules_changed", "rules file changed during the round; the mixed result was not installed as current"), map[string]any{
			"round":        e.opts.RoundID,
			"start_hash":   e.opts.Rules.Hash,
			"current_hash": hash,
		})
		e.log().Error(e.opts.RoundID, "business_error", biz.Fields(be))
		return nil, be
	}

	tracePath := e.opts.Store.TracePath(e.opts.RoundID, e.opts.Rules.VersionSlug())
	doc := e.buildDocument()
	doc.TracePath = tracePath
	if e.opts.InjectFailure == "snapshot" {
		be := biz.WithField(biz.New(biz.CodeSnapshot, "write_snapshot", "snapshot write failure injected by command line"), "inject_failure")
		e.log().Error(e.opts.RoundID, "business_error", biz.Fields(be))
		return nil, be
	}
	// Persist the completed checkpoint before the result snapshot. A crash after
	// this point still has a complete request trail and a valid rerun source.
	e.markCheckpointComplete()
	if err := e.saveCheckpoint(); err != nil {
		return nil, err
	}
	if _, err := e.opts.Store.WriteTrace(e.opts.RoundID, e.opts.Rules.VersionSlug(), e.events); err != nil {
		e.log().Error(e.opts.RoundID, "business_error", biz.Fields(err))
		return nil, err
	}
	path, err := e.opts.Store.WriteDocument(doc, true)
	if err != nil {
		e.log().Error(e.opts.RoundID, "business_error", biz.Fields(err))
		return nil, err
	}
	result := &Result{Document: doc, SnapshotPath: path, TracePath: tracePath}
	e.log().Info(e.opts.RoundID, "round_end", map[string]any{
		"rules_version":    doc.Round.RulesVersion,
		"status":           doc.Round.Status,
		"total":            doc.Summary.Total,
		"success":          doc.Summary.Success,
		"failure":          doc.Summary.Failure,
		"breaker_triggers": doc.Summary.BreakerTriggers,
		"quota_peak":       doc.Summary.QuotaPeakGlobal,
		"snapshot":         path,
		"trace":            tracePath,
	})

	if e.hasUpstreamFailure(doc) {
		be := biz.WithFields(biz.New(biz.CodeUpstream, "dispatch", "one or more upstream attempts were unreachable"), map[string]any{
			"round":    e.opts.RoundID,
			"snapshot": path,
			"trace":    tracePath,
			"injected": e.opts.InjectFailure == "upstream",
		})
		e.log().Error(e.opts.RoundID, "business_error", biz.Fields(be))
		return result, be
	}
	return result, nil
}

func (e *Engine) prepare() error {
	for i := range e.opts.Requests {
		req := &e.opts.Requests[i]
		route, ok := e.opts.Rules.Match(req.Path)
		if !ok {
			return biz.WithFields(biz.New(biz.CodeRule, "route_request", "request path does not match any route"), map[string]any{
				"request_id": req.ID,
				"path":       req.Path,
				"field":      "path",
			})
		}
		if _, ok := e.opts.Rules.ServiceByName[route.Service]; !ok {
			return biz.WithField(biz.New(biz.CodeRule, "route_request", "route references missing service"), "route")
		}
		e.requestIndex[req.ID] = i
	}
	return nil
}

func (e *Engine) initCheckpoint() {
	states := make([]model.RequestState, len(e.opts.Requests))
	for i, req := range e.opts.Requests {
		route, _ := e.opts.Rules.Match(req.Path)
		states[i] = model.RequestState{
			Request:       req,
			RoutePrefix:   route.Prefix,
			Service:       route.Service,
			TimeoutMS:     route.TimeoutMS,
			Retries:       route.Retries,
			AttemptBudget: route.AttemptBudget(),
			Status:        "pending",
			Outcome:       "pending",
		}
	}
	limits := quotaLimits(e.opts.Rules)
	e.cp = &model.Checkpoint{
		SchemaVersion:   model.SchemaVersion,
		RoundID:         e.opts.RoundID,
		Status:          "running",
		StartedAt:       e.anchor.UTC().Format(time.RFC3339Nano),
		RulesPath:       e.opts.Rules.Path,
		RulesVersion:    e.opts.Rules.VersionID,
		DeclaredVersion: e.opts.Rules.DeclaredVersion,
		RulesHash:       e.opts.Rules.Hash,
		Rules:           e.opts.Rules.Config,
		Requests:        states,
		ActiveService:   map[string]int{},
		Breakers:        e.breaker.Snapshot(),
		Quota: model.QuotaStats{
			GlobalLimit:   e.opts.Rules.Config.GlobalConcurrency,
			ServiceLimits: limits,
			ServicePeaks:  map[string]int{},
		},
	}
}

func (e *Engine) processRequest(ctx context.Context, index int) {
	req := e.opts.Requests[index]
	queueNS := e.nowNS()
	e.mu.Lock()
	e.events = append(e.events, model.TraceEvent{
		Type:      "request_queued",
		RoundID:   e.opts.RoundID,
		RequestID: req.ID,
		MonoMS:    queueNS / int64(time.Millisecond),
		MonoNS:    queueNS,
	})
	e.mu.Unlock()

	route, _ := e.opts.Rules.Match(req.Path)
	if err := e.limiter.acquire(ctx, route.Service); err != nil {
		e.finishRequestError(index, "E_UPSTREAM", err.Error(), queueNS)
		return
	}

	startNS := e.nowNS()
	activeGlobal, activeService := e.limiter.snapshot()
	e.mu.Lock()
	state := &e.cp.Requests[index]
	state.Status = "running"
	e.events = append(e.events, model.TraceEvent{
		Type:          "request_start",
		RoundID:       e.opts.RoundID,
		RequestID:     req.ID,
		Service:       route.Service,
		MonoMS:        startNS / int64(time.Millisecond),
		MonoNS:        startNS,
		ActiveGlobal:  activeGlobal,
		ActiveService: activeService[route.Service],
	})
	e.saveCheckpointLocked()
	e.mu.Unlock()

	instances := serviceInstances(e.opts.Rules, route.Service)
	finalSuccess := false
	finalCode := ""
	finalError := ""
	fastFailed := false

	for attemptNo := 1; attemptNo <= route.AttemptBudget(); attemptNo++ {
		if ctx.Err() != nil {
			finalCode, finalError = "E_UPSTREAM", ctx.Err().Error()
			break
		}
		order := selection.WeightedOrder(instances, e.opts.RoundID, req.ID, attemptNo)
		inst, ok, breakerEvents, openIDs := e.breaker.Select(order, req.ID, e.nowNS(), e.wallNow())
		if len(breakerEvents) > 0 {
			e.recordCircuitEvents(req.ID, breakerEvents)
		}
		if !ok {
			nowNS := e.nowNS()
			activeGlobal, activeService = e.limiter.snapshot()
			e.mu.Lock()
			e.events = append(e.events, model.TraceEvent{
				Type:                "fast_fail",
				RoundID:             e.opts.RoundID,
				RequestID:           req.ID,
				Service:             route.Service,
				MonoMS:              nowNS / int64(time.Millisecond),
				MonoNS:              nowNS,
				ActiveGlobal:        activeGlobal,
				ActiveService:       activeService[route.Service],
				Reason:              "all_circuits_open",
				TriggeredAt:         e.wallNow().UTC().Format(time.RFC3339Nano),
				TriggeringInstances: openIDs,
			})
			state := &e.cp.Requests[index]
			state.Outcome = "failure"
			state.ErrorCode = "E_CIRCUIT_OPEN"
			state.Error = "all circuits are open"
			e.saveCheckpointLocked()
			e.mu.Unlock()
			e.log().Warn(e.opts.RoundID, "circuit_fast_fail", map[string]any{
				"request_id": req.ID,
				"service":    route.Service,
				"instances":  openIDs,
				"reason":     "all_circuits_open",
			})
			finalCode, finalError, fastFailed = "E_CIRCUIT_OPEN", "all circuits are open", true
			break
		}

		selectionNS := e.nowNS()
		attemptStartNS := selectionNS
		activeGlobal, activeService = e.limiter.snapshot()
		attempt := model.Attempt{
			RequestID:     req.ID,
			RoundID:       e.opts.RoundID,
			AttemptNo:     attemptNo,
			InstanceID:    inst.ID,
			Service:       route.Service,
			Endpoint:      inst.Address,
			StartedMonoMS: attemptStartNS / int64(time.Millisecond),
			StartMonoNS:   attemptStartNS,
		}
		if e.opts.InjectFailure == "upstream" {
			attempt.InjectedFault = "upstream_unreachable"
		}
		e.mu.Lock()
		e.cp.Requests[index].CurrentAttempt = &attempt
		e.events = append(e.events, model.TraceEvent{
			Type:          "instance_selected",
			RoundID:       e.opts.RoundID,
			RequestID:     req.ID,
			Service:       route.Service,
			InstanceID:    inst.ID,
			AttemptNo:     attemptNo,
			MonoMS:        selectionNS / int64(time.Millisecond),
			MonoNS:        selectionNS,
			ActiveGlobal:  activeGlobal,
			ActiveService: activeService[route.Service],
		})
		e.saveCheckpointLocked()
		e.mu.Unlock()

		httpResult := e.performHTTP(ctx, inst, req, route.TimeoutMS)
		attempt.EndMonoNS = httpResult.endNS
		attempt.DurationMS = httpResult.durationMS
		attempt.Outcome = httpResult.outcome
		attempt.ErrorCode = httpResult.errorCode
		attempt.Error = httpResult.errText
		attempt.HTTPStatus = httpResult.status

		e.mu.Lock()
		cpState := &e.cp.Requests[index]
		cpState.Attempts = append(cpState.Attempts, attempt)
		cpState.CurrentAttempt = nil
		e.events = append(e.events, model.TraceEvent{
			Type:          "attempt",
			RoundID:       e.opts.RoundID,
			RequestID:     req.ID,
			Service:       route.Service,
			InstanceID:    inst.ID,
			AttemptNo:     attemptNo,
			MonoMS:        attemptStartNS / int64(time.Millisecond),
			MonoNS:        attemptStartNS,
			StartMonoNS:   attempt.StartMonoNS,
			EndMonoNS:     attempt.EndMonoNS,
			DurationMS:    attempt.DurationMS,
			ActiveGlobal:  activeGlobal,
			ActiveService: activeService[route.Service],
			Outcome:       attempt.Outcome,
			ErrorCode:     attempt.ErrorCode,
			Error:         attempt.Error,
			HTTPStatus:    attempt.HTTPStatus,
		})
		e.saveCheckpointLocked()
		e.mu.Unlock()

		e.log().Info(e.opts.RoundID, "attempt", map[string]any{
			"request_id":  req.ID,
			"service":     route.Service,
			"instance_id": inst.ID,
			"attempt_no":  attemptNo,
			"outcome":     attempt.Outcome,
			"duration_ms": attempt.DurationMS,
			"error_code":  attempt.ErrorCode,
			"http_status": attempt.HTTPStatus,
		})

		if attempt.Outcome == "success" {
			events := e.breaker.Success(inst.ID, req.ID, e.nowNS(), e.wallNow())
			if len(events) > 0 {
				e.recordCircuitEvents(req.ID, events)
			}
			finalSuccess = true
			finalCode, finalError = "", ""
			break
		}
		finalCode, finalError = attempt.ErrorCode, attempt.Error
		events := e.breaker.Failure(inst.ID, req.ID, attempt.ErrorCode, e.nowNS(), e.wallNow())
		if len(events) > 0 {
			e.recordCircuitEvents(req.ID, events)
		}
	}

	endNS := e.nowNS()
	activeGlobal, activeService = e.limiter.snapshot()
	e.mu.Lock()
	state = &e.cp.Requests[index]
	state.CurrentAttempt = nil
	state.Status = "done"
	if finalSuccess {
		state.Outcome = "success"
		state.ErrorCode = ""
		state.Error = ""
	} else {
		state.Outcome = "failure"
		state.ErrorCode = finalCode
		state.Error = finalError
		if state.ErrorCode == "" {
			state.ErrorCode = "E_UPSTREAM"
		}
	}
	e.events = append(e.events, model.TraceEvent{
		Type:          "request_end",
		RoundID:       e.opts.RoundID,
		RequestID:     req.ID,
		Service:       route.Service,
		MonoMS:        endNS / int64(time.Millisecond),
		MonoNS:        endNS,
		ActiveGlobal:  activeGlobal,
		ActiveService: activeService[route.Service],
		Outcome:       state.Outcome,
		ErrorCode:     state.ErrorCode,
		Reason:        fastFailReason(fastFailed),
	})
	e.saveCheckpointLocked()
	e.mu.Unlock()
	e.limiter.release(route.Service)

	e.mu.Lock()
	e.saveCheckpointLocked()
	e.mu.Unlock()
}

func (e *Engine) finishRequestError(index int, code, message string, atNS int64) {
	e.mu.Lock()
	state := &e.cp.Requests[index]
	state.Status = "done"
	state.Outcome = "failure"
	state.ErrorCode = code
	state.Error = message
	e.events = append(e.events, model.TraceEvent{
		Type:      "request_queued",
		RoundID:   e.opts.RoundID,
		RequestID: state.Request.ID,
		Service:   state.Service,
		MonoMS:    atNS / int64(time.Millisecond),
		MonoNS:    atNS,
		Reason:    message,
	})
	e.saveCheckpointLocked()
	e.mu.Unlock()
}

type httpAttemptResult struct {
	outcome    string
	errorCode  string
	errText    string
	status     int
	endNS      int64
	durationMS int64
}

type attemptPhase struct {
	connected atomic.Bool
}

type attemptPhaseKey struct{}

func (e *Engine) performHTTP(ctx context.Context, inst rules.Instance, req model.Request, timeoutMS int64) httpAttemptResult {
	startWall := time.Now()
	startNS := e.nowNS()
	attemptCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutMS)*time.Millisecond)
	defer cancel()
	phase := &attemptPhase{}
	attemptCtx = context.WithValue(attemptCtx, attemptPhaseKey{}, phase)
	target := inst.Address
	if e.opts.InjectFailure == "upstream" {
		target = "127.0.0.1:1"
	}
	url := "http://" + target + req.Path
	httpReq, err := http.NewRequestWithContext(attemptCtx, req.Method, url, strings.NewReader(req.Body))
	if err != nil {
		return e.httpFailure(startWall, startNS, "E_UPSTREAM", err.Error(), 0)
	}
	for k, v := range req.Headers {
		httpReq.Header.Set(k, v)
	}
	httpReq.Header.Set("User-Agent", "localmesh/1")
	resp, err := e.client.Do(httpReq)
	if err != nil {
		code := "E_UPSTREAM"
		if phase.connected.Load() && (errors.Is(err, context.DeadlineExceeded) || errors.Is(attemptCtx.Err(), context.DeadlineExceeded)) {
			code = "E_TIMEOUT"
		} else if phase.connected.Load() {
			var netErr net.Error
			if errors.As(err, &netErr) && netErr.Timeout() {
				code = "E_TIMEOUT"
			}
		}
		return e.httpFailure(startWall, startNS, code, err.Error(), 0)
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
	_ = resp.Body.Close()
	endWall := time.Now()
	endNS := startNS + endWall.Sub(startWall).Nanoseconds()
	duration := endWall.Sub(startWall).Milliseconds()
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return httpAttemptResult{outcome: "success", status: resp.StatusCode, endNS: endNS, durationMS: duration}
	}
	return httpAttemptResult{
		outcome:    "failure",
		errorCode:  "E_HTTP",
		errText:    fmt.Sprintf("upstream returned HTTP %d", resp.StatusCode),
		status:     resp.StatusCode,
		endNS:      endNS,
		durationMS: duration,
	}
}

func (e *Engine) httpFailure(startWall time.Time, startNS int64, code, message string, status int) httpAttemptResult {
	endWall := time.Now()
	// Preserve the injected fault text even if the low-level dial error varies by OS.
	if e.opts.InjectFailure == "upstream" {
		message = "injected upstream unreachable: " + message
	}
	return httpAttemptResult{
		outcome:    "failure",
		errorCode:  code,
		errText:    message,
		status:     status,
		endNS:      startNS + endWall.Sub(startWall).Nanoseconds(),
		durationMS: endWall.Sub(startWall).Milliseconds(),
	}
}

func (e *Engine) recordCircuitEvents(requestID string, events []model.CircuitEvent) {
	e.mu.Lock()
	for i := range events {
		ev := events[i]
		ev.MonoMS = ev.MonoNS / int64(time.Millisecond)
		e.circuitEvents = append(e.circuitEvents, ev)
		e.events = append(e.events, model.TraceEvent{
			Type:                ev.Type,
			RoundID:             ev.RoundID,
			RequestID:           requestID,
			Service:             ev.Service,
			InstanceID:          ev.InstanceID,
			MonoMS:              ev.MonoMS,
			MonoNS:              ev.MonoNS,
			Reason:              ev.Reason,
			TriggeredAt:         ev.TriggeredAt,
			CooldownUntilMonoMS: ev.CooldownUntilMonoMS,
			CooldownUntilNS:     ev.CooldownUntilNS,
		})
	}
	e.saveCheckpointLocked()
	e.mu.Unlock()
	for _, ev := range events {
		fields := map[string]any{
			"request_id":   requestID,
			"service":      ev.Service,
			"instance_id":  ev.InstanceID,
			"reason":       ev.Reason,
			"triggered_at": ev.TriggeredAt,
		}
		if ev.Type == "breaker_open" {
			fields["cooldown_until_ms"] = ev.CooldownUntilMonoMS
			e.log().Warn(e.opts.RoundID, ev.Type, fields)
		} else {
			e.log().Info(e.opts.RoundID, ev.Type, fields)
		}
	}
}

func (e *Engine) saveCheckpoint() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.saveCheckpointLocked()
}

func (e *Engine) saveCheckpointLocked() error {
	if e.cp == nil || e.fatal != nil {
		return e.fatal
	}
	activeGlobal, activeService := e.limiter.snapshot()
	peakGlobal, peakService := e.limiter.peaks()
	e.cp.SavedAt = time.Now().UTC().Format(time.RFC3339Nano)
	e.cp.ActiveGlobal = activeGlobal
	e.cp.ActiveService = activeService
	e.cp.Trace = append([]model.TraceEvent(nil), e.events...)
	e.cp.CircuitEvents = append([]model.CircuitEvent(nil), e.circuitEvents...)
	e.cp.Breakers = e.breaker.Snapshot()
	e.cp.Quota.GlobalPeak = peakGlobal
	e.cp.Quota.ServicePeaks = peakService
	e.cp.InFlight = e.inFlightLocked()
	e.cp.Completed = 0
	for i := range e.cp.Requests {
		if e.cp.Requests[i].Status == "done" {
			e.cp.Completed++
		}
	}
	if _, err := e.opts.Store.SaveCheckpoint(e.cp); err != nil {
		e.fatal = err
		e.log().Error(e.opts.RoundID, "business_error", biz.Fields(err))
		return err
	}
	return nil
}

func (e *Engine) inFlightLocked() []string {
	var ids []string
	for i := range e.cp.Requests {
		if e.cp.Requests[i].Status == "running" {
			ids = append(ids, e.cp.Requests[i].Request.ID)
		}
	}
	sort.Strings(ids)
	return ids
}

func (e *Engine) buildDocument() *model.Document {
	e.mu.Lock()
	defer e.mu.Unlock()
	requests := make([]model.RequestResult, len(e.cp.Requests))
	for i := range e.cp.Requests {
		s := e.cp.Requests[i]
		requests[i] = model.RequestResult{
			Request:        s.Request,
			RoutePrefix:    s.RoutePrefix,
			Service:        s.Service,
			TimeoutMS:      s.TimeoutMS,
			Retries:        s.Retries,
			AttemptBudget:  s.AttemptBudget,
			Attempts:       append([]model.Attempt(nil), s.Attempts...),
			Outcome:        s.Outcome,
			ErrorCode:      s.ErrorCode,
			Error:          s.Error,
			NeedsRerun:     s.NeedsRerun,
			QueuedMonoMS:   findQueuedMS(e.events, s.Request.ID),
			StartMonoMS:    findEventMS(e.events, s.Request.ID, "request_start"),
			EndMonoMS:      findEventMS(e.events, s.Request.ID, "request_end"),
			CurrentAttempt: nil,
		}
	}
	peakGlobal, peakService := e.limiter.peaks()
	doc := &model.Document{
		SchemaVersion: model.SchemaVersion,
		Round: model.RoundMeta{
			ID:                  e.opts.RoundID,
			Status:              "completed",
			StartedAt:           e.anchor.UTC().Format(time.RFC3339Nano),
			FinishedAt:          time.Now().UTC().Format(time.RFC3339Nano),
			RulesPath:           e.opts.Rules.Path,
			RulesVersion:        e.opts.Rules.VersionID,
			DeclaredVersion:     e.opts.Rules.DeclaredVersion,
			RulesHash:           e.opts.Rules.Hash,
			ExitReportAvailable: true,
		},
		Rules:                e.opts.Rules.Config,
		Requests:             requests,
		CircuitEvents:        append([]model.CircuitEvent(nil), e.circuitEvents...),
		Breakers:             e.breaker.Snapshot(),
		Quota:                model.QuotaStats{GlobalLimit: e.opts.Rules.Config.GlobalConcurrency, GlobalPeak: peakGlobal, ServiceLimits: quotaLimits(e.opts.Rules), ServicePeaks: peakService},
		RouteFingerprints:    e.opts.Rules.RouteFingerprints(),
		InstanceFingerprints: e.opts.Rules.InstanceFingerprints(),
	}
	doc.Summary = summarize(doc)
	return doc
}

func (e *Engine) finishInvalidated() {
	doc := e.buildDocument()
	doc.Round.Status = "invalidated"
	doc.Round.ExitReportAvailable = false
	doc.Round.RulesChangedDuringRun = true
	doc.Summary = summarize(doc)
	tracePath := e.opts.Store.TracePath(e.opts.RoundID, e.opts.Rules.VersionSlug())
	doc.TracePath = tracePath
	_, _ = e.opts.Store.WriteTrace(e.opts.RoundID, e.opts.Rules.VersionSlug(), e.events)
	_, _ = e.opts.Store.WriteDocument(doc, false)
	e.markCheckpointComplete()
	_ = e.saveCheckpoint()
}

func (e *Engine) markCheckpointComplete() {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.cp == nil {
		return
	}
	e.cp.Status = "completed"
	e.cp.SavedAt = time.Now().UTC().Format(time.RFC3339Nano)
}

func (e *Engine) hasUpstreamFailure(doc *model.Document) bool {
	for _, req := range doc.Requests {
		for _, attempt := range req.Attempts {
			if attempt.ErrorCode == "E_UPSTREAM" {
				return true
			}
		}
	}
	return false
}

func summarize(doc *model.Document) model.Summary {
	s := model.Summary{
		RoundID:          doc.Round.ID,
		RulesVersion:     doc.Round.RulesVersion,
		Total:            len(doc.Requests),
		QuotaPeakGlobal:  doc.Quota.GlobalPeak,
		QuotaPeakService: doc.Quota.ServicePeaks,
	}
	for _, req := range doc.Requests {
		switch req.Outcome {
		case "success":
			s.Success++
		case "failure":
			s.Failure++
		case "unknown":
			s.Unknown++
		}
	}
	for _, ev := range doc.CircuitEvents {
		if ev.Type == "breaker_open" {
			s.BreakerTriggers++
		}
	}
	return s
}

func (e *Engine) nowNS() int64       { return time.Since(e.anchor).Nanoseconds() }
func (e *Engine) wallNow() time.Time { return e.anchor.Add(time.Duration(e.nowNS())) }

func (e *Engine) log() *logx.Logger {
	if e.opts.Logger == nil {
		return logx.NewWriter(io.Discard)
	}
	return e.opts.Logger
}

func quotaLimits(set *rules.RuleSet) map[string]int {
	out := make(map[string]int, len(set.Config.Services))
	for _, svc := range set.Config.Services {
		out[svc.Name] = svc.Quota
	}
	return out
}

func serviceInstances(set *rules.RuleSet, name string) []rules.Instance {
	svc := set.ServiceByName[name]
	return svc.Instances
}

func findEventMS(events []model.TraceEvent, requestID, typ string) int64 {
	for _, ev := range events {
		if ev.RequestID == requestID && ev.Type == typ {
			return ev.MonoMS
		}
	}
	return 0
}

func findQueuedMS(events []model.TraceEvent, requestID string) int64 {
	return findEventMS(events, requestID, "request_queued")
}

func fastFailReason(fastFailed bool) string {
	if fastFailed {
		return "all_circuits_open"
	}
	return ""
}
