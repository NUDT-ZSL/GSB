package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/client"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
)

type failurePlan struct {
	FormatVersion int           `json:"format_version"`
	Failures      []failureRule `json:"failures"`
}

type failureRule struct {
	PartitionKey string `json:"partition_key"`
	Offset       int64  `json:"offset,omitempty"`
	ProducerSeq  int64  `json:"producer_seq,omitempty"`
	Times        int    `json:"times"`
	Reason       string `json:"reason"`
}

type consumerMarker struct {
	Timestamp    string `json:"ts"`
	Session      string `json:"session"`
	PartitionKey string `json:"partition_key"`
	Offset       int64  `json:"offset"`
	ProducerSeq  int64  `json:"producer_seq"`
	RetryCount   int    `json:"retry_count"`
	PullNumber   int    `json:"pull_number"`
	Result       string `json:"result"`
	Detail       string `json:"detail,omitempty"`
}

type consumerReport struct {
	OK                bool             `json:"ok"`
	Code              string           `json:"code"`
	Address           string           `json:"address"`
	PartitionKey      string           `json:"partition_key"`
	Session           string           `json:"session"`
	StartedAt         string           `json:"started_at"`
	FinishedAt        string           `json:"finished_at"`
	Pulls             int              `json:"pulls"`
	Delivered         int              `json:"delivered"`
	Committed         int              `json:"committed"`
	Failed            int              `json:"failed"`
	Fenced            int              `json:"fenced"`
	Poisoned          bool             `json:"poisoned"`
	BlockedOffset     int64            `json:"blocked_offset,omitempty"`
	BlockedRetryCount int              `json:"blocked_retry_count,omitempty"`
	TargetOffset      int64            `json:"target_offset,omitempty"`
	Markers           []consumerMarker `json:"markers"`
}

