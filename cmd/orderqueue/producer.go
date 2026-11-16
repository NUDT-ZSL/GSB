package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/client"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
)

type producerInput struct {
	RequestID       string `json:"request_id"`
	PartitionKey    string `json:"partition_key"`
	ProducerSession string `json:"producer_session"`
	ProducerSeq     int64  `json:"producer_seq"`
	Body            string `json:"body"`
}

type producerEntry struct {
	RequestID    string `json:"request_id"`
	PartitionKey string `json:"partition_key"`
	ProducerSeq  int64  `json:"producer_seq"`
	Offset       int64  `json:"offset,omitempty"`
	NextOffset   int64  `json:"next_offset,omitempty"`
	Code         string `json:"code"`
	Message      string `json:"message,omitempty"`
}

type producerReport struct {
	OK         bool            `json:"ok"`
	Code       string          `json:"code"`
	InputPath  string          `json:"input_path"`
	Address    string          `json:"address"`
	StartedAt  string          `json:"started_at"`
	FinishedAt string          `json:"finished_at"`
	Sent       int             `json:"sent"`
	Rejected   int             `json:"rejected"`
	Entries    []producerEntry `json:"entries"`
}

func runProduce(args []string) int {
	flags := flag.NewFlagSet("produce", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	address := flags.String("addr", "127.0.0.1:19160", "server address")
	inputPath := flags.String("input", "data/producer.jsonl", "producer JSONL input")
	reportPath := flags.String("report", "output/producer-report.json", "producer report path")
	dialTimeout := flags.Duration("dial-timeout", 3*time.Second, "TCP dial timeout")
	continueOnError := flags.Bool("continue-on-error", false, "continue after a rejected append")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	report := producerReport{Code: protocol.CodeOK, InputPath: *inputPath, Address: *address, StartedAt: time.Now().UTC().Format(time.RFC3339Nano), Entries: []producerEntry{}}
	file, err := os.Open(*inputPath)
	if err != nil {
		report.OK = false
		report.Code = protocol.CodeInternal
		_ = writeJSONAtomic(*reportPath, report)
		fmt.Fprintf(os.Stderr, "open producer input: %v\n", err)
		return 1
	}
	defer file.Close()
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
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 2*1024*1024)
	lineNumber := 0
	exitCode := 0
	for scanner.Scan() {
		lineNumber++
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		var input producerInput
		if err := json.Unmarshal([]byte(line), &input); err != nil {
			report.Rejected++
			report.Entries = append(report.Entries, producerEntry{RequestID: fmt.Sprintf("line-%d", lineNumber), Code: protocol.CodeProtocol, Message: err.Error()})
			exitCode = 1
			if !*continueOnError {
				break
			}
			continue
		}
		response, err := cli.Call(protocol.Request{
			Type:            "append",
			RequestID:       input.RequestID,
			PartitionKey:    input.PartitionKey,
			ProducerSession: input.ProducerSession,
			ProducerSeq:     input.ProducerSeq,
			Body:            input.Body,
		})
		if err != nil {
			report.Rejected++
			report.Entries = append(report.Entries, producerEntry{RequestID: input.RequestID, PartitionKey: input.PartitionKey, ProducerSeq: input.ProducerSeq, Code: protocol.CodeInternal, Message: err.Error()})
			exitCode = 1
			break
		}
		entry := producerEntry{
			RequestID:    response.RequestID,
			PartitionKey: input.PartitionKey,
			ProducerSeq:  input.ProducerSeq,
			Offset:       response.Offset,
			NextOffset:   response.NextOffset,
			Code:         response.Code,
			Message:      response.Message,
		}
		report.Entries = append(report.Entries, entry)
		if response.OK {
			report.Sent++
		} else {
			report.Rejected++
			exitCode = 1
			if !*continueOnError {
				break
			}
		}
	}
	if err := scanner.Err(); err != nil {
		report.Rejected++
		report.Code = protocol.CodeInternal
		report.Entries = append(report.Entries, producerEntry{Code: protocol.CodeInternal, Message: err.Error()})
		exitCode = 1
	}
	report.OK = exitCode == 0
	if !report.OK {
		report.Code = protocol.CodeAppend
	}
	report.FinishedAt = time.Now().UTC().Format(time.RFC3339Nano)
	if err := writeJSONAtomic(*reportPath, report); err != nil {
		fmt.Fprintf(os.Stderr, "write producer report: %v\n", err)
		return 1
	}
	fmt.Println(mustJSON(map[string]any{"ok": report.OK, "code": report.Code, "sent": report.Sent, "rejected": report.Rejected, "report": *reportPath}))
	return exitCode
}
