package recovery

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"localmesh/internal/biz"
	"localmesh/internal/logx"
	"localmesh/internal/model"
	"localmesh/internal/rules"
	"localmesh/internal/snapshot"
	"localmesh/internal/verify"
)

type Options struct {
	CheckpointPath string
	Store          *snapshot.Store
	Logger         *logx.Logger
}

type Result struct {
	Document          *model.Document
	SnapshotPath      string
	TracePath         string
	RerunPath         string
	UnknownRequestIDs []string
	ReplayChecked     bool
	ReplayRound       string
}

func Recover(opts Options) (*Result, error) {
	cp, err := opts.Store.LoadCheckpoint(opts.CheckpointPath)
	if err != nil {
		return nil, err
	}
	log := opts.Logger
	if log == nil {
		log = logx.NewWriter(os.Stderr)
	}
	if cp.RoundID == "" {
		return nil, biz.WithField(biz.New(biz.CodeReplay, "recover", "checkpoint has no round id"), "round_id")
	}
	log.Info(cp.RoundID, "recover_start", map[string]any{
		"checkpoint": opts.CheckpointPath,
		"status":     cp.Status,
		"completed":  cp.Completed,
		"in_flight":  cp.InFlight,
	})

	replayChecked := false
	replayRound := ""
	latest, latestPath, err := opts.Store.LatestCompleted()
	if err != nil {
		return nil, err
	}
	if latest != nil {
		events, err := opts.Store.LoadTrace(latest.TracePath)
		if err != nil {
			return nil, replayError(cp.RoundID, latest.Round.ID, "trace", err.Error(), err)
		}
		report := verify.Run(verify.VerifyOptions{Document: latest, Events: events, SnapshotPath: latestPath, TracePath: latest.TracePath})
		if !report.OK {
			msg := "replay verification failed"
			field := "verification"
			if report.FirstDeviation != nil {
				msg = report.FirstDeviation.Message
				field = report.FirstDeviation.Field
			}
			be := replayError(cp.RoundID, latest.Round.ID, field, msg, nil)
			be.Fields["deviations"] = report.Deviations
			log.Error(cp.RoundID, "business_error", biz.Fields(be))
			return nil, be
		}
		replayChecked = true
		replayRound = latest.Round.ID
	}

	now := time.Now().UTC()
	start, _ := time.Parse(time.RFC3339Nano, cp.StartedAt)
	monoNS := now.Sub(start).Nanoseconds()
	if monoNS < 0 {
		monoNS = 0
	}
	events := append([]model.TraceEvent(nil), cp.Trace...)
	var unknownIDs []string
	var rerun []model.Request
	requests := make([]model.RequestResult, 0, len(cp.Requests))
	for i := range cp.Requests {
		state := cp.Requests[i]
		result := model.RequestResult{
			Request:        state.Request,
			RoutePrefix:    state.RoutePrefix,
			Service:        state.Service,
			TimeoutMS:      state.TimeoutMS,
			Retries:        state.Retries,
			AttemptBudget:  state.AttemptBudget,
			Attempts:       append([]model.Attempt(nil), state.Attempts...),
			Outcome:        state.Outcome,
			ErrorCode:      state.ErrorCode,
			Error:          state.Error,
			NeedsRerun:     state.NeedsRerun,
			CurrentAttempt: state.CurrentAttempt,
		}
		if state.Status != "done" {
			result.Outcome = "unknown"
			result.NeedsRerun = true
			result.ErrorCode = "E_UNKNOWN"
			result.Error = "process did not finish this request; rerun required"
			unknownIDs = append(unknownIDs, state.Request.ID)
			rerun = append(rerun, state.Request)
			events = append(events, model.TraceEvent{
				Type:        "request_unknown",
				RoundID:     cp.RoundID,
				RequestID:   state.Request.ID,
				Service:     state.Service,
				MonoMS:      monoNS / int64(time.Millisecond),
				MonoNS:      monoNS,
				Reason:      "process_terminated_before_completion",
				TriggeredAt: now.Format(time.RFC3339Nano),
			})
			if state.CurrentAttempt != nil {
				events = append(events, model.TraceEvent{
					Type:        "attempt_unknown",
					RoundID:     cp.RoundID,
					RequestID:   state.Request.ID,
					Service:     state.Service,
					InstanceID:  state.CurrentAttempt.InstanceID,
					AttemptNo:   state.CurrentAttempt.AttemptNo,
					MonoMS:      state.CurrentAttempt.StartedMonoMS,
					MonoNS:      state.CurrentAttempt.StartMonoNS,
					StartMonoNS: state.CurrentAttempt.StartMonoNS,
					Reason:      "attempt_in_flight_at_termination",
					TriggeredAt: now.Format(time.RFC3339Nano),
				})
			}
		}
		requests = append(requests, result)
	}
	sort.Strings(unknownIDs)

	tracePath := opts.Store.TracePath(cp.RoundID, "recovered")
	if _, err := opts.Store.WriteTrace(cp.RoundID, "recovered", events); err != nil {
		return nil, err
	}
	quota := cp.Quota
	quota.GlobalLimit = cp.Rules.GlobalConcurrency
	quota.RecoveredActiveGlobal = cp.ActiveGlobal
	quota.RecoveredActiveService = cp.ActiveService
	rs := &rules.RuleSet{Config: cp.Rules}
	doc := &model.Document{
		SchemaVersion: model.SchemaVersion,
		Round: model.RoundMeta{
			ID:                  cp.RoundID,
			Status:              "recovered",
			StartedAt:           cp.StartedAt,
			FinishedAt:          now.Format(time.RFC3339Nano),
			RulesPath:           cp.RulesPath,
			RulesVersion:        cp.RulesVersion,
			DeclaredVersion:     cp.DeclaredVersion,
			RulesHash:           cp.RulesHash,
			ExitReportAvailable: false,
		},
		Rules:                cp.Rules,
		Requests:             requests,
		CircuitEvents:        append([]model.CircuitEvent(nil), cp.CircuitEvents...),
		Breakers:             cp.Breakers,
		Quota:                quota,
		TracePath:            tracePath,
		RouteFingerprints:    rs.RouteFingerprints(),
		InstanceFingerprints: rs.InstanceFingerprints(),
	}
	doc.Summary = summarizeRecovered(doc)
	path, err := opts.Store.WriteRecoveredDocument(doc)
	if err != nil {
		return nil, err
	}
	rerunPath := ""
	if len(rerun) > 0 {
		rerunPath, err = opts.Store.WriteRerunRequests(cp.RoundID, rerun)
		if err != nil {
			return nil, err
		}
	}
	result := &Result{
		Document:          doc,
		SnapshotPath:      path,
		TracePath:         tracePath,
		RerunPath:         rerunPath,
		UnknownRequestIDs: unknownIDs,
		ReplayChecked:     replayChecked,
		ReplayRound:       replayRound,
	}
	log.Warn(cp.RoundID, "recover_end", map[string]any{
		"unknown_requests": len(unknownIDs),
		"snapshot":         path,
		"rerun_requests":   rerunPath,
		"replay_checked":   replayChecked,
		"replay_round":     replayRound,
		"exit_report":      false,
	})
	return result, nil
}

func replayError(roundID, replayRound, field, message string, err error) *biz.Error {
	be := biz.WithFields(biz.Wrap(biz.CodeReplay, "recover_replay", message, err), map[string]any{
		"round":        roundID,
		"replay_round": replayRound,
	})
	return biz.WithField(be, field)
}

func summarizeRecovered(doc *model.Document) model.Summary {
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

func HumanSummary(result *Result) string {
	ids := strings.Join(result.UnknownRequestIDs, ",")
	if ids == "" {
		ids = "none"
	}
	replay := "skipped"
	if result.ReplayChecked {
		replay = result.ReplayRound
	}
	return fmt.Sprintf("round=%s status=recovered unknown=%d rerun_file=%s replay_checked=%s exit_report=unavailable unknown_requests={%s}",
		result.Document.Round.ID, len(result.UnknownRequestIDs), result.RerunPath, replay, ids)
}