func runConsume(args []string) int {
	flags := flag.NewFlagSet("consume", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	address := flags.String("addr", "127.0.0.1:19160", "server address")
	partitionKey := flags.String("partition-key", "orders.eu", "partition to consume")
	session := flags.String("session", "consumer-1", "consumer session id")
	limit := flags.Int("limit", 10, "pull batch limit")
	windowTimeout := flags.Int64("window-timeout-ms", 5000, "delivery window timeout in milliseconds")
	failurePath := flags.String("failure-plan", "", "failure injection JSON")
	markerPath := flags.String("marker-out", "output/consumer-markers.jsonl", "message result marker JSONL")
	reportPath := flags.String("report", "output/consumer-report.json", "consumer report JSON")
	targetOffset := flags.Int64("target-offset", 0, "stop after this committed offset")
	maxPulls := flags.Int("max-pulls", 10000, "maximum pulls")
	idlePulls := flags.Int("idle-pulls", 2, "exit after this many empty pulls")
	pullOnly := flags.Bool("pull-only", false, "pull one window without processing or committing")
	dialTimeout := flags.Duration("dial-timeout", 3*time.Second, "TCP dial timeout")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	report := consumerReport{
		Code:         protocol.CodeOK,
		Address:      *address,
		PartitionKey: *partitionKey,
		Session:      *session,
		StartedAt:    time.Now().UTC().Format(time.RFC3339Nano),
		TargetOffset: *targetOffset,
		Markers:      []consumerMarker{},
	}
	failures, err := loadFailureRules(*failurePath)
	if err != nil {
		report.OK = false
		report.Code = protocol.CodeProtocol
		report.FinishedAt = time.Now().UTC().Format(time.RFC3339Nano)
		_ = writeJSONAtomic(*reportPath, report)
		fmt.Fprintf(os.Stderr, "load failure plan: %v\n", err)
		return 1
	}
	markers, err := newMarkerWriter(*markerPath)
	if err != nil {
		report.OK = false
		report.Code = protocol.CodeInternal
		report.FinishedAt = time.Now().UTC().Format(time.RFC3339Nano)
		_ = writeJSONAtomic(*reportPath, report)
		fmt.Fprintf(os.Stderr, "open marker output: %v\n", err)
		return 1
	}
	defer markers.Close()
	cli, err := client.Dial(*address, *dialTimeout)
	if err != nil {
		report.OK = false
		report.Code = protocol.CodeInternal
		report.FinishedAt = time.Now().UTC().Format(time.RFC3339Nano)
		_ = writeJSONAtomic(*reportPath, report)
		fmt.Fprintf(os.Stderr, "dial server: %v\n", err)
		return 1
	}
	defer cli.Close()

	idle := 0
	exitCode := 0
	done := false
	for pullNumber := 1; pullNumber <= *maxPulls && !done; pullNumber++ {
		response, err := cli.Pull(*partitionKey, *session, *limit, *windowTimeout)
		if err != nil {
			report.OK = false
			report.Code = protocol.CodeInternal
			report.FinishedAt = time.Now().UTC().Format(time.RFC3339Nano)
			_ = writeJSONAtomic(*reportPath, report)
			fmt.Fprintf(os.Stderr, "pull: %v\n", err)
			return 1
		}
		report.Pulls++
		if !response.OK {
			switch response.Code {
			case protocol.CodeFence:
				report.Fenced++
				time.Sleep(30 * time.Millisecond)
				continue
			case protocol.CodePoison:
				report.Poisoned = true
				report.BlockedOffset = response.BlockedOffset
				report.BlockedRetryCount = response.BlockedRetryCount
				report.OK = false
				report.Code = protocol.CodePoison
				exitCode = 3
				done = true
				continue
			default:
				report.OK = false
				report.Code = response.Code
				exitCode = 1
				done = true
				continue
			}
		}
		if len(response.Messages) == 0 {
			idle++
			if idle >= *idlePulls {
				done = true
			}
			continue
		}
		idle = 0
		if *pullOnly {
			for _, message := range response.Messages {
				marker := consumerMarker{
					Timestamp:    time.Now().UTC().Format(time.RFC3339Nano),
					Session:      *session,
					PartitionKey: *partitionKey,
					Offset:       message.Offset,
					ProducerSeq:  message.ProducerSeq,
					RetryCount:   message.RetryCount,
					PullNumber:   pullNumber,
					Result:       "delivered_no_commit",
					Detail:       "window intentionally left unconfirmed for recovery demonstration",
				}
				report.Markers = append(report.Markers, marker)
				report.Delivered++
				if err := markers.Write(marker); err != nil {
					report.OK = false
					report.Code = protocol.CodeInternal
					exitCode = 1
					done = true
					break
				}
			}
			done = true
			continue
		}
		for _, message := range response.Messages {
			report.Delivered++
			rule, fail := nextFailure(failures, *partitionKey, message.Offset, message.ProducerSeq)
			if fail {
				marker := consumerMarker{
					Timestamp:    time.Now().UTC().Format(time.RFC3339Nano),
					Session:      *session,
					PartitionKey: *partitionKey,
					Offset:       message.Offset,
					ProducerSeq:  message.ProducerSeq,
					RetryCount:   message.RetryCount,
					PullNumber:   pullNumber,
					Result:       "fail",
					Detail:       rule.Reason,
				}
				report.Markers = append(report.Markers, marker)
				report.Failed++
				_ = markers.Write(marker)
				failResponse, err := cli.Fail(*partitionKey, *session, message.Offset, rule.Reason)
				if err != nil {
					report.OK = false
					report.Code = protocol.CodeInternal
					exitCode = 1
					done = true
					break
				}
				if !failResponse.OK {
					if failResponse.Code == protocol.CodePoison {
						report.Poisoned = true
						report.BlockedOffset = failResponse.BlockedOffset
						report.BlockedRetryCount = failResponse.BlockedRetryCount
						report.Code = protocol.CodePoison
						exitCode = 3
					} else {
						report.Code = failResponse.Code
						exitCode = 1
					}
					report.OK = false
					done = true
				}
				break
			}
			marker := consumerMarker{
				Timestamp:    time.Now().UTC().Format(time.RFC3339Nano),
				Session:      *session,
				PartitionKey: *partitionKey,
				Offset:       message.Offset,
				ProducerSeq:  message.ProducerSeq,
				RetryCount:   message.RetryCount,
				PullNumber:   pullNumber,
				Result:       "success",
			}
			report.Markers = append(report.Markers, marker)
			if err := markers.Write(marker); err != nil {
				report.OK = false
				report.Code = protocol.CodeInternal
				exitCode = 1
				done = true
				break
			}
			commitResponse, err := cli.Commit(*partitionKey, *session, message.Offset)
			if err != nil || !commitResponse.OK {
				report.OK = false
				if err != nil {
					report.Code = protocol.CodeInternal
					marker.Detail = err.Error()
				} else {
					report.Code = commitResponse.Code
					marker.Detail = commitResponse.Message
				}
				exitCode = 1
				done = true
				break
			}
			report.Committed++
			if *targetOffset > 0 && message.Offset >= *targetOffset {
				done = true
				break
			}
		}
	}
	if !done && report.Pulls >= *maxPulls {
		report.OK = false
		report.Code = protocol.CodeInternal
		exitCode = 1
	}
	if exitCode == 0 {
		report.OK = true
		report.Code = protocol.CodeOK
	}
	report.FinishedAt = time.Now().UTC().Format(time.RFC3339Nano)
	if err := writeJSONAtomic(*reportPath, report); err != nil {
		fmt.Fprintf(os.Stderr, "write consumer report: %v\n", err)
		return 1
	}
	fmt.Println(mustJSON(map[string]any{"ok": report.OK, "code": report.Code, "pulls": report.Pulls, "delivered": report.Delivered, "committed": report.Committed, "failed": report.Failed, "poisoned": report.Poisoned, "report": *reportPath}))
	return exitCode
}

func loadFailureRules(path string) ([]*failureRule, error) {
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var plan failurePlan
	if err := json.Unmarshal(data, &plan); err != nil {
		return nil, err
	}
	if plan.FormatVersion != 1 {
		return nil, fmt.Errorf("unsupported failure plan version %d", plan.FormatVersion)
	}
	rules := make([]*failureRule, 0, len(plan.Failures))
	for index := range plan.Failures {
		rule := plan.Failures[index]
		if rule.Times <= 0 {
			rule.Times = 1
		}
		if rule.Offset <= 0 && rule.ProducerSeq <= 0 {
			return nil, fmt.Errorf("failure rule %d must set offset or producer_seq", index)
		}
		rules = append(rules, &rule)
	}
	return rules, nil
}

func nextFailure(rules []*failureRule, partition string, offset, producerSeq int64) (*failureRule, bool) {
	for _, rule := range rules {
		if rule.PartitionKey != partition {
			continue
		}
		if rule.Offset != 0 && rule.Offset != offset {
			continue
		}
		if rule.ProducerSeq != 0 && rule.ProducerSeq != producerSeq {
			continue
		}
		if rule.Times > 0 {
			rule.Times--
			return rule, true
		}
	}
	return nil, false
}

type markerWriter struct {
	file *os.File
}

func newMarkerWriter(path string) (*markerWriter, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil && filepath.Dir(path) != "." {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &markerWriter{file: file}, nil
}

func (w *markerWriter) Write(marker consumerMarker) error {
	data, err := json.Marshal(marker)
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if _, err := w.file.Write(data); err != nil {
		return err
	}
	return w.file.Sync()
}

func (w *markerWriter) Close() error {
	if w == nil || w.file == nil {
		return nil
	}
	return w.file.Close()
}
