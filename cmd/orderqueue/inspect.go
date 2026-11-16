package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/client"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/store"
)

type dumpWindow struct {
	Holder         string `json:"holder"`
	StartOffset    int64  `json:"start_offset"`
	EndOffset      int64  `json:"end_offset"`
	DeadlineUnixMS int64  `json:"deadline_unix_ms"`
	TTLMS          int64  `json:"ttl_ms"`
	Recovered      bool   `json:"recovered"`
}

type dumpPartition struct {
	PartitionKey      string         `json:"partition_key"`
	NextOffset        int64          `json:"next_offset"`
	CommittedOffset   int64          `json:"committed_offset"`
	ConfirmedOffset   int64          `json:"confirmed_offset"`
	Window            *dumpWindow    `json:"window,omitempty"`
	RetryCounts       map[string]int `json:"retry_counts"`
	PendingRedelivery bool           `json:"pending_redelivery"`
	RedeliveryFrom    int64          `json:"redelivery_from,omitempty"`
	Blocked           bool           `json:"blocked"`
	BlockedOffset     int64          `json:"blocked_offset,omitempty"`
	BlockedRetryCount int            `json:"blocked_retry_count,omitempty"`
	LedgerVersion     int64          `json:"ledger_version"`
	StateRevision     int64          `json:"state_revision"`
	MessageCount      int            `json:"message_count"`
	EventCount        int            `json:"event_count"`
}

type dumpReport struct {
	GeneratedAt         string          `json:"generated_at"`
	DBPath              string          `json:"db_path"`
	LogFormatVersion    int64           `json:"log_format_version"`
	LedgerFormatVersion int64           `json:"ledger_format_version"`
	MaxRetries          int             `json:"max_retries"`
	Partitions          []dumpPartition `json:"partitions"`
}

func runDump(args []string) int {
	flags := flag.NewFlagSet("dump", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	dbPath := flags.String("db", "output/orderqueue.db", "SQLite database path")
	outPath := flags.String("out", "", "output JSON path; stdout when empty")
	partitionFilter := flags.String("partition", "", "optional partition key filter")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	ctx := context.Background()
	db, err := store.OpenReadOnly(*dbPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open SQLite read-only: %v\n", err)
		return 1
	}
	defer db.Close()
	meta, err := db.LoadMeta(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read meta: %v\n", err)
		return 1
	}
	rows, err := db.ListPartitions(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read partitions: %v\n", err)
		return 1
	}
	report := dumpReport{
		GeneratedAt:         time.Now().UTC().Format(time.RFC3339Nano),
		DBPath:              *dbPath,
		LogFormatVersion:    meta.LogFormatVersion,
		LedgerFormatVersion: meta.LedgerFormatVersion,
		MaxRetries:          meta.MaxRetries,
		Partitions:          []dumpPartition{},
	}
	for _, row := range rows {
		if *partitionFilter != "" && row.PartitionKey != *partitionFilter {
			continue
		}
		retries, err := db.LoadRetryCounts(ctx, row.PartitionKey)
		if err != nil {
			fmt.Fprintf(os.Stderr, "read retry counts: %v\n", err)
			return 1
		}
		retryView := map[string]int{}
		for offset, attempts := range retries {
			retryView[strconv.FormatInt(offset, 10)] = attempts
		}
		messages, err := db.LoadMessages(ctx, row.PartitionKey)
		if err != nil {
			fmt.Fprintf(os.Stderr, "read messages: %v\n", err)
			return 1
		}
		events, err := db.LoadEvents(ctx, row.PartitionKey)
		if err != nil {
			fmt.Fprintf(os.Stderr, "read events: %v\n", err)
			return 1
		}
		partition := dumpPartition{
			PartitionKey:      row.PartitionKey,
			NextOffset:        row.NextOffset,
			CommittedOffset:   row.CommittedOffset,
			ConfirmedOffset:   row.ConfirmedOffset,
			RetryCounts:       retryView,
			PendingRedelivery: row.PendingRedelivery,
			RedeliveryFrom:    row.RedeliveryFrom,
			Blocked:           row.Blocked,
			BlockedOffset:     row.BlockedOffset,
			BlockedRetryCount: row.BlockedRetryCount,
			LedgerVersion:     row.LedgerVersion,
			StateRevision:     row.StateRevision,
			MessageCount:      len(messages),
			EventCount:        len(events),
		}
		if row.HasWindow() {
			partition.Window = &dumpWindow{
				Holder:         row.WindowHolder,
				StartOffset:    row.WindowStart,
				EndOffset:      row.WindowEnd,
				DeadlineUnixMS: row.WindowDeadlineUnixMS,
				TTLMS:          row.WindowTTLMS,
				Recovered:      row.WindowRecovered,
			}
		}
		report.Partitions = append(report.Partitions, partition)
	}
	if *outPath != "" {
		if err := writeJSONAtomic(*outPath, report); err != nil {
			fmt.Fprintf(os.Stderr, "write dump: %v\n", err)
			return 1
		}
		fmt.Println(mustJSON(map[string]any{"ok": true, "partitions": len(report.Partitions), "out": *outPath}))
		return 0
	}
	fmt.Println(mustJSON(report))
	return 0
}

func runStatus(args []string) int {
	flags := flag.NewFlagSet("status", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	address := flags.String("addr", "127.0.0.1:19160", "server address")
	partition := flags.String("partition", "", "optional partition key")
	outPath := flags.String("out", "", "output JSON path; stdout when empty")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	cli, err := client.Dial(*address, 3*time.Second)
	if err != nil {
		fmt.Fprintf(os.Stderr, "dial server: %v\n", err)
		return 1
	}
	defer cli.Close()
	resp, err := cli.Status(*partition)
	if err != nil {
		fmt.Fprintf(os.Stderr, "status request: %v\n", err)
		return 1
	}
	if !resp.OK {
		fmt.Fprintln(os.Stderr, mustJSON(resp))
		return 1
	}
	if *outPath != "" {
		if err := writeJSONAtomic(*outPath, resp); err != nil {
			fmt.Fprintf(os.Stderr, "write status: %v\n", err)
			return 1
		}
		fmt.Println(mustJSON(map[string]any{"ok": true, "out": *outPath, "partitions": len(resp.Status.Partitions)}))
		return 0
	}
	fmt.Println(mustJSON(resp))
	return 0
}
